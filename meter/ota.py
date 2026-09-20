"""Protocol-4 acknowledged OTA transport, independent of release discovery/UI."""
from __future__ import annotations
import asyncio
import secrets
from pathlib import Path
from .bluetooth import value_budget
from .protocol import (OTA_CONTROL_UUID, OTA_DATA_UUID, OTA_STATUS_UUID, OTAState,
                       OTAError, OTAStatus, ota_begin, ota_fragment, ota_command,
                       ota_data, payload_limit)


class UpdateCancelled(RuntimeError):
    pass


class OTATransfer:
    def __init__(self, client, emit=lambda event: None):
        self.client, self.emit = client, emit
        self.notifications = asyncio.Queue()
        self.cancel = asyncio.Event()
        self.session = secrets.randbelow(0xffffffff) + 1
        self.commit_started = False
        self.value_size = value_budget(client)

    def notification(self, _characteristic, data):
        try:
            value = OTAStatus.decode(bytes(data))
        except ValueError:
            return  # Malformed/foreign notifications cannot acknowledge a write.
        if value.session == self.session:
            self.notifications.put_nowait(value)

    def _matches(self, status, opcode, state, offset, total):
        if status.session != self.session:
            return False
        if status.error != OTAError.OK:
            raise RuntimeError('Device update error: ' + status.error.name)
        return (status.opcode == ord(opcode) and status.state == state and
                status.offset == offset and status.total == total)

    async def exchange(self, characteristic, packet, opcode, state, offset, total, timeout=10):
        if self.cancel.is_set() and not self.commit_started and opcode != 'X':
            raise UpdateCancelled('Update cancelled')
        while not self.notifications.empty():
            self.notifications.get_nowait()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.wait_for(self.client.write_gatt_char(characteristic, packet, response=True), 10)
        while loop.time() < deadline:
            if self.cancel.is_set() and not self.commit_started and opcode != 'X':
                raise UpdateCancelled('Update cancelled')
            try:
                status = await asyncio.wait_for(self.notifications.get(), min(.25, max(.001, deadline-loop.time())))
            except asyncio.TimeoutError:
                continue
            if self._matches(status, opcode, state, offset, total):
                return status
        # One read fallback can prove an acknowledged offset even if notification was lost.
        raw = await asyncio.wait_for(self.client.read_gatt_char(OTA_STATUS_UUID), 10)
        status = OTAStatus.decode(bytes(raw))
        if self._matches(status, opcode, state, offset, total):
            return status
        raise TimeoutError('Update acknowledgement missing; no chunk was retransmitted')

    async def _cancel_remote(self):
        if self.commit_started or not self.client.is_connected:
            return
        try:
            # Cancellation retains current offsets; inspect the response separately.
            await asyncio.wait_for(self.client.write_gatt_char(
                OTA_CONTROL_UUID, ota_command('X', self.session), response=True), 10)
            deadline = asyncio.get_running_loop().time() + 10
            while asyncio.get_running_loop().time() < deadline:
                status = await asyncio.wait_for(self.notifications.get(), max(.001, deadline-asyncio.get_running_loop().time()))
                if status.session == self.session and status.opcode == ord('X'):
                    if status.state == OTAState.CANCELLED and status.error == OTAError.OK:
                        return
                    raise RuntimeError('Device cancellation not confirmed')
        except Exception:
            # Disconnect is a second, device-enforced precommit abort boundary.
            await self.client.disconnect()

    async def run(self, image_path, envelope, companion_version, *, usb_power=False):
        if usb_power is not True:
            raise ValueError('Explicit USB power acknowledgement required')
        path = Path(image_path)
        total = path.stat().st_size
        await self.client.start_notify(OTA_STATUS_UUID, self.notification)
        try:
            await self.exchange(OTA_CONTROL_UUID, ota_begin(self.session, len(envelope), companion_version,
                                usb_power=True), 'M', OTAState.METADATA, 0, len(envelope))
            size = payload_limit(self.value_size, metadata=True)
            for offset in range(0, len(envelope), size):
                chunk = envelope[offset:offset+size]
                await self.exchange(OTA_CONTROL_UUID, ota_fragment(self.session, offset, chunk,
                                    value_size=self.value_size), 'm', OTAState.METADATA,
                                    offset+len(chunk), len(envelope))
            self.emit({'event': 'ota_progress', 'phase': 'Preparing device', 'percent': 0, 'cancellable': True})
            await self.exchange(OTA_CONTROL_UUID, ota_command('S', self.session), 'S', OTAState.IMAGE, 0, total, 65)
            size = payload_limit(self.value_size)
            with path.open('rb') as handle:
                offset, last_percent = 0, -1
                while chunk := handle.read(size):
                    await self.exchange(OTA_DATA_UUID, ota_data(self.session, offset, chunk,
                                        value_size=self.value_size), 'd', OTAState.IMAGE,
                                        offset+len(chunk), total)
                    offset += len(chunk)
                    percent = int(100 * offset / total)
                    if percent != last_percent:
                        last_percent = percent
                        self.emit({'event': 'ota_progress', 'phase': 'Sending firmware',
                                   'percent': percent, 'cancellable': True})
            if offset != total:
                raise RuntimeError('Staged image changed during transfer')
            if self.cancel.is_set():
                raise UpdateCancelled('Update cancelled')
            self.commit_started = True
            self.emit({'event': 'ota_progress', 'phase': 'Verifying and restarting',
                       'percent': 100, 'cancellable': False})
            await self.exchange(OTA_CONTROL_UUID, ota_command('F', self.session),
                                'F', OTAState.REBOOTING, total, total, 35)
        except BaseException:
            await self._cancel_remote()
            raise
        finally:
            if self.client.is_connected:
                try:
                    await self.client.stop_notify(OTA_STATUS_UUID)
                except Exception:
                    pass
