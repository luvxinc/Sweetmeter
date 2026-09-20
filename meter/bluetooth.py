"""Cross-platform BLE central; credentials never enter this module."""
from __future__ import annotations
import asyncio
import json
import queue
import random
import re
import socket
import struct
import sys
import threading
import time
import uuid
import zlib
from datetime import datetime
from pathlib import Path

SERVICE_UUID = '7a1e0001-ff1b-4d9f-a023-47c7752c1a01'
CONTROL_UUID = '7a1e0002-ff1b-4d9f-a023-47c7752c1a01'
DATA_UUID = '7a1e0003-ff1b-4d9f-a023-47c7752c1a01'
STATUS_UUID = '7a1e0004-ff1b-4d9f-a023-47c7752c1a01'
HOST_PATTERN = re.compile(r'7a1e1000-ff1b-4d9f-a023-[0-9a-f]{12}\Z')
FRAME_ACK_TIMEOUT = 30

def companion_identity(state_dir):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / 'companion.json'
    try:
        host_id = json.loads(path.read_text(encoding='utf-8'))['host_id']
        if not isinstance(host_id, str) or not HOST_PATTERN.fullmatch(host_id):
            raise ValueError('Invalid host identity')
    except (OSError, ValueError, KeyError, TypeError):
        host_id = '7a1e1000-ff1b-4d9f-a023-' + uuid.uuid4().hex[-12:]
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'host_id': host_id}) + '\n', encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(path)
    name = ''.join(c for c in socket.gethostname().split('.')[0] if 32 <= ord(c) <= 126)[:20].strip()
    fallback = 'Mac' if sys.platform == 'darwin' else 'Windows PC' if sys.platform == 'win32' else 'Linux PC'
    return host_id, name or fallback

def parse_status(raw):
    if not 1 <= len(raw) <= 512:
        raise ValueError('Invalid device status length')
    value = json.loads(bytes(raw).decode('utf-8'))
    if not isinstance(value, dict) or type(value.get('protocol')) is not int or value['protocol'] not in (3, 4):
        raise ValueError('Unsupported device protocol')
    selected = value.get('selected_host', '')
    if not isinstance(selected, str) or selected and not HOST_PATTERN.fullmatch(selected):
        raise ValueError('Invalid selected computer')
    if not isinstance(value.get('firmware'), str):
        raise ValueError('Missing device firmware')
    return value

def value_budget(client):
    mtu = getattr(client, 'mtu_size', 23)
    return min(182, max(20, mtu - 3)) if isinstance(mtu, int) else 20

class DiscoveryOpened(Exception):
    pass

