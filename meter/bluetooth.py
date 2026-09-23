"""Cross-platform BLE central; credentials never enter this module.

Pairing model (protocol 4, firmware with ``"auth": 1`` in its status):

* Each meter is keyed by its stable eFuse serial, never by the OS-specific BLE
  address. This computer generates a random 32-byte secret per meter and sends
  it while the owner has the meter's physical selection menu open.
* The meter remembers up to eight paired computers and authorizes only the one
  selected on the device. Each connection carries a fresh challenge in the
  status; the hello proves the secret with HMAC-SHA256 (``P``).
* Meters still running pre-secret firmware (2026.9.8/2026.9.13) are handled
  with the legacy ``H`` hello so they can receive the firmware that adds this.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import os
import queue
import random
import re
import secrets
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
SERIAL_PATTERN = re.compile(r'[0-9a-f]{12}\Z')
CHALLENGE_PATTERN = re.compile(r'[0-9a-f]{32}\Z')
FRAME_ACK_TIMEOUT = 30
NOTICE_MAX_AGE = 60
SECRET_SIZE = 32
AUTH_LABEL = b'SWM-AUTH-1'
MENU_NAME_SUFFIX = '-PAIR'
SCAN_SECONDS = 3
MAX_IDLE_GAP = 30
MAX_FOUND_GAP = 15
# Hello results shared by H and P.
HELLO_OK, HELLO_BUSY, HELLO_STORE_FAILED, HELLO_REJECTED, HELLO_PROVISION, HELLO_NOT_SELECTED = 0, 2, 5, 7, 8, 9
# Frame A results: 0 drawn now, 1 accepted and drawn with the next minute tick.
ACK_LABELS = {0: 'FULL', 1: 'QUEUED'}


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
    if 'auth' in value:
        # Authenticated-pairing firmware never names its selected computer.
        if (value['auth'] != 1 or type(value['auth']) is not int or 'selected_host' in value or
                not isinstance(value.get('serial'), str) or not SERIAL_PATTERN.fullmatch(value['serial']) or
                not isinstance(value.get('challenge'), str) or not CHALLENGE_PATTERN.fullmatch(value['challenge']) or
                type(value.get('selected')) is not bool or type(value.get('secured')) is not bool):
            raise ValueError('Invalid device pairing status')
    return value


def value_budget(client):
    mtu = getattr(client, 'mtu_size', 23)
    return min(182, max(20, mtu - 3)) if isinstance(mtu, int) else 20


def pairing_proof(secret, challenge_hex, serial, host_id):
    """First 16 bytes of HMAC-SHA256 over label, challenge, serial and host ID."""
    message = AUTH_LABEL + bytes.fromhex(challenge_hex) + serial.encode('ascii') + host_id.encode('ascii')
    return hmac.new(secret, message, hashlib.sha256).digest()[:16]


def device_key(address, status):
    serial = status.get('serial')
    return 'serial:' + serial if isinstance(serial, str) and SERIAL_PATTERN.fullmatch(serial) else 'address:' + address


class DiscoveryOpened(Exception):
    pass


class PairingRejected(Exception):
    """The meter refused this computer. This is never an OS permission problem.

    ``reason`` is ``'other_computer'`` (another computer is selected on the
    meter) or ``'unpaired'`` (no computer is selected yet). ``paired`` is True
    when the meter still recognizes this computer's secret.
    """
    def __init__(self, reason, *, paired=False):
        super().__init__('Select this computer using the meter buttons')
        self.reason, self.paired = reason, paired


class HelloBusy(RuntimeError):
    pass


_MESSAGES = {
    'bluetooth_off': 'Bluetooth is turned off. Turn it on to connect to your meter.',
    'bluetooth_unauthorized': ('Sweetmeter is not allowed to use Bluetooth. Allow it in your system '
                               'privacy or Bluetooth settings, then reopen Sweetmeter.'),
    'bluetooth_unavailable': 'No Bluetooth Low Energy adapter is available on this computer.',
    'device_not_found': 'The meter is out of range or asleep. Press its top button; Sweetmeter keeps looking.',
    'timeout': 'The meter did not respond in time. Sweetmeter will retry automatically.',
    'device_error': 'The meter sent an unexpected reply. Sweetmeter will retry automatically.',
    'connection_failed': 'Could not talk to the meter over Bluetooth. Sweetmeter will retry automatically.',
    'gatt_changed': ('The meter\'s Bluetooth services changed. In Bluetooth settings forget only the '
                     'Sweetmeter device, then let Sweetmeter reconnect.'),
    'worker_restarted': 'The Bluetooth connection restarted after an unexpected error.',
}
_HEALTH = {'bluetooth_off': 'off', 'bluetooth_unauthorized': 'unauthorized', 'bluetooth_unavailable': 'error'}


def classify_error(error):
    """Map bleak/OS errors to a stable code; see ``_MESSAGES`` for the text."""
    name = type(error).__name__
    text = str(error).lower()
    reason = getattr(getattr(error, 'reason', None), 'name', '')
    if name == 'BleakBluetoothNotAvailableError':
        if reason == 'POWERED_OFF':
            return 'bluetooth_off'
        if reason.startswith('DENIED'):
            return 'bluetooth_unauthorized'
        return 'bluetooth_unavailable'
    if isinstance(error, PermissionError) or 'not authorized' in text or 'unauthorized' in text or \
            'access denied' in text or 'permission' in text:
        return 'bluetooth_unauthorized'
    if 'turned off' in text or 'powered off' in text or 'poweredoff' in text or 'not ready' in text or \
            'notready' in text or 'radio is off' in text:
        return 'bluetooth_off'
    if 'no bluetooth adapter' in text or 'adapter not found' in text or 'no such adapter' in text:
        return 'bluetooth_unavailable'
    if name == 'BleakCharacteristicNotFoundError' or 'characteristic' in text and 'not found' in text:
        return 'gatt_changed'
    if name == 'BleakDeviceNotFoundError' or 'not found' in text:
        return 'device_not_found'
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return 'timeout'
    if isinstance(error, (ValueError, RuntimeError)) and not name.startswith('Bleak'):
        return 'device_error'
    return 'connection_failed'


class PairingStore:
    """Per-meter pairing secrets, private to this user (``pairings.json``, 0600)."""
    def __init__(self, state_dir):
        self.path = Path(state_dir) / 'pairings.json'
        self.lock = threading.RLock()
        self.meters = {}
        existing = self.path.exists()
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if raw.get('schema') != 1 or not isinstance(raw.get('meters'), dict):
                raise ValueError('Unknown pairing file')
            for key, record in raw['meters'].items():
                if isinstance(key, str) and isinstance(record, dict):
                    secret = record.get('secret')
                    if secret is not None and not (isinstance(secret, str) and re.fullmatch(r'[0-9a-f]{64}', secret)):
                        continue
                    self.meters[key] = record
            if os.name == 'posix' and self.path.stat().st_mode & 0o077:
                self.path.chmod(0o600)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self.meters = {}
        if not existing:
            # Earlier companions pinned one meter by BLE address after a legacy hello.
            try:
                pinned = json.loads((Path(state_dir) / 'bluetooth.json').read_text()).get('device_id')
            except (OSError, ValueError, TypeError, AttributeError):
                pinned = None
            if isinstance(pinned, str) and pinned:
                self.meters['address:' + pinned] = {'paired': True, 'legacy': True, 'address': pinned,
                                                    'updated_at': time.time()}
            self.save()

    def save(self):
        with self.lock:
            data = (json.dumps({'schema': 1, 'meters': self.meters}, sort_keys=True) + '\n').encode('utf-8')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            if os.name == 'posix':
                os.chmod(temp, 0o600)
            os.replace(temp, self.path)

    def secret(self, key):
        with self.lock:
            value = self.meters.get(key, {}).get('secret')
            return bytes.fromhex(value) if value else None

    def secret_for(self, key, address):
        """The secret registered with this meter, created (and saved) on first use."""
        with self.lock:
            record = self.meters.setdefault(key, {'paired': False})
            if not record.get('secret'):
                value = secrets.token_bytes(SECRET_SIZE)
                while not any(value):
                    value = secrets.token_bytes(SECRET_SIZE)
                record['secret'] = value.hex()
            record['address'], record['updated_at'] = address, time.time()
            self.save()
            return bytes.fromhex(record['secret'])

    def trusted(self, key, address):
        with self.lock:
            if self.meters.get(key, {}).get('paired'):
                return True
            legacy = self.meters.get('address:' + address, {})
            return bool(legacy.get('paired') and legacy.get('legacy'))

    def mark_paired(self, key, address, *, legacy=False):
        with self.lock:
            record = self.meters.setdefault(key, {})
            record.update(paired=True, address=address, updated_at=time.time())
            if legacy:
                record['legacy'] = True
            else:
                record.pop('legacy', None)
                # A meter migrated from pre-secret firmware is now keyed by serial.
                if key != 'address:' + address:
                    self.meters.pop('address:' + address, None)
            self.save()

    def mark_unpaired(self, key, address):
        with self.lock:
            changed = False
            for name in (key, 'address:' + address):
                record = self.meters.get(name)
                if record and record.get('paired'):
                    record['paired'] = False
                    changed = True
            if changed:
                self.save()

    def paired_addresses(self):
        with self.lock:
            return {r.get('address') for r in self.meters.values() if r.get('paired') and r.get('address')}

    def latest_address(self):
        with self.lock:
            paired = [r for r in self.meters.values() if r.get('paired') and r.get('address')]
            return max(paired, key=lambda r: r.get('updated_at', 0))['address'] if paired else None

    def forget_all(self):
        with self.lock:
            self.meters = {}
            self.save()


class Session:
    def __init__(self, client, host_id, name, emit, *, protocol=4):
        if protocol not in (3, 4):
            raise ValueError('Unsupported dashboard protocol')
        self.client, self.host_id, self.name, self.emit = client, host_id, name, emit
        self.protocol = protocol
        self.messages = asyncio.Queue()
        self.discovery = False
        self.sequence = 0
        self.tasks = set()

    def notification(self, _characteristic, raw):
        raw = bytes(raw)
        if raw == b'R':
            self.emit({'event': 'refresh'})
        elif raw == b'U':
            self.emit({'event': 'update_request'})
        elif len(raw) == 9 and raw[:1] == b'D':
            nonce, duration = struct.unpack('<II', raw[1:])
            self.discovery = True
            self.emit({'event': 'discovery', 'nonce': nonce, 'window_ms': duration})
            task = asyncio.create_task(self.client.disconnect())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
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

    async def _hello_result(self, packet, opcode=b'H'):
        await self.write(CONTROL_UUID, packet)
        reply = await self.wait(lambda p: len(p) == 2 and p[:1] == opcode, 10)
        if reply[1] == HELLO_BUSY:
            raise HelloBusy('Meter busy; hello will be retried')
        return reply[1]

    async def hello(self):
        """Legacy hello. Returns 0 (authorized) or 8 (provision a secret now)."""
        result = await self._hello_result(b'H' + self.host_id.encode('ascii') + self.name.encode('ascii'))
        if result not in (HELLO_OK, HELLO_PROVISION):
            raise PairingRejected('other_computer')
        return result

    async def authenticate(self, secret, challenge, serial):
        """Prove the pairing secret for this link's challenge (``P``)."""
        result = await self._hello_result(b'P' + pairing_proof(secret, challenge, serial, self.host_id))
        if result == HELLO_NOT_SELECTED:
            raise PairingRejected('other_computer', paired=True)
        if result != HELLO_OK:
            raise PairingRejected('other_computer')

    async def provision(self, secret):
        """Migration from pre-secret firmware: store our secret right after H."""
        if len(secret) != SECRET_SIZE:
            raise ValueError('Invalid pairing secret')
        result = await self._hello_result(b'Y' + secret, b'Y')
        if result != HELLO_OK:
            raise PairingRejected('other_computer')

    async def update_notice(self, code):
        await self.write(CONTROL_UUID, bytes((ord('u'), code)))

    async def clock(self):
        offset = int(datetime.now().astimezone().utcoffset().total_seconds())
        await self.write(CONTROL_UUID, struct.pack('<cIi', b'T', int(time.time()), offset))

    async def register(self, nonce, secret=None):
        """Add this computer to the meter's open selection menu.

        Authenticated-pairing firmware requires ``secret`` (32 bytes) in the
        body; pre-secret firmware must receive the legacy body without it.
        """
        session = random.SystemRandom().randint(1, 0xffffffff)
        body = self.host_id.encode('ascii') + bytes([len(self.name)]) + self.name.encode('ascii')
        if secret is not None:
            if len(secret) != SECRET_SIZE:
                raise ValueError('Invalid pairing secret')
            body += secret
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
        # Current firmware answers as soon as the frame is stored (1) and draws it
        # with the next minute tick; the first frame after boot is drawn first (0).
        ack = await self.wait(lambda p: len(p) == 10 and p[:1] == b'A' and
                              struct.unpack_from('<I', p, 2)[0] == self.sequence, FRAME_ACK_TIMEOUT)
        _, result, sequence, checksum = struct.unpack('<cBII', ack)
        if result not in ACK_LABELS or checksum != crc:
            raise RuntimeError(f'Device rejected dashboard ({result})')
        self.emit({'event': 'ack', 'sequence': sequence, 'crc32': f'{checksum:08x}',
                   'ack': ACK_LABELS[result], 'displayed': result == 0})


