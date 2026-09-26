import isolation  # noqa: F401  (test sandbox; must be the first import)
import asyncio
import json
import os
import queue
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from meter.bluetooth import (Bluetooth, Session, PairingStore, PairingRejected, companion_identity,
                             parse_status, pairing_proof, classify_error, value_budget, advertised_kind,
                             meter_proof, CONTROL_UUID, DATA_UUID, DiscoveryOpened, JOB_EXPIRED,
                             RENAME_TIMEOUT)

HOST = '7a1e1000-ff1b-4d9f-a023-0123456789ab'
OTHER = '7a1e1000-ff1b-4d9f-a023-00000000000b'
SERIAL = 'a1b2c3d4e5f6'


def events_of(radio):
    result = []
    while not radio.events.empty():
        result.append(radio.events.get())
    return result


class FakeClient:
    mtu_size = 23
    is_connected = True
    def __init__(self):
        self.writes = []
        self.callback = None
        self.body = bytearray()
        self.frame = bytearray()
        self.result = 0
        self.hello_result = 0
        self.begin_result = 0
        self.auto_begin = True
        self.begin_written = asyncio.Event()
    async def start_notify(self, characteristic, callback):
        self.callback = callback
    async def disconnect(self):
        self.is_connected = False
    async def write_gatt_char(self, characteristic, data, response):
        assert response is True
        self.writes.append((characteristic, bytes(data)))
        if characteristic == DATA_UUID:
            offset = struct.unpack_from('<H', data)[0]
            assert offset == len(self.frame)
            self.frame.extend(data[2:])
        elif data[:1] in (b'H', b'P'):
            self.callback(None, b'H' + bytes([self.hello_result]))
        elif data[:1] == b'Y':
            self.callback(None, b'Y' + bytes([self.hello_result]))
        elif data[:1] == b'J':
            _, self.session, nonce, self.total = struct.unpack('<cIIH', data)
            self.callback(None, struct.pack('<cBII', b'J', self.result, self.session, 0))
        elif data[:1] == b'j':
            _, session, offset = struct.unpack('<cII', data[:9])
            assert offset == len(self.body)
            self.body.extend(data[9:])
            self.callback(None, struct.pack('<cBII', b'J', self.result, session, len(self.body)))
        elif data[:1] == b'K':
            self.callback(None, struct.pack('<cBII', b'J', self.result, self.session, self.total))
        elif data[:1] == b'B':
            _, self.sequence, self.crc, _ = struct.unpack('<cIIH', data)
            self.frame = bytearray()
            self.begin_written.set()
            if self.auto_begin:
                self.callback(None, struct.pack('<cBII', b'b', self.begin_result, self.sequence, self.crc))
        elif data[:1] == b'C':
            self.callback(None, struct.pack('<cBII', b'A', self.result, self.sequence, self.crc))


class FakeMeter:
    """Emulates the firmware pairing rules (tests/test_pairing.cpp covers the C++)."""
    def __init__(self, address='meter', *, serial=SERIAL, legacy_status=False, mutual=False, rogue=None):
        self.address, self.serial, self.legacy_status = address, serial, legacy_status
        # mutual: firmware that proves the secret back after N ("mutual": 1).
        # rogue: 'accept' answers every proof with 0, 'reject' with 7 (a
        # peripheral that copied the serial and knows no secret).
        self.mutual, self.rogue = mutual, rogue
        self.paired = {}            # host -> secret (None = migrated legacy selection)
        self.selected = None
        self.menu_nonce = 0
        self.connections = 0
        self.frames = []
        self.menu_name = False
        self.challenge = '00' * 16
        self.writes = []
        self.rename = False         # firmware with the meter name command (status "rename": 1)
        self.rename_result = 0
        self.label = b''
    def status(self):
        if self.legacy_status:
            return {'protocol': 4, 'firmware': '2026.9.13', 'selected_host': self.selected or '',
                    'menu': bool(self.menu_nonce), 'discovery_nonce': self.menu_nonce}
        status = {'protocol': 4, 'firmware': '2026.9.14', 'auth': 1, 'serial': self.serial,
                  'selected': self.selected is not None,
                  'secured': self.selected is not None and self.paired.get(self.selected) is not None,
                  'challenge': self.challenge, 'menu': bool(self.menu_nonce), 'discovery_nonce': self.menu_nonce}
        if self.mutual:
            status['mutual'] = 1
        if self.rename:
            status['rename'] = 1
        return status
    def client_factory(self, meters):
        meter_by_address = {m.address: m for m in meters}
        class Client:
            mtu_size = 23
            def __init__(self, device, **_):
                self.meter = meter_by_address[device.address]
                self.is_connected = True
                self.callback = None
                self.state = 'open'
                self.body = bytearray()
                self.nonce = None
            async def __aenter__(self):
                self.meter.connections += 1
                self.meter.challenge = os.urandom(16).hex()
                return self
            async def __aexit__(self, *_):
                self.is_connected = False
            async def read_gatt_char(self, _):
                return json.dumps(self.meter.status()).encode()
            async def start_notify(self, _, callback):
                self.callback = callback
            async def disconnect(self):
                self.is_connected = False
            async def write_gatt_char(self, characteristic, data, response):
                m, data = self.meter, bytes(data)
                m.writes.append(data)
                if characteristic == DATA_UUID:
                    self.frame.extend(data[2:])
                    return
                op = data[:1]
                if op == b'H':
                    host = data[1:37].decode()
                    if m.legacy_status and m.selected == host:
                        self.state, reply = 'authorized', 0
                    elif not m.legacy_status and m.selected == host and m.paired.get(host) is None and self.state == 'open':
                        self.state, reply = 'provisioning', 8
                    else:
                        self.state, reply = 'closed', 7
                    self.callback(None, b'H' + bytes([reply]))
                    if reply == 7:
                        self.is_connected = False
                elif op == b'N':
                    assert len(data) == 17 and self.nonce is None and self.state == 'open'
                    self.nonce = data[1:]
                elif op == b'P':
                    reply, proof = 7, b''
                    if m.rogue == 'accept':
                        reply = 0
                        proof = os.urandom(16) if self.nonce is not None and m.mutual else b''
                    elif m.rogue is None and self.state == 'open' and (not m.menu_nonce or self.nonce is not None):
                        for host, secret in m.paired.items():
                            if secret is not None and pairing_proof(secret, m.challenge, m.serial, host) == data[1:]:
                                # In the open menu a hello only reports membership.
                                reply = 0 if host == m.selected and not m.menu_nonce else 9
                                if self.nonce is not None and m.mutual:
                                    proof = meter_proof(secret, m.challenge, self.nonce, m.serial, host)
                    self.state = 'authorized' if reply == 0 else 'closed'
                    self.callback(None, b'H' + bytes([reply]) + proof)
                    if reply:
                        self.is_connected = False
                elif op == b'Y':
                    if self.state == 'provisioning' and len(data) == 33:
                        m.paired[m.selected] = data[1:]
                        self.state = 'authorized'
                        self.callback(None, b'Y\x00')
                    else:
                        self.callback(None, b'Y\x07')
                elif op == b'J':
                    _, self.session, nonce, self.total = struct.unpack('<cIIH', data)
                    self.body = bytearray()
                    self.callback(None, struct.pack('<cBII', b'J', 0, self.session, 0))
                elif op == b'j':
                    self.body.extend(data[9:])
                    self.callback(None, struct.pack('<cBII', b'J', 0, self.session, len(self.body)))
                elif op == b'K':
                    m.candidates = getattr(m, 'candidates', {})
                    host = bytes(self.body[:36]).decode()
                    size = self.body[36]
                    m.candidates[host] = bytes(self.body[37 + size:]) or None
                    self.callback(None, struct.pack('<cBII', b'J', 0, self.session, self.total))
                    self.is_connected = False
                elif op == b'L':
                    assert self.state == 'authorized' and len(data) == 2 + data[1]
                    if m.rename_result == 0:
                        m.label = data[2:]
                    self.callback(None, b'L' + bytes([m.rename_result]))
                elif op == b'T':
                    assert self.state == 'authorized'
                    m.clock_writes = getattr(m, 'clock_writes', 0) + 1
                    self.is_connected = False  # end the connected loop quickly
        Client.frame = bytearray()
        return Client
    def select(self, host):
        """The owner picks a computer from the menu (paired or newly registered)."""
        candidates = getattr(self, 'candidates', {})
        if host in candidates:
            self.paired[host] = candidates[host]
        self.selected = host
        self.menu_nonce = 0


def advertisement(meter, hinted, *, marker=True, name=True):
    """What the firmware advertises: pairing firmware adds the capability marker."""
    data = {} if meter.legacy_status or not marker else {0xFFFF: b'SM' + bytes([1 | (2 if hinted else 0)])}
    base = meter.label.decode() if meter.label else 'Sweetmeter-ABCD'
    local = (base + ('-PAIR' if hinted and not meter.legacy_status else '')) if name else None
    return SimpleNamespace(local_name=local, manufacturer_data=data, rssi=-58)


def radio_for(folder, meters, *, hints=(), marker=True, name=True):
    (Path(folder) / 'companion.json').write_text(json.dumps({'host_id': HOST}))
    radio = Bluetooth(folder, start=False)
    class Scanner:
        running = None
        def __init__(self, detection_callback, **_):
            self.found = detection_callback
        def report(self):
            for meter in meters:
                self.found(SimpleNamespace(address=meter.address),
                           advertisement(meter, meter.address in hints, marker=marker, name=name))
        async def __aenter__(self):
            Scanner.running = self
            self.report()
        async def __aexit__(self, *_):
            Scanner.running = None
    radio.scanner_factory = Scanner
    scan = radio._scan
    async def scan_again():
        # A running scanner keeps reporting the meters it hears.
        running = getattr(radio.scanner_factory, 'running', None)
        if running is not None and radio._scan_stack is not None:
            running.report()
        return await scan()
    radio._scan = scan_again
    radio.client_factory = meters[0].client_factory(meters) if meters else None
    async def no_sleep(_seconds):
        return None
    radio._sleep = no_sleep
    radio.loop = None
    return radio