class Session:
    def __init__(self, client, host_id, name, emit, *, protocol=4):
        if protocol not in (3, 4):
            raise ValueError('Unsupported dashboard protocol')
        self.client, self.host_id, self.name, self.emit = client, host_id, name, emit
        self.protocol = protocol
        self.messages = asyncio.Queue()
        self.discovery = False
        self.sequence = 0

    def notification(self, _characteristic, raw):
        raw = bytes(raw)
        if raw == b'R':
            self.emit({'event': 'refresh'})
        elif len(raw) == 9 and raw[:1] == b'D':
            nonce, duration = struct.unpack('<II', raw[1:])
            self.discovery = True
            self.emit({'event': 'discovery', 'nonce': nonce, 'window_ms': duration})
            asyncio.create_task(self.client.disconnect())
            self.messages.put_nowait(raw)
        else:
            self.messages.put_nowait(raw)

    async def write(self, characteristic, packet):
        if self.discovery:
            raise DiscoveryOpened()
        await asyncio.wait_for(self.client.write_gatt_char(characteristic, packet, response=True), 10)

    async def wait(self, match, timeout):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.discovery:
                raise DiscoveryOpened()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError('Device acknowledgement timed out')
            packet = await asyncio.wait_for(self.messages.get(), remaining)
            if match(packet):
                return packet

    async def subscribe(self):
        await self.client.start_notify(CONTROL_UUID, self.notification)

    async def hello(self):
        await self.write(CONTROL_UUID, b'H' + self.host_id.encode('ascii') + self.name.encode('ascii'))
        reply = await self.wait(lambda p: len(p) == 2 and p[:1] == b'H', 10)
        if reply[1] != 0:
            raise PermissionError('Select this computer using the meter buttons')

    async def clock(self):
        offset = int(datetime.now().astimezone().utcoffset().total_seconds())
        await self.write(CONTROL_UUID, struct.pack('<cIi', b'T', int(time.time()), offset))

    async def register(self, nonce):
        session = random.SystemRandom().randint(1, 0xffffffff)
        body = self.host_id.encode('ascii') + bytes([len(self.name)]) + self.name.encode('ascii')
        async def step(packet, expected):
            await self.write(CONTROL_UUID, packet)
            ack = await self.wait(lambda p: len(p) == 10 and p[:1] == b'J' and
                                  struct.unpack_from('<I', p, 2)[0] == session, 5)
            _, result, _, offset = struct.unpack('<cBII', ack)
            if result or offset != expected:
                raise RuntimeError(f'Computer registration rejected ({result}, offset {offset})')
        await step(struct.pack('<cIIH', b'J', session, nonce, len(body)), 0)
        count = value_budget(self.client) - 9
        for offset in range(0, len(body), count):
            piece = body[offset:offset + count]
            await step(struct.pack('<cII', b'j', session, offset) + piece, offset + len(piece))
        await step(struct.pack('<cI', b'K', session), len(body))

    async def frame(self, frame):
        if len(frame) != 4000:
            raise ValueError('Expected 4000-byte frame')
        self.sequence = (self.sequence + 1) & 0xffffffff
        crc = zlib.crc32(frame)
        await self.write(CONTROL_UUID, struct.pack('<cIIH', b'B', self.sequence, crc, 4000))
        if self.protocol == 4:
            # The GATT response can precede the worker finishing a panel refresh.
            # No payload may enter its small queue until it explicitly admits B.
            ready = await self.wait(lambda p: len(p) == 10 and p[:1] == b'b' and
                                    struct.unpack_from('<II', p, 2) == (self.sequence, crc),
                                    FRAME_ACK_TIMEOUT)
            if ready[1] != 0:
                raise RuntimeError(f'Device rejected dashboard begin ({ready[1]})')
        chunk_size = min(180, value_budget(self.client) - 2)
        for offset in range(0, len(frame), chunk_size):
            await self.write(DATA_UUID, struct.pack('<H', offset) + frame[offset:offset + chunk_size])
        await self.write(CONTROL_UUID, struct.pack('<cI', b'C', self.sequence))
        ack = await self.wait(lambda p: len(p) == 10 and p[:1] == b'A' and
                              struct.unpack_from('<I', p, 2)[0] == self.sequence, FRAME_ACK_TIMEOUT)
        _, result, sequence, checksum = struct.unpack('<cBII', ack)
        if result not in (0, 1) or checksum != crc:
            raise RuntimeError(f'Device rejected dashboard ({result})')
        self.emit({'event': 'ack', 'sequence': sequence, 'crc32': f'{checksum:08x}',
                   'ack': 'SAME' if result else 'FULL'})