class Bluetooth:
    """Thread-safe façade; all GATT operations share one persistent asyncio loop.

    ``health`` is None until the first scan attempt finishes, then ``'ok'``,
    ``'off'``, ``'unauthorized'`` or ``'error'``.
    """
    def __init__(self, state_dir, *, scanner_factory=None, client_factory=None, start=True):
        self.state_dir = Path(state_dir)
        self.host_id, self.name = companion_identity(state_dir)
        self.store = PairingStore(self.state_dir)
        self.pinned = self.store.latest_address()
        self.health = None
        self.events = queue.Queue()
        self.stop, self.ready = threading.Event(), threading.Event()
        self.latest_frame, self.loop, self.ota = None, None, None
        self.startup_error = None
        self.frame_lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=1)
        self.notices = queue.Queue()
        self.scanner_factory, self.client_factory = scanner_factory, client_factory
        self.registered = {}
        self._not_before = {}
        self._idle_gap = 0
        self._wake = threading.Event()
        self._reset_requested = False
        self._forget_generation = 0
        self._last_error = (None, 0.0)
        self.thread = threading.Thread(target=self._thread, name='sweetmeter-ble', daemon=True)
        if start:
            self.thread.start()

    # --- worker supervision -------------------------------------------------
    def _thread(self):
        while not self.stop.is_set():
            try:
                if asyncio.run(self._run()):
                    break  # unrecoverable startup failure; already reported
            except BaseException as error:  # noqa: BLE001 - the worker must never die silently
                if self.stop.is_set():
                    break
                self._error('worker_restarted', error)
                self.stop.wait(1)
        self.ready.set()
        self.events.put({'event': 'exit'})

    async def _run(self):
        """Returns True only for an unrecoverable startup failure."""
        if self.scanner_factory is None:
            try:
                from bleak import BleakClient, BleakScanner
            except Exception as error:  # noqa: BLE001 - reported to the user
                self.startup_error = type(error).__name__
                self.events.put({'event': 'error', 'code': 'bluetooth_unavailable',
                                 'error': 'Bluetooth support failed to load: ' + type(error).__name__})
                return True
            self.scanner_factory, self.client_factory = BleakScanner, BleakClient
        self.loop = asyncio.get_running_loop()
        self.ready.set()
        while not self.stop.is_set():
            try:
                await self._cycle()
            except asyncio.CancelledError as error:
                if self.stop.is_set():
                    break
                task = asyncio.current_task()
                if task is not None and hasattr(task, 'uncancel'):
                    while task.cancelling():
                        task.uncancel()
                # Some backends leak CancelledError from internal timeouts.
                self._error('worker_restarted', error)
                await self._sleep(2)
            except BaseException as error:  # noqa: BLE001 - supervise and continue
                if self.stop.is_set():
                    break
                self._error('worker_restarted', error)
                await self._sleep(2)
        return False

    # --- scanning ------------------------------------------------------------
    async def _scan(self):
        devices = {}
        def found(device, advertisement):
            devices[device.address] = (device, advertisement)
        async with self.scanner_factory(detection_callback=found, service_uuids=[SERVICE_UUID]):
            await self._sleep(SCAN_SECONDS)
        return devices

    @staticmethod
    def _menu_hint(advertisement):
        name = getattr(advertisement, 'local_name', None)
        return isinstance(name, str) and name.endswith(MENU_NAME_SUFFIX)

    async def _cycle(self):
        if self._reset_requested:
            # Applied on the loop thread; rescan()/forget() only set the flag.
            self._reset_requested = False
            self._idle_gap = 0
            self._not_before.clear()
            self.registered.clear()
        try:
            devices = await self._scan()
        except Exception as error:  # noqa: BLE001 - mapped to health/error events
            code = classify_error(error)
            self._set_health(_HEALTH.get(code, 'error'))
            self._error(code, error)
            await self._sleep(self._next_idle_gap())
            return
        self._set_health('ok')
        paired = self.store.paired_addresses()
        order = sorted(devices.items(), key=lambda item: (item[0] not in paired, not self._menu_hint(item[1][1])))
        for address, (device, advertisement) in order:
            if self.stop.is_set():
                return
            hinted = self._menu_hint(advertisement)
            if not hinted and time.monotonic() < self._not_before.get(address, 0):
                continue
            delay = await self._visit(device, hinted)
            self._not_before[address] = time.monotonic() + delay
        if devices:
            # Scan again when the soonest nearby meter is due (a paired meter
            # that just disconnected is due at once); strangers' meters in a
            # long backoff do not keep this computer scanning every few seconds.
            self._idle_gap = 0
            soonest = min(self._not_before.get(address, 0) for address in devices) - time.monotonic()
            await self._sleep(random.uniform(2, 5) if soonest <= 5 else min(MAX_FOUND_GAP, soonest))
        else:
            await self._sleep(self._next_idle_gap())

    def _next_idle_gap(self):
        """No meter seen: back off 5, 10, 20, then 30 s between 3 s scans."""
        self._idle_gap = 5 if not self._idle_gap else min(MAX_IDLE_GAP, self._idle_gap * 2)
        return self._idle_gap + random.uniform(0, 1)

    def _set_health(self, state):
        if state != self.health:
            self.health = state
            self.events.put({'event': 'bluetooth_state', 'state': state})

    def _error(self, code, error=None):
        # Repeated identical errors are reported at most every 30 seconds.
        last, at = self._last_error
        now = time.monotonic()
        if last == code and now - at < 30:
            return
        self._last_error = (code, now)
        event = {'event': 'error', 'code': code, 'error': _MESSAGES[code]}
        if error is not None:
            event['detail'] = type(error).__name__
        self.events.put(event)

    # --- one meter -------------------------------------------------------------
    async def _visit(self, device, hinted):
        """Probe/drive one meter; returns seconds before probing it again."""
        address = device.address
        was_connected, key = False, None
        try:
            async with self.client_factory(device, timeout=20) as client:
                status = parse_status(await asyncio.wait_for(client.read_gatt_char(STATUS_UUID), 10))
                key = device_key(address, status)
                self._status(address, status, self.store.trusted(key, address))
                session = Session(client, self.host_id, self.name, self.events.put, protocol=status['protocol'])
                await session.subscribe()
                nonce = status.get('discovery_nonce', 0)
                if status['protocol'] == 4 and status.get('menu') and type(nonce) is int and nonce:
                    if self.registered.get(address) != nonce:
                        secret = self.store.secret_for(key, address) if status.get('auth') == 1 else None
                        await session.register(nonce, secret)
                        self.registered[address] = nonce
                        self.events.put({'event': 'registered', 'name': self.name})
                    return random.uniform(5, 9)
                legacy = status.get('auth') != 1
                if status.get('auth') == 1:
                    await self._authenticate(session, address, key, status)
                else:
                    selected = status.get('selected_host', '')
                    if selected and selected != self.host_id:
                        raise PairingRejected('other_computer')
                    if status['protocol'] == 4 and not selected:
                        raise PairingRejected('unpaired')
                    await session.hello()
                self.store.mark_paired(key, address, legacy=legacy)
                self.pinned = address
                was_connected = True
                self._idle_gap = 0
                self.events.put({'event': 'connected', 'device_id': address})
                await self._connected(session, address, status, key)
                return 0
        except DiscoveryOpened:
            return random.uniform(2, 5)
        except PairingRejected as rejection:
            return self._rejected(address, rejection, key)
        except HelloBusy:
            return random.uniform(2, 5)
        except Exception as error:  # noqa: BLE001 - a single meter must not stop the worker
            code = classify_error(error)
            if code in _HEALTH:
                self._set_health(_HEALTH[code])
            self._error(code, error)
            return random.uniform(2, 5)
        finally:
            if was_connected:
                self.events.put({'event': 'disconnected'})

    async def _authenticate(self, session, address, key, status):
        if not status['selected']:
            raise PairingRejected('unpaired')
        if status['secured']:
            secret = self.store.secret(key)
            if secret is None:
                raise PairingRejected('other_computer')
            await session.authenticate(secret, status['challenge'], status['serial'])
            return
        # The meter's selection predates pairing secrets. Only that computer's
        # legacy hello is accepted, once, and must immediately store a secret.
        if await session.hello() == HELLO_PROVISION:
            await session.provision(self.store.secret_for(key, address))

    def _rejected(self, address, rejection, key):
        if key is not None and not rejection.paired:
            self.store.mark_unpaired(key, address)
        event = {'event': 'selection_required', 'name': self.name, 'reason': rejection.reason}
        self.events.put(event)
        if rejection.reason == 'unpaired':
            return random.uniform(3, 5)
        # A computer that the meter still knows returns promptly when the owner
        # switches back to it; strangers to this meter back off politely.
        return random.uniform(8, 12) if rejection.paired else random.uniform(30, 45)

    async def _connected(self, session, address, status, key):
        sent, next_status = None, 0
        generation = self._forget_generation
        while session.client.is_connected and not self.stop.is_set():
            if session.discovery:
                raise DiscoveryOpened()
            if generation != self._forget_generation:
                await session.client.disconnect()
                return
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
                self._status(address, status, True)
                if status.get('menu'):
                    return
                await session.clock()
                next_status = now + 30
            while True:
                try:
                    code, queued_at = self.notices.get_nowait()
                except queue.Empty:
                    break
                # A result that could not be delivered promptly is stale.
                if status['protocol'] == 4 and time.monotonic() - queued_at <= NOTICE_MAX_AGE:
                    await session.update_notice(code)
            with self.frame_lock:
                frame = self.latest_frame
            if frame is not None and frame != sent and not status.get('critical'):
                await session.frame(frame)
                sent = frame
            await self._sleep(.1)

    def _status(self, address, status, trusted):
        self.events.put({'event': 'status', 'device_id': address, 'status': status, 'trusted': bool(trusted)})

    async def _sleep(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stop.is_set() and time.monotonic() < deadline:
            if self._wake.is_set():
                self._wake.clear()
                return
            await asyncio.sleep(min(.2, max(0, deadline - time.monotonic())))

    # --- public API (any thread) ------------------------------------------------
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

    def rescan(self):
        """User action: look for meters now and reset the idle scan backoff."""
        self._reset_requested = True
        self._wake.set()

    def update_notice(self, code):
        """Show a rocker-hold update result on the connected meter (dropped after 60 s)."""
        if code not in (2, 3, 4, 5):
            raise ValueError('Unknown update notice')
        self.notices.put((code, time.monotonic()))

    def forget(self):
        """Forget every paired meter and its secret on this computer."""
        self.store.forget_all()
        self.pinned = None
        self._forget_generation += 1
        self.rescan()
        self.events.put({'event': 'forgotten'})

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