class BluetoothTests(unittest.TestCase):
    def test_stable_identity_and_printable_names(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            with patch('meter.bluetooth.socket.gethostname', return_value='设备\n'):
                host, name = companion_identity(state)
                self.assertNotIn('\n', name)
                self.assertTrue(name)
            self.assertEqual(companion_identity(state)[0], host)
            (state / 'companion.json').write_text('{"host_id":"invalid"}')
            self.assertNotEqual(companion_identity(state)[0], host)
    def test_status_rejects_wrong_types_invalid_hosts_and_oversize(self):
        for raw in (b'{}', b'[]', b'x'*513, b'{"protocol":true}', b'{"protocol":4,"firmware":"2026.9.1","selected_host":"bad"}'):
            with self.assertRaises(ValueError): parse_status(raw)
        self.assertEqual(parse_status(b'{"protocol":3,"firmware":"QM3.2"}')['protocol'], 3)
    def test_authenticated_status_never_names_a_host_and_is_strict(self):
        good = {'protocol': 4, 'firmware': '2026.9.14', 'auth': 1, 'serial': SERIAL, 'selected': True,
                'secured': True, 'challenge': 'ab' * 16}
        self.assertEqual(parse_status(json.dumps(good).encode())['serial'], SERIAL)
        for change in ({'selected_host': HOST}, {'serial': 'A1B2C3D4E5F6'}, {'challenge': 'ab'}, {'auth': True},
                       {'selected': 1}, {'secured': None}, {'auth': 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_status(json.dumps({**good, **change}).encode())
    def test_proof_matches_firmware_vector(self):
        # Same vector as tests/test_pairing.cpp (firmware mbedtls HMAC path).
        proof = pairing_proof(bytes(range(1, 33)), bytes(range(0xa0, 0xb0)).hex(), SERIAL, HOST)
        self.assertEqual(proof.hex(), '18dfddf998637f8fbb7564618d611077')
    def test_safe_mtu_budget(self):
        self.assertEqual(value_budget(SimpleNamespace(mtu_size=23)), 20)
        self.assertEqual(value_budget(SimpleNamespace(mtu_size=512)), 182)
    def test_send_keeps_only_latest_frame_and_rejects_wrong_size(self):
        radio = Bluetooth.__new__(Bluetooth)
        radio.frame_lock = threading.Lock()
        radio.send(b'x'*4000)
        radio.send(b'y'*4000)
        self.assertEqual(radio.latest_frame, b'y'*4000)
        with self.assertRaises(ValueError): radio.send(b'bad')
    def test_error_classification_is_plain_and_stable(self):
        class BleakBluetoothNotAvailableError(Exception):
            def __init__(self, reason): super().__init__('x'); self.reason = SimpleNamespace(name=reason)
        class BleakDeviceNotFoundError(Exception): pass
        class BleakCharacteristicNotFoundError(Exception): pass
        cases = [(BleakBluetoothNotAvailableError('POWERED_OFF'), 'bluetooth_off'),
                 (BleakBluetoothNotAvailableError('DENIED_BY_USER'), 'bluetooth_unauthorized'),
                 (BleakBluetoothNotAvailableError('NO_BLUETOOTH'), 'bluetooth_unavailable'),
                 (Exception('Bluetooth device is turned off'), 'bluetooth_off'),
                 (Exception('BLE is not authorized - check macOS privacy settings'), 'bluetooth_unauthorized'),
                 (OSError('[WinError -2147020577] The device is not ready for use'), 'bluetooth_off'),
                 (Exception('org.bluez.Error.NotReady'), 'bluetooth_off'),
                 (Exception('No Bluetooth adapters found.'), 'bluetooth_unavailable'),
                 (BleakDeviceNotFoundError('AA'), 'device_not_found'),
                 (BleakCharacteristicNotFoundError('7a1e0005'), 'gatt_changed'),
                 (asyncio.TimeoutError(), 'timeout'), (ValueError('bad status'), 'device_error'),
                 (Exception('weird'), 'connection_failed')]
        for error, code in cases:
            with self.subTest(error=error):
                self.assertEqual(classify_error(error), code)
        # A meter refusing this computer is not a permission problem.
        self.assertNotIsInstance(PairingRejected('other_computer'), PermissionError)


class PairingStoreTests(unittest.TestCase):
    def test_pending_secrets_are_fresh_and_promoted_only_explicitly(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PairingStore(folder)
            key = 'serial:' + SERIAL
            first = store.new_pending(key, 'addr-1')
            second = store.new_pending(key, 'addr-1')
            self.assertEqual(len(first), 32)
            self.assertNotEqual(first, second)  # every registration sends a new secret
            self.assertEqual(store.pending(key, 'addr-1'), second)
            self.assertIsNone(store.secret(key))
            self.assertFalse(store.trusted(key, 'addr-1'))
            self.assertTrue(store.related('addr-1') and not store.related('addr-9'))
            self.assertTrue(store.promote(key, 'addr-1', second))
            self.assertEqual(store.secret(key), second)
            self.assertIsNone(store.pending(key, 'addr-1'))
            reloaded = PairingStore(folder)
            self.assertTrue(reloaded.trusted(key, 'anything'))
            self.assertEqual(reloaded.secret(key), second)
            self.assertFalse(reloaded.needs_registration(key))
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE((Path(folder) / 'pairings.json').stat().st_mode), 0o600)
            generation = reloaded.generation
            reloaded.forget_all()
            self.assertIsNone(PairingStore(folder).secret(key))
            # Writers that started before Forget cannot resurrect anything.
            self.assertIsNone(reloaded.new_pending(key, 'addr-1', generation=generation))
            self.assertFalse(reloaded.promote(key, 'addr-1', second, generation=generation))
            self.assertFalse(reloaded.mark_paired(key, 'addr-1', generation=generation))
            self.assertEqual(reloaded.meters, {})

    def test_pending_secrets_are_bounded_per_address(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PairingStore(folder)
            for index in range(PairingStore.MAX_PENDING + 2):
                store.new_pending('serial:' + SERIAL, f'addr-{index}')
            self.assertEqual(len(store.meters['serial:' + SERIAL]['pending']), PairingStore.MAX_PENDING)
            self.assertIsNone(store.pending('serial:' + SERIAL, 'addr-0'))

    def test_legacy_pin_is_imported_once(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'bluetooth.json').write_text(json.dumps({'device_id': 'old-meter'}))
            store = PairingStore(folder)
            self.assertTrue(store.trusted('address:old-meter', 'old-meter'))
            self.assertTrue(store.legacy_record('old-meter') and store.related('old-meter'))
            self.assertFalse(store.legacy_record('another-meter'))
            store.forget_all()
            # After forgetting, the old pin cannot resurrect itself.
            self.assertFalse(PairingStore(folder).trusted('address:old-meter', 'old-meter'))

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'pairings.json').write_text(
                '{"schema":1,"meters":{"serial:x":{"secret":"zz"},'
                '"serial:y":{"secret":"' + '00' * 32 + '"},'
                '"serial:z":{"pending":{"a":{"secret":"bad"}}}}}')
            store = PairingStore(folder)
            self.assertIsNone(store.secret('serial:x'))
            self.assertIsNone(store.secret('serial:y'))
            self.assertIsNone(store.pending('serial:z', 'a'))


def confirm(radio, meter, key=None):
    """This computer and ``meter`` share a confirmed secret (as after pairing)."""
    key = key or 'serial:' + meter.serial
    secret = bytes([len(meter.address)]) * 32
    radio.store.promote(key, meter.address, secret)
    meter.paired[HOST] = secret
    return secret


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def visit(self, radio, meter, hinted=False):
        return await radio._visit(SimpleNamespace(address=meter.address), hinted)

    async def test_unselected_meter_requires_selection_without_trust(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            delay = await self.visit(radio, meter)
            events = events_of(radio)
            self.assertEqual([e['event'] for e in events], ['status', 'selection_required'])
            self.assertFalse(events[0]['trusted'])
            self.assertEqual(events[1]['reason'], 'unpaired')
            self.assertLessEqual(delay, 5)

    async def test_register_select_authenticate_then_switch_away_and_back(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            meter.menu_nonce = 77
            await self.visit(radio, meter, hinted=True)
            self.assertIn(HOST, meter.candidates)
            pending = radio.store.pending('serial:' + SERIAL, 'meter')
            self.assertEqual(meter.candidates[HOST], pending)
            self.assertIsNone(radio.store.secret('serial:' + SERIAL))  # not confirmed yet
            self.assertEqual(events_of(radio)[-1], {'event': 'registered', 'name': radio.name, 'device_id': 'meter'})
            meter.select(HOST)
            self.assertEqual(await self.visit(radio, meter), 0)
            events = events_of(radio)
            self.assertEqual([e['event'] for e in events][:3], ['status', 'status', 'connected'])
            self.assertEqual([e['trusted'] for e in events[:2]], [False, True])
            self.assertIn('disconnected', [e['event'] for e in events])
            self.assertEqual(radio.store.secret('serial:' + SERIAL), pending)  # proven, now confirmed
            self.assertIsNone(radio.store.pending('serial:' + SERIAL, 'meter'))
            self.assertEqual(radio.pinned, 'meter')
            # The owner switches the meter to another paired computer.
            meter.paired[OTHER] = b'\x05' * 32
            meter.select(OTHER)
            delay = await self.visit(radio, meter)
            events = events_of(radio)
            self.assertFalse(any(e.get('trusted') for e in events))  # never trusted without our authorization
            self.assertEqual(events[-1]['reason'], 'other_computer')
            self.assertLessEqual(delay, 12)  # prompt return when switched back
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'x'))
            meter.select(HOST)
            self.assertEqual(await self.visit(radio, meter), 0)
            self.assertIn('connected', [e['event'] for e in events_of(radio)])

    async def test_paired_computer_does_not_reregister_in_an_open_menu(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            confirm(radio, meter)
            meter.selected = HOST
            self.assertEqual(await self.visit(radio, meter), 0)
            meter.menu_nonce = 5
            await self.visit(radio, meter, hinted=True)
            self.assertNotIn(HOST, getattr(meter, 'candidates', {}))

    async def test_copied_serial_in_menu_never_receives_the_confirmed_secret(self):
        """A rogue peripheral copying the serial and claiming an open menu."""
        with tempfile.TemporaryDirectory() as folder:
            real, fake = FakeMeter('real'), FakeMeter('fake')
            radio = radio_for(folder, [real, fake], hints={'fake'})
            confirmed = confirm(radio, real)
            real.selected = HOST
            self.assertEqual(await self.visit(radio, real), 0)
            fake.menu_nonce = 99
            await self.visit(radio, fake, hinted=True)
            self.assertNotIn(HOST, getattr(fake, 'candidates', {}))  # paired: nothing is sent at all
            # Even when this computer must register again, only a fresh secret leaves it.
            radio.store.mark_unpaired('serial:' + SERIAL, 'real')
            fake.menu_nonce = 100
            await self.visit(radio, fake, hinted=True)
            leaked = fake.candidates[HOST]
            self.assertNotEqual(leaked, confirmed)
            # The fake's secret is useless on the real meter; ours still works there.
            self.assertEqual(radio.store.secret('serial:' + SERIAL), confirmed)
            self.assertEqual(await self.visit(radio, real), 0)
            self.assertNotIn(leaked, real.paired.values())

    async def test_copied_serial_claiming_unsecured_selection_gets_no_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            real, fake = FakeMeter('real'), FakeMeter('fake')
            radio = radio_for(folder, [real, fake])
            confirm(radio, real)
            fake.paired[HOST] = None  # "selected, secured: false": invites H then Y
            fake.selected = HOST
            await self.visit(radio, fake)
            self.assertFalse([p for p in fake.writes if p[:1] in (b'H', b'Y')])
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'real'))  # our pairing is untouched
            # Without any confirmed secret, still no TOFU unless a legacy record exists for that address.
            radio.store.forget_all()
            await self.visit(radio, fake)
            self.assertFalse([p for p in fake.writes if p[:1] in (b'H', b'Y')])

    async def test_status_is_untrusted_until_this_link_authenticates(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = FakeMeter('fake')
            radio = radio_for(folder, [fake])
            radio.store.promote('serial:' + SERIAL, 'fake', b'\x09' * 32)
            fake.paired[OTHER] = b'\x05' * 32
            fake.selected = OTHER  # our proof is refused
            await self.visit(radio, fake)
            statuses = [e for e in events_of(radio) if e['event'] == 'status']
            self.assertTrue(statuses and not any(e['trusted'] for e in statuses))

    async def test_refused_pending_secret_falls_back_to_confirmed(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            confirmed = confirm(radio, meter)
            meter.selected = HOST
            radio.store.new_pending('serial:' + SERIAL, 'meter')  # registered, never chosen on the meter
            delay = await self.visit(radio, meter)
            self.assertLess(delay, 5)  # one hello per link: retry soon with the confirmed secret
            self.assertIsNone(radio.store.pending('serial:' + SERIAL, 'meter'))
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'meter'))
            self.assertEqual(await self.visit(radio, meter), 0)
            self.assertEqual(radio.store.secret('serial:' + SERIAL), confirmed)

    async def test_removed_from_meter_loses_trust_and_backs_off(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            radio.store.promote('serial:' + SERIAL, 'meter', b'\x09' * 32)
            meter.paired[OTHER] = b'\x05' * 32
            meter.selected = OTHER
            delay = await self.visit(radio, meter)
            self.assertGreaterEqual(delay, 30)
            self.assertEqual(events_of(radio)[-1]['reason'], 'other_computer')
            self.assertFalse(radio.store.trusted('serial:' + SERIAL, 'meter'))
            self.assertTrue(radio.store.needs_registration('serial:' + SERIAL))

    async def test_meter_replacement_and_second_meter(self):
        with tempfile.TemporaryDirectory() as folder:
            old, new = FakeMeter('old', serial='000000000001'), FakeMeter('new', serial='000000000002')
            radio = radio_for(folder, [old, new], hints={'new'})
            confirm(radio, old)
            old.selected = HOST
            new.menu_nonce = 5
            await radio._cycle()
            self.assertIn(HOST, new.candidates)
            new.select(HOST)
            self.assertEqual(await self.visit(radio, new), 0)
            self.assertTrue(radio.store.trusted('serial:000000000002', 'new'))
            self.assertTrue(radio.store.trusted('serial:000000000001', 'old'))
            self.assertNotEqual(radio.store.secret('serial:000000000001'), radio.store.secret('serial:000000000002'))

    async def test_legacy_selection_migrates_with_one_hello_and_fresh_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            meter.paired[HOST] = None
            meter.selected = HOST
            radio = radio_for(folder, [meter])
            radio.store.mark_paired('address:meter', 'meter', legacy=True)  # old companion state
            self.assertEqual(await self.visit(radio, meter), 0)
            secret = radio.store.secret('serial:' + SERIAL)
            self.assertEqual(meter.paired[HOST], secret)
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'meter'))
            self.assertFalse(radio.store.legacy_record('meter'))  # migration happens once
            # Afterwards only the authenticated hello works.
            self.assertEqual(await self.visit(radio, meter), 0)

    async def test_legacy_migration_requires_a_legacy_record_for_that_address(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            meter.paired[HOST] = None
            meter.selected = HOST
            radio = radio_for(folder, [meter])
            radio.store.mark_paired('address:elsewhere', 'elsewhere', legacy=True)
            await self.visit(radio, meter)
            self.assertIsNone(meter.paired[HOST])
            # Unrelated pairing firmware with a closed menu: the link is closed before any write.
            self.assertEqual(meter.writes, [])
            self.assertNotIn('connected', [e['event'] for e in events_of(radio)])

    async def test_legacy_migration_by_another_computer_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            meter.paired[OTHER] = None
            meter.selected = OTHER
            radio = radio_for(folder, [meter])
            radio.store.mark_paired('address:meter', 'meter', legacy=True)
            await self.visit(radio, meter)
            self.assertEqual(events_of(radio)[-1]['reason'], 'other_computer')
            self.assertIsNone(meter.paired[OTHER])

    async def test_pre_secret_firmware_still_connects_for_its_update(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(legacy_status=True)
            meter.selected = HOST
            radio = radio_for(folder, [meter])
            self.assertEqual(await self.visit(radio, meter), 0)
            events = events_of(radio)
            self.assertIn('connected', [e['event'] for e in events])
            self.assertEqual([e['trusted'] for e in events if e['event'] == 'status'][:2], [False, True])
            self.assertTrue(radio.store.trusted('address:meter', 'meter'))
            # Pre-secret firmware registers with the legacy body (no secret).
            meter.selected, meter.menu_nonce = None, 9
            await self.visit(radio, meter)
            self.assertIsNone(meter.candidates[HOST])

    async def test_forget_during_authentication_is_not_undone(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            confirm(radio, meter)
            meter.selected = HOST
            original = Session.authenticate
            async def forget_meanwhile(session, *args, **kwargs):
                await original(session, *args, **kwargs)
                radio.forget()
            with patch.object(Session, 'authenticate', forget_meanwhile):
                await self.visit(radio, meter)
            kinds = [e['event'] for e in events_of(radio)]
            self.assertNotIn('connected', kinds)
            self.assertEqual(radio.store.meters, {})
            self.assertIsNone(radio.pinned)

    async def test_unrelated_meters_are_not_contacted_unless_their_menu_is_open(self):
        with tempfile.TemporaryDirectory() as folder:
            stranger = FakeMeter('stranger')
            radio = radio_for(folder, [stranger])
            await radio._cycle()
            self.assertEqual(stranger.connections, 0)  # no connection, so no bond on either side
            self.assertEqual([e['event'] for e in events_of(radio)], ['bluetooth_state', 'nearby', 'selection_required'])
            radio.scanner_factory = radio_for(folder, [stranger], hints={'stranger'}).scanner_factory
            stranger.menu_nonce = 3
            await radio._cycle()
            self.assertEqual(stranger.connections, 1)
            self.assertIn(HOST, stranger.candidates)
            # Pending registration relates it: after the menu closes it is contacted to authenticate.
            stranger.select(HOST)
            radio.scanner_factory = radio_for(folder, [stranger]).scanner_factory
            radio._not_before.clear()
            await radio._cycle()
            self.assertEqual(stranger.connections, 2)
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'stranger'))

    async def test_idle_scan_backs_off_and_rescan_resets(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            gaps = []
            async def record(seconds):
                gaps.append(seconds)
            radio._sleep = record
            for _ in range(5):
                await radio._cycle()
            idle = [int(g) for g in gaps if g != 3]
            self.assertEqual(idle, [5, 10, 20, 30, 30])
            radio.rescan()
            gaps.clear()
            await radio._cycle()
            self.assertLess([g for g in gaps if g != 3][0], 6)

    async def test_scan_failure_sets_health_and_plain_error(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            self.assertIsNone(radio.health)
            class Off:
                def __init__(self, **_): pass
                async def __aenter__(self): raise Exception('Bluetooth device is turned off')
                async def __aexit__(self, *_): pass
            radio.scanner_factory = Off
            await radio._cycle()
            await radio._cycle()
            events = events_of(radio)
            self.assertEqual(radio.health, 'off')
            self.assertEqual(events[0], {'event': 'bluetooth_state', 'state': 'off'})
            errors = [e for e in events if e['event'] == 'error']
            self.assertEqual(len(errors), 1)  # repeated identical errors are not spammed
            self.assertEqual(errors[0]['code'], 'bluetooth_off')
            self.assertIn('turned off', errors[0]['error'])
            radio.scanner_factory = radio_for(folder, []).scanner_factory
            await radio._cycle()
            self.assertEqual(radio.health, 'ok')
            self.assertEqual(events_of(radio)[-2:], [{'event': 'bluetooth_state', 'state': 'ok'},
                                                     {'event': 'nearby', 'meters': []}])

    async def test_worker_survives_cancellation_and_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            calls = []
            async def cycle():
                calls.append(1)
                if len(calls) == 1:
                    raise asyncio.CancelledError()
                if len(calls) == 2:
                    raise KeyError('bug')
                radio.stop.set()
            radio._cycle = cycle
            self.assertFalse(await radio._run())
            self.assertEqual(len(calls), 3)
            codes = [e.get('code') for e in events_of(radio) if e['event'] == 'error']
            self.assertEqual(codes, ['worker_restarted'])  # deduplicated
            self.assertNotIn('exit', codes)

    async def connected_session(self, radio, *, cycles=1):
        client = FakeClient()
        session = Session(client, HOST, 'Test PC', radio.events.put)
        await session.subscribe()
        status = {'protocol': 4, 'firmware': '2026.9.14'}
        async def read(_):
            return json.dumps(status).encode()
        loops = []
        async def stop(_seconds):
            loops.append(1)
            if len(loops) >= cycles:
                client.is_connected = False
        client.read_gatt_char, radio._sleep = read, stop
        return client, session, status

    async def test_old_notices_are_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio.update_notice(2)
            radio.notices.put((3, time.monotonic() - 61))  # queued while disconnected
            client, session, status = await self.connected_session(radio)
            async def clock(_self):
                pass
            with patch.object(Session, 'clock', clock):
                await radio._connected(session, 'meter', status, 'address:meter')
            self.assertEqual([p for c, p in client.writes if p[:1] == b'u'], [b'u\x02'])
            self.assertTrue(radio.notices.empty())
        with self.assertRaises(ValueError):
            radio.update_notice(6)

    async def run_job(self, radio, address='meter'):
        started = []
        class Transfer:
            commit_started = False
            def __init__(self, client, emit):
                pass
            async def run(self, **job):
                started.append(job)
        client, session, status = await self.connected_session(radio)
        async def clock(_self):
            pass
        with patch('meter.ota.OTATransfer', Transfer), patch.object(Session, 'clock', clock):
            await radio._connected(session, address, status, 'address:' + address)
        return started

    async def test_firmware_job_runs_only_on_its_meter(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            job = dict(image_path='image.bin', envelope=b'env', companion_version='2026.9.15', usb_power=True)
            radio.install_firmware(device_id='meter', **job)
            self.assertEqual(await self.run_job(radio), [job])  # device_id is not passed to the transfer
            self.assertIn({'event': 'ota_rebooting'}, events_of(radio))
            # Another meter connecting first never receives it.
            radio.install_firmware(device_id='meter', **job)
            self.assertEqual(await self.run_job(radio, 'neighbor'), [])
            expired = [e for e in events_of(radio) if e.get('code') == 'job_expired']
            self.assertEqual(expired, [{'event': 'ota_error', 'code': 'job_expired',
                                        'error': 'The meter disconnected before the update started. Try again.'}])
            self.assertTrue(radio.jobs.empty())
            # Older callers without device_id keep working.
            radio.install_firmware(**job)
            self.assertEqual(await self.run_job(radio, 'anything'), [job])

    async def test_firmware_job_expires_when_not_started_or_meter_leaves(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            job = dict(image_path='image.bin', envelope=b'env', companion_version='2026.9.15', usb_power=True)
            radio.jobs.put_nowait(({**job, 'device_id': 'meter'}, time.monotonic() - 61))
            radio._expire_job()
            self.assertTrue(radio.jobs.empty())
            self.assertEqual(events_of(radio)[-1]['code'], 'job_expired')
            radio.install_firmware(device_id='meter', **job)
            radio._expire_job()
            self.assertFalse(radio.jobs.empty())  # still fresh
            radio._drop_job('neighbor')
            self.assertFalse(radio.jobs.empty())  # someone else's disconnect
            radio._drop_job('meter')
            self.assertTrue(radio.jobs.empty())
            self.assertEqual(events_of(radio)[-1]['code'], 'job_expired')

    async def test_forget_clears_pairings_and_announces(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio.store.promote('serial:' + SERIAL, 'meter', b'\x09' * 32)
            radio.pinned = 'meter'
            radio.forget()
            self.assertIsNone(radio.pinned)
            self.assertIsNone(radio.store.secret('serial:' + SERIAL))
            self.assertEqual(events_of(radio)[-1], {'event': 'forgotten'})

    async def test_first_binding_instruction_without_connecting(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()  # pairing firmware, menu closed
            radio = radio_for(folder, [meter])
            delays = []
            async def sleep(seconds):
                delays.append(seconds)
                if len(delays) == 2:
                    radio.stop.set()
            radio._sleep = sleep
            await radio._run()
            kinds = [e['event'] for e in events_of(radio)]
            self.assertEqual(kinds, ['bluetooth_state', 'nearby', 'selection_required'])
            self.assertEqual(meter.connections, 0)
            self.assertLessEqual(delays[-1], 5)


class AdvertisementTests(unittest.IsolatedAsyncioTestCase):
    def test_marker_menu_flag_and_name_fallbacks(self):
        ns = SimpleNamespace
        self.assertEqual(advertised_kind(ns(local_name='Sweetmeter-ABCD', manufacturer_data={0xFFFF: b'SM\x03'})), 'menu')
        self.assertEqual(advertised_kind(ns(local_name='Sweetmeter-ABCD', manufacturer_data={0xFFFF: b'SM\x01'})), 'closed')
        self.assertEqual(advertised_kind(ns(local_name=None, manufacturer_data={0xFFFF: b'SM\x01'})), 'closed')
        # Only the name arrived: the -PAIR suffix still reveals an open menu.
        self.assertEqual(advertised_kind(ns(local_name='Sweetmeter-ABCD-PAIR', manufacturer_data={})), 'menu')
        # A name without any marker is pre-secret firmware.
        self.assertEqual(advertised_kind(ns(local_name='Sweetmeter-ABCD', manufacturer_data={})), 'legacy')
        self.assertEqual(advertised_kind(ns(local_name='Sweetmeter-ABCD', manufacturer_data={0xFFFF: b'XY\x01'})), 'legacy')
        self.assertEqual(advertised_kind(ns(local_name='Sweetmeter-ABCD')), 'legacy')
        # Neither yet (scan response pending): undecided.
        self.assertIsNone(advertised_kind(ns(local_name=None, manufacturer_data={})))

    async def test_scan_merges_the_separate_scan_response(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            class Scanner:
                def __init__(self, detection_callback, **_):
                    self.found = detection_callback
                async def __aenter__(self):
                    device = SimpleNamespace(address='meter')
                    self.found(device, SimpleNamespace(local_name='Sweetmeter-ABCD',
                                                       manufacturer_data={0xFFFF: b'SM\x03'}))
                    self.found(device, SimpleNamespace(local_name=None, manufacturer_data={}))  # advertisement only
                    self.found(device, SimpleNamespace(local_name=None, manufacturer_data={0xFFFF: b'SM\x01'}))
                async def __aexit__(self, *_):
                    pass
            radio.scanner_factory = Scanner
            devices = await radio._scan()
            merged = devices['meter'][1]
            self.assertEqual(merged.local_name, 'Sweetmeter-ABCD')
            self.assertEqual(advertised_kind(merged), 'closed')  # the latest flags win

    async def test_undecided_advertisement_is_not_contacted(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter], marker=False, name=False)
            await radio._cycle()
            self.assertEqual(meter.connections, 0)
            self.assertNotIn('selection_required', [e['event'] for e in events_of(radio)])

    async def test_fresh_companion_pairs_and_updates_an_unpaired_legacy_meter(self):
        """A factory-fresh 2026.9.13 meter paired to nobody (no marker, no -PAIR)."""
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(legacy_status=True)
            radio = radio_for(folder, [meter])
            await radio._cycle()
            self.assertEqual(meter.connections, 1)  # legacy firmware is probed as before
            events = events_of(radio)
            self.assertEqual(events[-1]['reason'], 'unpaired')
            # The owner opens the legacy menu (no hint in its advertisement).
            meter.menu_nonce = 21
            radio._not_before.clear()
            await radio._cycle()
            self.assertIsNone(meter.candidates[HOST])  # legacy body, no secret
            meter.select(HOST)
            radio._not_before.clear()
            await radio._cycle()
            self.assertIn('connected', [e['event'] for e in events_of(radio)])
            self.assertTrue(radio.store.legacy_record('meter'))
            # The OTA installs pairing firmware; the stored selection has no secret yet.
            meter.legacy_status = False
            meter.paired[HOST] = None
            radio._not_before.clear()
            await radio._cycle()  # marker, menu closed, but related: H then Y with a fresh secret
            self.assertEqual(meter.paired[HOST], radio.store.secret('serial:' + SERIAL))
            self.assertIsNotNone(meter.paired[HOST])
            self.assertIn('connected', [e['event'] for e in events_of(radio)])


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeClient()
        self.events = []
        self.session = Session(self.client, HOST, 'Test PC', self.events.append)
        await self.session.subscribe()
    async def test_minimum_mtu_registration_exact_body(self):
        await self.session.register(123)
        self.assertEqual(bytes(self.client.body), HOST.encode()+b'\x07Test PC')
        self.assertTrue(all(len(packet)<=20 for _, packet in self.client.writes))
    async def test_registration_carries_the_pairing_secret(self):
        secret = bytes(range(32))
        await self.session.register(123, secret)
        self.assertEqual(bytes(self.client.body), HOST.encode()+b'\x07Test PC'+secret)
        self.assertEqual(self.client.total, 36 + 1 + 7 + 32)
        with self.assertRaises(ValueError):
            await self.session.register(1, b'short')
    async def test_rejected_registration_never_commits(self):
        self.client.result = 2
        with self.assertRaises(RuntimeError): await self.session.register(123)
        self.assertFalse(any(p[:1] == b'K' for _,p in self.client.writes))
    async def test_authenticated_hello_sends_only_a_proof(self):
        secret = b'\x11' * 32
        await self.session.authenticate(secret, 'cd' * 16, SERIAL)
        packet = self.client.writes[-1][1]
        self.assertEqual(packet, b'P' + pairing_proof(secret, 'cd' * 16, SERIAL, HOST))
        self.assertEqual(len(packet), 17)
        self.assertNotIn(HOST.encode(), packet)
        self.client.hello_result = 9
        with self.assertRaises(PairingRejected) as caught:
            await self.session.authenticate(secret, 'cd' * 16, SERIAL)
        self.assertTrue(caught.exception.paired)
        self.client.hello_result = 7
        with self.assertRaises(PairingRejected) as caught:
            await self.session.authenticate(secret, 'cd' * 16, SERIAL)
        self.assertFalse(caught.exception.paired)
    async def test_provision_after_legacy_hello(self):
        self.client.hello_result = 8
        self.assertEqual(await self.session.hello(), 8)
        self.client.hello_result = 0
        await self.session.provision(b'\x22' * 32)
        self.assertEqual(self.client.writes[-1][1], b'Y' + b'\x22' * 32)
    async def test_rocker_hold_requests_update_and_receives_result(self):
        self.client.callback(None, b'U')
        self.assertEqual(self.events[-1], {'event': 'update_request'})
        await self.session.update_notice(2)
        self.assertEqual(self.client.writes[-1], (CONTROL_UUID, b'u\x02'))
    async def test_frame_requires_matching_application_ack(self):
        data = bytes(range(250))*16
        await self.session.frame(data)
        self.assertEqual(bytes(self.client.frame), data)
        self.assertEqual(self.events[-1]['ack'], 'FULL')
        self.assertTrue(self.events[-1]['displayed'])
        self.assertTrue(all(len(p)<=20 for c,p in self.client.writes if c==DATA_UUID))
    async def test_deferred_frame_ack_is_success(self):
        self.client.result = 1
        await self.session.frame(b'd' * 4000)
        self.assertEqual(self.events[-1]['ack'], 'QUEUED')
        self.assertFalse(self.events[-1]['displayed'])
    async def test_busy_panel_delays_begin_and_no_data_precedes_ready(self):
        self.client.auto_begin = False
        frame = b'q' * 4000
        task = asyncio.create_task(self.session.frame(frame))
        await self.client.begin_written.wait()
        await asyncio.sleep(.01)
        self.assertFalse(self.client.frame)
        self.assertFalse(any(p[:1] == b'C' for c,p in self.client.writes if c==CONTROL_UUID))
        self.assertFalse(task.done())
        # Neither the wrong sequence nor CRC may release the receive gate.
        self.client.callback(None, struct.pack('<cBII', b'b', 0, self.client.sequence+1, self.client.crc))
        self.client.callback(None, struct.pack('<cBII', b'b', 0, self.client.sequence, self.client.crc ^ 1))
        await asyncio.sleep(.01)
        self.assertFalse(self.client.frame)
        self.client.callback(None, struct.pack('<cBII', b'b', 0, self.client.sequence, self.client.crc))
        await task
        self.assertEqual(bytes(self.client.frame), frame)
        self.assertEqual(self.events[-1]['ack'], 'FULL')
    async def test_negative_begin_ack_sends_no_payload_or_commit(self):
        for result in (2, 6, 7):
            with self.subTest(result=result):
                self.client.begin_result = result
                self.client.writes.clear()
                with self.assertRaisesRegex(RuntimeError, 'dashboard begin'):
                    await self.session.frame(b'q' * 4000)
                self.assertEqual(len(self.client.writes), 1)
                self.assertFalse(self.client.frame)
                self.assertFalse(self.events)
    async def test_begin_timeout_sends_no_payload_or_commit(self):
        self.client.auto_begin = False
        with patch('meter.bluetooth.FRAME_ACK_TIMEOUT', .01):
            with self.assertRaises(TimeoutError):
                await self.session.frame(b'q' * 4000)
        self.assertEqual(len(self.client.writes), 1)
        self.assertFalse(self.client.frame)
        self.assertFalse(self.events)
    async def test_protocol_three_does_not_wait_for_new_begin_ack(self):
        self.client.auto_begin = False
        self.session.protocol = 3
        await self.session.frame(b'q' * 4000)
        self.assertEqual(bytes(self.client.frame), b'q' * 4000)
        self.assertEqual(self.events[-1]['ack'], 'FULL')
    async def test_display_failure_is_not_success(self):
        self.client.result = 5
        with self.assertRaises(RuntimeError): await self.session.frame(b'0'*4000)
        self.assertFalse(self.events)
    async def test_physical_discovery_disconnects_immediately(self):
        self.session.notification(None, struct.pack('<cII', b'D', 4, 60000))
        self.assertEqual(len(self.session.tasks), 1)  # the disconnect task is retained
        await asyncio.sleep(0)
        self.assertFalse(self.client.is_connected)
        with self.assertRaises(DiscoveryOpened): await self.session.clock()
    async def test_nonselected_hello_refused_without_permission_error(self):
        self.client.hello_result = 7
        with self.assertRaises(PairingRejected) as caught:
            await self.session.hello()
        self.assertNotIsInstance(caught.exception, PermissionError)


KEY = 'serial:' + SERIAL


class BoundPairingTests(unittest.IsolatedAsyncioTestCase):
    """A confirmed pairing is bound to (serial, address the secret was proven at)."""
    async def visit(self, radio, meter, hinted=False):
        return await radio._visit(SimpleNamespace(address=meter.address), hinted)

    async def pair(self, radio, meter):
        meter.menu_nonce = 41
        await self.visit(radio, meter, hinted=True)
        meter.select(HOST)
        self.assertEqual(await self.visit(radio, meter), 0)
        events_of(radio)
        return radio.store.secret(KEY)

    async def test_rogue_with_copied_serial_cannot_demote_or_take_over_the_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            real, rogue = FakeMeter('real'), FakeMeter('rogue', rogue='reject')
            radio = radio_for(folder, [real, rogue])
            confirmed = await self.pair(radio, real)
            # 1. The rogue copies the serial, claims our selection and refuses our hello.
            rogue.selected, rogue.paired[HOST] = HOST, b'\x01' * 32
            await self.visit(radio, rogue)
            self.assertFalse(radio.store.needs_registration(KEY))
            self.assertTrue(radio.store.trusted(KEY, 'real'))
            # 2. It opens a "menu": this computer is still paired, so it registers nothing.
            rogue.selected, rogue.menu_nonce = None, 7
            await self.visit(radio, rogue, hinted=True)
            self.assertNotIn(HOST, getattr(rogue, 'candidates', {}))
            self.assertIsNone(radio.store.pending(KEY, 'rogue'))
            # 3. It claims the selection and accepts any proof. Even with a pending
            # secret for its address (as an earlier companion registered there),
            # it is never driven and nothing moves.
            stale = radio.store.new_pending(KEY, 'rogue')
            rogue.rogue, rogue.menu_nonce, rogue.selected = 'accept', 0, HOST
            delay = await self.visit(radio, rogue)
            events = events_of(radio)
            self.assertNotIn('connected', [e['event'] for e in events])
            self.assertIn('meter_conflict', [e.get('code') for e in events])
            self.assertGreaterEqual(delay, 30)
            self.assertFalse(getattr(rogue, 'clock_writes', 0))  # no clock, frame or firmware
            self.assertEqual(radio.store.secret(KEY), confirmed)
            self.assertEqual(radio.store.bound_address(KEY), 'real')
            self.assertEqual(radio.store.pending(KEY, 'rogue'), stale)
            self.assertTrue(radio.store.related('real'))
            # The real meter is still ours.
            self.assertEqual(await self.visit(radio, real), 0)
            self.assertIn('connected', [e['event'] for e in events_of(radio)])

    async def test_pending_from_another_address_waits_until_the_bound_meter_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            real, other = FakeMeter('real'), FakeMeter('other')
            radio = radio_for(folder, [real, other])
            confirmed = await self.pair(radio, real)
            # A pending secret for another address (sent to whoever answered there).
            pending = radio.store.new_pending(KEY, 'other')
            self.assertEqual(radio.store.bound_address(KEY), 'real')  # a registration never moves it
            other.paired[HOST], other.selected = pending, HOST
            await self.visit(radio, other)
            self.assertEqual(radio.store.secret(KEY), confirmed)
            self.assertEqual(radio.store.pending(KEY, 'other'), pending)  # kept per address
            self.assertFalse(radio.store.promote(KEY, 'other', pending))
            # The meter at the bound address forgets us (refuses the confirmed secret).
            real.paired.clear()
            real.paired[OTHER], real.selected = b'\x05' * 32, OTHER
            await self.visit(radio, real)
            self.assertTrue(radio.store.refused(KEY))
            # Only now may the pending secret proven at the other address replace it.
            self.assertEqual(await self.visit(radio, other), 0)
            self.assertEqual(radio.store.secret(KEY), pending)
            self.assertEqual(radio.store.bound_address(KEY), 'other')

    async def test_mutual_proof_exposes_a_rogue_and_follows_a_real_address_change(self):
        with tempfile.TemporaryDirectory() as folder:
            real = FakeMeter('real', mutual=True)
            rogue = FakeMeter('rogue', mutual=True, rogue='accept')
            moved = FakeMeter('moved', mutual=True)
            radio = radio_for(folder, [real, rogue, moved])
            confirmed = await self.pair(radio, real)
            self.assertTrue(radio.store.requires_mutual(KEY))
            self.assertIn(b'N', [w[:1] for w in real.writes])
            # A rogue that accepts any proof cannot produce the meter proof (it
            # holds a pending secret an earlier companion registered with it).
            radio.store.new_pending(KEY, 'rogue')
            rogue.selected, rogue.paired[HOST] = HOST, b'\x01' * 32
            await self.visit(radio, rogue)
            self.assertIn('meter_conflict', [e.get('code') for e in events_of(radio)])
            self.assertFalse(getattr(rogue, 'clock_writes', 0))
            # Nor can it downgrade by hiding mutual support.
            rogue.mutual = False
            radio._last_error = (None, 0)
            await self.visit(radio, rogue)
            self.assertIn('meter_conflict', [e.get('code') for e in events_of(radio)])
            self.assertEqual(rogue.connections, 2)
            self.assertEqual((radio.store.secret(KEY), radio.store.bound_address(KEY)), (confirmed, 'real'))
            # Our meter at a new address (the OS reset its identifiers) proves the
            # confirmed secret itself in its open menu, so the pairing follows it.
            moved.paired[HOST], moved.menu_nonce = confirmed, 70
            await self.visit(radio, moved, hinted=True)
            self.assertNotIn(HOST, getattr(moved, 'candidates', {}))
            self.assertEqual(radio.store.bound_address(KEY), 'moved')
            moved.select(HOST)
            self.assertEqual(await self.visit(radio, moved), 0)
            self.assertEqual(radio.store.bound_address(KEY), 'moved')
            self.assertEqual(radio.store.secret(KEY), confirmed)

    async def test_nothing_selected_keeps_a_pairing_the_meter_still_knows(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            await self.pair(radio, meter)
            meter.selected = None  # the owner removed the selected computer's selection
            delay = await self.visit(radio, meter)
            self.assertEqual(events_of(radio)[-1]['reason'], 'unpaired')
            self.assertLessEqual(delay, 5)
            self.assertFalse(radio.store.needs_registration(KEY))  # no fresh secret, no NEW KEY prompt
            # Firmware without mutual authentication cannot answer membership in
            # its menu: a paired computer neither registers nor sends a hello there.
            meter.menu_nonce, meter.writes = 50, []
            await self.visit(radio, meter, hinted=True)
            self.assertEqual(meter.writes, [])
            # Forgotten while nothing is selected: this computer registers again.
            meter.paired.clear()
            meter.menu_nonce = 0
            await self.visit(radio, meter)
            self.assertTrue(radio.store.needs_registration(KEY))

    async def test_open_menu_reports_whether_this_computer_is_still_paired(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(mutual=True)
            radio = radio_for(folder, [meter])
            confirmed = await self.pair(radio, meter)
            meter.candidates = {}
            # Still paired: one membership hello (N, P), no registration.
            meter.menu_nonce, meter.writes = 60, []
            await self.visit(radio, meter, hinted=True)
            self.assertEqual([w[:1] for w in meter.writes], [b'N', b'P'])
            self.assertNotIn(HOST, meter.candidates)
            await self.visit(radio, meter, hinted=True)
            self.assertEqual(len(meter.writes), 2)  # checked once per menu window
            # Removed in the menu: the meter answers 7, and this computer reappears.
            del meter.paired[HOST]
            meter.menu_nonce = 61
            delay = await self.visit(radio, meter, hinted=True)
            self.assertLessEqual(delay, 2)
            self.assertTrue(radio.store.needs_registration(KEY))
            await self.visit(radio, meter, hinted=True)
            self.assertIn(HOST, meter.candidates)
            self.assertNotEqual(meter.candidates[HOST], confirmed)
            meter.select(HOST)
            self.assertEqual(await self.visit(radio, meter), 0)
            self.assertEqual(radio.store.secret(KEY), meter.paired[HOST])

    async def test_dropped_marker_is_reclassified_from_the_status(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter('stranger')  # pairing firmware; this scanner drops manufacturer data
            radio = radio_for(folder, [meter], marker=False)
            await radio._cycle()
            self.assertEqual(meter.connections, 1)  # looked pre-secret, so it was probed once
            self.assertEqual(meter.writes, [])       # left right after the status read
            self.assertIn('selection_required', [e['event'] for e in events_of(radio)])
            radio._not_before.clear()
            await radio._cycle()
            self.assertEqual(meter.connections, 1)  # now treated as pairing firmware, menu closed
            # Its menu opens: the name suffix still says so, and it registers.
            meter.menu_nonce = 8
            radio.scanner_factory = radio_for(folder, [meter], hints={'stranger'}, marker=False).scanner_factory
            await radio._cycle()
            self.assertIn(HOST, meter.candidates)


class RefreshAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def run_link(self, radio, script, cycles=6):
        client = FakeClient()
        session = Session(client, HOST, 'Test PC', radio.events.put)
        await session.subscribe()
        status = {'protocol': 4, 'firmware': '2026.9.14'}
        async def read(_):
            return json.dumps(status).encode()
        client.read_gatt_char = read
        steps = []
        async def step(_seconds):
            steps.append(1)
            action = script.get(len(steps))
            if action:
                action(client)
            if len(steps) >= cycles:
                client.is_connected = False
        radio._sleep = step
        async def clock(_self):
            pass
        with patch.object(Session, 'clock', clock):
            await radio._connected(session, 'meter', status, 'address:meter')
        return [p for c, p in client.writes if c == CONTROL_UUID and p[:1] == b'B']

    async def test_refresh_press_gets_one_frame_even_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            frame = b'\x0f' * 4000
            radio.send(frame)
            begins = await self.run_link(radio, {
                1: lambda client: client.callback(None, b'R'),  # the meter asks; the app forces a refresh
                3: lambda client: radio.send(frame),             # ...which renders the same frame
                4: lambda client: radio.send(frame),             # later identical frames are not resent
            })
            self.assertEqual(len(begins), 2)
            self.assertIn({'event': 'refresh'}, events_of(radio))

    async def test_identical_frames_without_a_press_are_not_resent(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            frame = b'\x0f' * 4000
            radio.send(frame)
            begins = await self.run_link(radio, {2: lambda client: radio.send(frame)})
            self.assertEqual(len(begins), 1)
            # A press whose refresh never produces a frame sends nothing (the meter's
            # 10-second marker acknowledges it).
            radio2 = radio_for(folder, [])
            radio2.send(frame)
            begins = await self.run_link(radio2, {1: lambda client: client.callback(None, b'R')})
            self.assertEqual(len(begins), 1)


class JobLockTests(unittest.TestCase):
    def test_install_during_expiry_check_never_reports_the_wrong_job_expired(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'companion.json').write_text(json.dumps({'host_id': HOST}))
            radio = Bluetooth(folder, start=False)
            first = dict(image_path='a.bin', envelope=b'env', companion_version='2026.9.15', usb_power=True)
            radio.install_firmware(device_id='meter', **first)
            taken, release = threading.Event(), threading.Event()
            original = radio.jobs.get_nowait
            def slow_get():
                item = original()
                taken.set()
                release.wait(2)  # the job is out of the queue for a moment
                return item
            radio.jobs.get_nowait = slow_get
            worker = threading.Thread(target=radio._expire_job)
            worker.start()
            self.assertTrue(taken.wait(2))
            outcome = []
            def install():
                try:
                    radio.install_firmware(device_id='meter', image_path='b.bin')
                    outcome.append('queued')
                except queue.Full:
                    outcome.append('full')
            second = threading.Thread(target=install)
            second.start()
            second.join(.2)
            self.assertTrue(second.is_alive())  # waits for the expiry check instead of racing it
            release.set()
            worker.join(2)
            second.join(2)
            radio.jobs.get_nowait = original
            self.assertEqual(outcome, ['full'])  # the fresh first job is still queued
            self.assertNotIn(JOB_EXPIRED, events_of(radio))
            self.assertEqual(radio.jobs.get_nowait()[0]['image_path'], 'a.bin')


class MutualStatusTests(unittest.TestCase):
    def test_rename_flag_is_strict(self):
        base = {'protocol': 4, 'firmware': '2026.9.19', 'auth': 1, 'serial': SERIAL, 'selected': False,
                'secured': False, 'challenge': '00' * 16}
        self.assertEqual(parse_status(json.dumps({**base, 'rename': 1}).encode())['rename'], 1)
        self.assertNotIn('rename', parse_status(json.dumps(base).encode()))  # optional: dropped if it would not fit
        for bad in (0, 2, True, '1'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_status(json.dumps({**base, 'rename': bad}).encode())
        legacy = {'protocol': 4, 'firmware': '2026.9.13', 'selected_host': '', 'rename': 1}
        with self.assertRaises(ValueError):
            parse_status(json.dumps(legacy).encode())

    def test_mutual_flag_is_strict(self):
        good = {'protocol': 4, 'firmware': '2026.9.14', 'auth': 1, 'serial': SERIAL, 'selected': True,
                'secured': True, 'challenge': 'ab' * 16, 'mutual': 1}
        self.assertEqual(parse_status(json.dumps(good).encode())['mutual'], 1)
        for change in ({'mutual': True}, {'mutual': 2}, {'mutual': '1'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_status(json.dumps({**good, **change}).encode())
        legacy = {'protocol': 4, 'firmware': '2026.9.13', 'selected_host': '', 'mutual': 1}
        with self.assertRaises(ValueError):
            parse_status(json.dumps(legacy).encode())

    def test_meter_proof_matches_firmware_vector(self):
        # Same vector as tests/test_pairing.cpp (mutualAuthentication).
        proof = meter_proof(bytes(range(1, 33)), bytes(range(0xa0, 0xb0)).hex(), bytes(range(0x10, 0x20)),
                            SERIAL, HOST)
        self.assertEqual(proof.hex(), '8b2739822a4b9a04626344b72fad7e91')


if __name__ == '__main__':
    unittest.main()


class SetupEventTests(unittest.IsolatedAsyncioTestCase):
    """Events the setup window relies on: nearby meters, registration results, naming."""
    async def visit(self, radio, meter, hinted=False):
        return await radio._visit(SimpleNamespace(address=meter.address), hinted)

    async def test_nearby_reports_every_meter_without_connecting(self):
        with tempfile.TemporaryDirectory() as folder:
            mine, other = FakeMeter('mine'), FakeMeter('other', serial='a1b2c3d4e5f7')
            other.label = 'Desk'.encode()
            radio = radio_for(folder, [mine, other], hints={'other'})
            confirm(radio, mine)
            radio._visit = lambda *_: asyncio.sleep(0, 0)  # only the scan is under test
            await radio._cycle()
            nearby = [e for e in events_of(radio) if e['event'] == 'nearby'][0]['meters']
            by_address = {m['address']: m for m in nearby}
            self.assertEqual(by_address['mine'], {'address': 'mine', 'name': 'Sweetmeter-ABCD', 'rssi': -58,
                                                  'kind': 'closed', 'paired': True})
            # The open-menu suffix is not part of the name.
            self.assertEqual(by_address['other']['name'], 'Desk')
            self.assertEqual(by_address['other']['kind'], 'menu')
            self.assertFalse(by_address['other']['paired'])

    async def test_registration_results_are_explained(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            meter.menu_nonce = 9
            client = FakeClient()
            client.result = 5
            async def reject(_nonce, _secret=None):
                from meter.bluetooth import RegistrationRejected
                raise RegistrationRejected(5, 0)
            with patch.object(Session, 'register', side_effect=reject):
                delay = await self.visit(radio, meter, hinted=True)
            failed = [e for e in events_of(radio) if e['event'] == 'registration_failed']
            self.assertEqual(failed[0]['result'], 5)
            self.assertIn('list is full', failed[0]['error'])
            self.assertGreaterEqual(delay, 8)  # not hammered every few seconds
            async def transient(_nonce, _secret=None):
                from meter.bluetooth import RegistrationRejected
                raise RegistrationRejected(4, 3)
            radio.registered.clear()
            with patch.object(Session, 'register', side_effect=transient):
                await self.visit(radio, meter, hinted=True)
            events = events_of(radio)
            self.assertFalse([e for e in events if e['event'] == 'registration_failed'])
            self.assertEqual([e['code'] for e in events if e['event'] == 'error'], ['device_error'])

    async def test_connected_event_names_the_meter(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(serial='d405927bcf24')
            radio = radio_for(folder, [meter])
            confirm(radio, meter)
            meter.selected = HOST
            await self.visit(radio, meter)
            connected = [e for e in events_of(radio) if e['event'] == 'connected'][0]
            self.assertEqual(connected['name'], 'Sweetmeter-CF24')  # default from the serial before any scan
            radio.names[meter.address] = 'Kitchen'
            await self.visit(radio, meter)
            connected = [e for e in events_of(radio) if e['event'] == 'connected'][0]
            self.assertEqual(connected['name'], 'Kitchen')

    async def test_rename_is_sent_only_to_capable_firmware_and_reports_the_result(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(serial='d405927bcf24')
            radio = radio_for(folder, [meter])
            confirm(radio, meter)
            meter.selected = HOST
            name = '客厅'.encode()
            radio.rename(name)
            await self.visit(radio, meter)  # earlier firmware: no "rename" in its status
            renamed = [e for e in events_of(radio) if e['event'] == 'renamed']
            self.assertFalse(renamed[0]['ok'])
            self.assertIn('firmware update', renamed[0]['error'])
            self.assertFalse([w for w in meter.writes if w[:1] == b'L'])  # never sent an unknown opcode
            meter.rename = True
            radio.rename(name)
            await self.visit(radio, meter)
            self.assertIn(b'L' + bytes([len(name)]) + name, meter.writes)
            renamed = [e for e in events_of(radio) if e['event'] == 'renamed']
            self.assertEqual(renamed, [{'event': 'renamed', 'device_id': meter.address, 'ok': True, 'name': '客厅'}])
            self.assertEqual(radio.names[meter.address], '客厅')
            # Restoring the default sends an empty name and reports the default.
            radio.rename(b'')
            await self.visit(radio, meter)
            self.assertIn(b'L\x00', meter.writes)
            renamed = [e for e in events_of(radio) if e['event'] == 'renamed']
            self.assertEqual(renamed[0]['name'], 'Sweetmeter-CF24')
            meter.rename_result = 5
            radio.rename(b'Desk')
            await self.visit(radio, meter)
            renamed = [e for e in events_of(radio) if e['event'] == 'renamed']
            self.assertFalse(renamed[0]['ok'])
            self.assertIn('could not save', renamed[0]['error'])

    async def test_rename_without_a_connected_meter_expires(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio.rename(b'Desk')
            with patch('meter.bluetooth.time.monotonic', return_value=time.monotonic() + RENAME_TIMEOUT + 1):
                await radio._cycle()
            renamed = [e for e in events_of(radio) if e['event'] == 'renamed']
            self.assertEqual(len(renamed), 1)
            self.assertFalse(renamed[0]['ok'])
            with self.assertRaises(ValueError):
                radio.rename(b'x' * 17)
            with self.assertRaises(ValueError):
                radio.rename('Desk')  # text must be encoded by naming.encode_meter_name first

    async def test_rename_packet_layout(self):
        client = FakeClient()
        session = Session(client, HOST, 'Mac', lambda _event: None)
        await session.subscribe()
        async def write(characteristic, data, response):
            client.writes.append((characteristic, bytes(data)))
            client.callback(None, b'L\x00')
        client.write_gatt_char = write
        self.assertEqual(await session.rename('书房'.encode()), 0)
        self.assertEqual(client.writes[-1], (CONTROL_UUID, b'L\x06' + '书房'.encode()))
        with self.assertRaises(ValueError):
            await session.rename(b'x' * 17)


class HurryTests(unittest.IsolatedAsyncioTestCase):
    async def test_hurry_restarts_the_idle_backoff_but_keeps_registrations(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            gaps = []
            async def record(seconds):
                gaps.append(seconds)
            radio._sleep = record
            for _ in range(3):
                await radio._cycle()
            radio.registered['meter'] = 7
            radio._not_before['meter'] = time.monotonic() + 60
            radio.hurry()
            gaps.clear()
            await radio._cycle()
            self.assertLess([g for g in gaps if g != 3][0], 6)  # back to the first idle gap
            self.assertEqual(radio.registered, {'meter': 7})  # an open menu is not registered twice
            self.assertIn('meter', radio._not_before)


class PairingPromptAndStaleKeyTests(unittest.IsolatedAsyncioTestCase):
    def test_stale_os_pairing_is_recognized(self):
        from meter.bluetooth import classify_error
        mac = Exception('failed to connect: Error Domain=CBErrorDomain Code=14 "Peer removed pairing information" '
                        'UserInfo={NSLocalizedDescription=Peer removed pairing information}')
        self.assertEqual(classify_error(mac), 'stale_pairing')
        # BlueZ uses AuthenticationFailed for a cancelled first pairing too: not called stale.
        self.assertEqual(classify_error(Exception('org.bluez.Error.AuthenticationFailed')), 'connection_failed')
        self.assertEqual(classify_error(Exception('disconnect failed: Error Domain=CBErrorDomain Code=7 '
                                                  '"The specified device has disconnected from us."')),
                         'connection_failed')

    def test_stale_pairing_message_names_the_os_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            for platform, words in (('darwin', 'Forget This Device'), ('win32', 'Remove device'),
                                    ('linux', 'bluetoothctl remove')):
                radio._last_error = (None, 0.0)
                with patch('meter.bluetooth.sys.platform', platform):
                    radio._error('stale_pairing', Exception('Peer removed pairing information'))
                event = events_of(radio)[-1]
                self.assertEqual(event['code'], 'stale_pairing')
                self.assertIn(words, event['error'])

    async def test_a_slow_first_read_reports_the_os_pairing_prompt(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            class Slow:
                async def read_gatt_char(self, _uuid):
                    await asyncio.sleep(.3)
                    return b'{}'
            with patch('meter.bluetooth.PAIRING_PROMPT_AFTER', .05):
                self.assertEqual(await radio._first_status(Slow()), b'{}')
            self.assertEqual([e['event'] for e in events_of(radio)], ['os_pairing_prompt', 'os_pairing_done'])
            class Fast:
                async def read_gatt_char(self, _uuid):
                    return b'{}'
            self.assertEqual(await radio._first_status(Fast()), b'{}')
            self.assertEqual(events_of(radio), [])  # an existing pairing: no prompt, nothing to say
            class Refused:
                async def read_gatt_char(self, _uuid):
                    await asyncio.sleep(.2)
                    raise OSError('Encryption is insufficient')
            with patch('meter.bluetooth.PAIRING_PROMPT_AFTER', .05), self.assertRaises(OSError):
                await radio._first_status(Refused())
            self.assertEqual([e['event'] for e in events_of(radio)], ['os_pairing_prompt', 'os_pairing_done'])

    async def test_the_first_read_outlasts_the_backends_own_read_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            class PairsLate:
                """bleak's macOS read times out (20 s there) before the user clicks Connect."""
                is_connected = True
                reads = 0
                async def read_gatt_char(self, _uuid):
                    self.reads += 1
                    await asyncio.sleep(.1)
                    if self.reads < 3:
                        raise asyncio.TimeoutError()
                    return b'{}'
            client = PairsLate()
            with patch('meter.bluetooth.PAIRING_PROMPT_AFTER', .05):
                self.assertEqual(await radio._first_status(client), b'{}')
            self.assertEqual(client.reads, 3)
            self.assertEqual([e['event'] for e in events_of(radio)], ['os_pairing_prompt', 'os_pairing_done'])
            class Gone(PairsLate):
                is_connected = False  # the meter dropped the link: do not read again
            with patch('meter.bluetooth.PAIRING_PROMPT_AFTER', .05), self.assertRaises(asyncio.TimeoutError):
                await radio._first_status(Gone())
            with patch('meter.bluetooth.PAIRING_PROMPT_AFTER', .05), patch('meter.bluetooth.FIRST_STATUS_TIMEOUT', .3), \
                    self.assertRaises(asyncio.TimeoutError):
                class Never(PairsLate):
                    async def read_gatt_char(self, _uuid):
                        await asyncio.sleep(10)
                await radio._first_status(Never())


class ReviewRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def visit(self, radio, meter, hinted=False):
        return await radio._visit(SimpleNamespace(address=meter.address), hinted)

    async def test_search_again_never_registers_twice_in_one_menu_opening(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            meter.menu_nonce = 41
            await self.visit(radio, meter, hinted=True)
            first = meter.candidates[HOST]
            radio.rescan()  # the Search again button
            await radio._cycle()
            self.assertEqual(meter.candidates[HOST], first)  # a second secret would be a CONFLICT row
            self.assertEqual(sum(1 for w in meter.writes if w[:1] == b'K'), 1)

    async def test_a_rename_that_fails_in_flight_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(serial='d405927bcf24')
            meter.rename = True
            radio = radio_for(folder, [meter])
            confirm(radio, meter)
            meter.selected = HOST
            radio.rename(b'Desk')
            async def lost(_name):
                raise DiscoveryOpened()
            with patch.object(Session, 'rename', side_effect=lost):
                await self.visit(radio, meter)
            renamed = [e for e in events_of(radio) if e['event'] == 'renamed']
            self.assertEqual(len(renamed), 1)
            self.assertFalse(renamed[0]['ok'])
            self.assertIn('did not confirm', renamed[0]['error'])

    async def test_nearby_calls_only_a_confirmed_pairing_paired(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            radio.store.new_pending('serial:' + SERIAL, meter.address)  # registered, not chosen yet
            radio._visit = lambda *_: asyncio.sleep(0, 0)
            await radio._cycle()
            nearby = [e for e in events_of(radio) if e['event'] == 'nearby'][0]['meters']
            self.assertFalse(nearby[0]['paired'])
            confirm(radio, meter)
            radio.scanner_factory = radio_for(folder, [meter]).scanner_factory  # a fresh report
            await radio._cycle()
            nearby = [e for e in events_of(radio) if e['event'] == 'nearby'][0]['meters']
            self.assertTrue(nearby[0]['paired'])

    async def test_an_unanswered_prompt_on_a_strangers_open_menu_is_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            stranger = FakeMeter('stranger')
            stranger.menu_nonce = 5
            radio = radio_for(folder, [stranger], hints={'stranger'})
            confirm(radio, FakeMeter('mine', serial='a1b2c3d4e5f7'))  # this computer already has its meter
            async def unanswered(self, client, *, patient=True):
                assert patient is False  # not pairing with this one: normal read timeout
                raise asyncio.TimeoutError()
            with patch.object(Bluetooth, '_first_status', unanswered):
                await radio._cycle()
                connections = stranger.connections
                await radio._cycle()  # its menu is still open, but it is held
            self.assertEqual(stranger.connections, connections)

    async def test_a_refused_registration_is_not_retried_at_once_in_an_open_menu(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            meter.menu_nonce = 6
            radio = radio_for(folder, [meter], hints={'meter'})
            async def full(_nonce, _secret=None):
                from meter.bluetooth import RegistrationRejected
                raise RegistrationRejected(5, 0)
            with patch.object(Session, 'register', side_effect=full):
                await radio._cycle()
                await radio._cycle()
            self.assertEqual(len([e for e in events_of(radio) if e['event'] == 'registration_failed']), 1)

    def test_hurry_does_not_cut_a_scan_short(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio._scanning = True
            radio.hurry()
            self.assertFalse(radio._wake.is_set())
            self.assertTrue(radio._hurry_requested)
            radio._scanning = False
            radio.hurry()
            self.assertTrue(radio._wake.is_set())

    async def test_first_read_waits_long_only_while_pairing(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            waits = []
            real_wait_for = asyncio.wait_for
            async def record(awaitable, timeout):
                waits.append(timeout)
                return await real_wait_for(awaitable, timeout)
            class Quick:
                is_connected = True
                async def read_gatt_char(self, _uuid):
                    await asyncio.sleep(.1)
                    return b'{}'
            with patch('meter.bluetooth.PAIRING_PROMPT_AFTER', .01), patch('meter.bluetooth.asyncio.wait_for', record):
                await radio._first_status(Quick(), patient=False)
                await radio._first_status(Quick(), patient=True)
            self.assertLessEqual(waits[0], 10)
            self.assertGreater(waits[1], 30)


class PersistentScannerTests(unittest.IsolatedAsyncioTestCase):
    """The scanner keeps running between scans and stops only to connect."""
    def scanner(self, log):
        class Scanner:
            instances = []
            def __init__(self, detection_callback, **_):
                self.found = detection_callback
                Scanner.instances.append(self)
            async def __aenter__(self):
                log.append('start')
                return self
            async def __aexit__(self, *_):
                log.append('stop')
        return Scanner

    async def test_scanner_runs_across_scans_and_pauses_for_a_visit(self):
        with tempfile.TemporaryDirectory() as folder:
            log = []
            radio = radio_for(folder, [])
            radio.scanner_factory = self.scanner(log)
            await radio._scan()
            await radio._scan()
            self.assertEqual(log, ['start'])  # not restarted every few seconds
            meter = SimpleNamespace(address='m')
            scanner = radio.scanner_factory.instances[0]
            scanner.found(meter, SimpleNamespace(local_name='Sweetmeter-ABCD', manufacturer_data={}, rssi=-60))
            self.assertIn('m', await radio._scan())  # reports between scans are kept
            await radio._pause_scanner()
            self.assertEqual(log, ['start', 'stop'])
            with patch('meter.bluetooth.time.monotonic', return_value=time.monotonic() + 1):
                await radio._scan()
            self.assertEqual(log, ['start', 'stop', 'start'])

    async def test_a_meter_appearing_between_scans_wakes_the_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio.scanner_factory = self.scanner([])
            await radio._scan()
            radio._wake.clear()
            scanner = radio.scanner_factory.instances[0]
            scanner.found(SimpleNamespace(address='m'), SimpleNamespace(local_name='Sweetmeter-ABCD',
                                                                        manufacturer_data={0xFFFF: b'SM\x01'}, rssi=-60))
            self.assertTrue(radio._wake.is_set())  # new meter
            await radio._scan()  # now known, menu closed
            radio._wake.clear()
            scanner.found(SimpleNamespace(address='m'), SimpleNamespace(local_name='Sweetmeter-ABCD',
                                                                        manufacturer_data={0xFFFF: b'SM\x01'}, rssi=-60))
            self.assertFalse(radio._wake.is_set())  # nothing new: keep the idle pacing
            scanner.found(SimpleNamespace(address='m'), SimpleNamespace(local_name='Sweetmeter-ABCD-PAIR',
                                                                        manufacturer_data={0xFFFF: b'SM\x03'}, rssi=-60))
            self.assertTrue(radio._wake.is_set())  # its computer list just opened
            # Also in the middle of a scan: the owner is waiting for this computer.
            await radio._scan()
            scanner.found(SimpleNamespace(address='m'), SimpleNamespace(local_name='Sweetmeter-ABCD',
                                                                        manufacturer_data={0xFFFF: b'SM\x01'}, rssi=-60))
            await radio._scan()  # its list closed again
            radio._wake.clear()
            radio._scanning = True
            scanner.found(SimpleNamespace(address='m'), SimpleNamespace(local_name='Sweetmeter-ABCD-PAIR',
                                                                        manufacturer_data={0xFFFF: b'SM\x03'}, rssi=-60))
            self.assertTrue(radio._wake.is_set())
            radio._scanning = False

    async def test_scanning_continues_while_connecting_except_on_linux(self):
        with tempfile.TemporaryDirectory() as folder:
            for platform, stops in (('darwin', False), ('win32', False), ('linux', True)):
                log = []
                radio = radio_for(folder, [])
                radio.scanner_factory = self.scanner(log)
                await radio._scan()
                with patch('meter.bluetooth.sys.platform', platform):
                    await radio._pause_scanner(connecting=True)
                # A restarted macOS scan reported a meter that had just disconnected
                # 20-40 s late; BlueZ may fail to connect while it scans.
                self.assertEqual(log, ['start', 'stop'] if stops else ['start'], platform)

    async def test_the_scanner_restarts_now_and_then_to_notice_bluetooth_problems(self):
        with tempfile.TemporaryDirectory() as folder:
            log = []
            radio = radio_for(folder, [])
            radio.scanner_factory = self.scanner(log)
            await radio._scan()
            from meter.bluetooth import SCANNER_RESTART
            with patch('meter.bluetooth.time.monotonic', return_value=time.monotonic() + SCANNER_RESTART + 1):
                await radio._scan()
            self.assertEqual(log, ['start', 'stop', 'start'])