class Bluetooth:
    """Thread-safe façade; all GATT operations share one persistent asyncio loop."""
    def __init__(self, state_dir, *, scanner_factory=None, client_factory=None):
        self.state_dir = Path(state_dir)
        self.host_id, self.name = companion_identity(state_dir)
        self.pinned = None
        try:
            self.pinned = json.loads((self.state_dir / 'bluetooth.json').read_text()).get('device_id')
        except (OSError, ValueError, TypeError):
            pass
        self.events = queue.Queue()
        self.stop, self.ready = threading.Event(), threading.Event()
        self.latest_frame, self.loop, self.ota = None, None, None
        self.startup_error = None
        self.frame_lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=1)
        self.scanner_factory, self.client_factory = scanner_factory, client_factory
        self.thread = threading.Thread(target=self._thread, name='sweetmeter-ble', daemon=True)
        self.thread.start()

    def _thread(self):
        try:
            asyncio.run(self._run())
        except Exception as error:
            self.startup_error = type(error).__name__
            self.events.put({'event': 'error', 'error': 'Bluetooth worker: ' + type(error).__name__})
        finally:
            self.ready.set()
            self.events.put({'event': 'exit'})

    async def _run(self):
        if self.scanner_factory is None:
            from bleak import BleakClient, BleakScanner
            self.scanner_factory, self.client_factory = BleakScanner, BleakClient
        self.loop = asyncio.get_running_loop()
        self.ready.set()
        registered = {}
        while not self.stop.is_set():
            delay = random.uniform(2, 5)
            try:
                devices = {}
                def found(device, advertisement):
                    if self.pinned is None or device.address == self.pinned:
                        devices[device.address] = device
                async with self.scanner_factory(detection_callback=found, service_uuids=[SERVICE_UUID]):
                    await self._sleep(3)
                for device in devices.values():
                    if self.stop.is_set():
                        break
                    async with self.client_factory(device, timeout=20) as client:
                        status = parse_status(await asyncio.wait_for(client.read_gatt_char(STATUS_UUID), 10))
                        self._status(device.address, status)
                        session = Session(client, self.host_id, self.name, self.events.put,
                                          protocol=status['protocol'])
                        await session.subscribe()
                        nonce = status.get('discovery_nonce', 0)
                        if status['protocol'] == 4 and status.get('menu') and type(nonce) is int and nonce:
                            if registered.get(device.address) != nonce:
                                await session.register(nonce)
                                registered[device.address] = nonce
                                self.events.put({'event': 'registered', 'name': self.name})
                            delay = random.uniform(5, 9)
                            continue
                        selected = status.get('selected_host', '')
                        if (selected and selected != self.host_id) or (status['protocol'] == 4 and not selected):
                            delay = random.uniform(30, 45)
                            self.events.put({'event': 'selection_required', 'name': self.name})
                            continue
                        await session.hello()
                        self.pinned = device.address
                        self.events.put({'event': 'connected', 'device_id': device.address})
                        await self._connected(session, device.address, status)
            except DiscoveryOpened:
                delay = random.uniform(2, 5)
            except PermissionError:
                delay = random.uniform(30, 45)
            except Exception as error:
                self.events.put({'event': 'error', 'error': 'Bluetooth: ' + type(error).__name__})
            finally:
                self.events.put({'event': 'disconnected'})
            await self._sleep(delay)

    async def _connected(self, session, address, status):
        sent, next_status = None, 0
        while session.client.is_connected and not self.stop.is_set():
            if session.discovery:
                raise DiscoveryOpened()
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                job = None
            if job:
                if status['protocol'] != 4:
                    self.events.put({'event': 'ota_error', 'error': 'Legacy firmware requires USB bootstrap'})
                    continue
                from .ota import OTATransfer
                self.ota = OTATransfer(session.client, self.events.put)
                try:
                    await self.ota.run(**job)
                    self.events.put({'event': 'ota_rebooting'})
                except Exception as error:
                    kind = 'ota_unconfirmed' if self.ota.commit_started else 'ota_error'
                    self.events.put({'event': kind, 'error': str(error)[:200]})
                finally:
                    self.ota = None
                await session.client.disconnect()
                return
            now = time.monotonic()
            if now >= next_status:
                status = parse_status(await asyncio.wait_for(session.client.read_gatt_char(STATUS_UUID), 10))
                self._status(address, status)
                if status.get('menu'):
                    return
                await session.clock()
                next_status = now + 30
            with self.frame_lock:
                frame = self.latest_frame
            if frame is not None and frame != sent and not status.get('critical'):
                await session.frame(frame)
                sent = frame
            await self._sleep(.1)

    def _status(self, address, status):
        self.events.put({'event': 'status', 'device_id': address, 'status': status})

    async def _sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(min(.2, max(0, deadline - time.monotonic())))

    def poll(self, timeout=1):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def send(self, frame):
        if len(frame) != 4000:
            raise ValueError('Expected 4000-byte frame')
        with self.frame_lock:
            self.latest_frame = bytes(frame)

    def install_firmware(self, **job):
        if self.ota is not None:
            raise RuntimeError('An update is already running')
        self.jobs.put_nowait(job)

    def cancel_update(self):
        try:
            self.jobs.get_nowait()
            self.events.put({'event': 'ota_error', 'error': 'Update cancelled before transfer'})
        except queue.Empty:
            pass
        if self.loop and self.ota:
            self.loop.call_soon_threadsafe(self.ota.cancel.set)

    def close(self):
        self.stop.set()
        self.cancel_update()
        self.thread.join(timeout=12)
