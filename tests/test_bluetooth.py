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
                             parse_status, pairing_proof, classify_error, value_budget,
                             CONTROL_UUID, DATA_UUID, DiscoveryOpened)

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
            self.begin_written.set()
            if self.auto_begin:
                self.callback(None, struct.pack('<cBII', b'b', self.begin_result, self.sequence, self.crc))
        elif data[:1] == b'C':
            self.callback(None, struct.pack('<cBII', b'A', self.result, self.sequence, self.crc))


class FakeMeter:
    """Emulates the firmware pairing rules (tests/test_pairing.cpp covers the C++)."""
    def __init__(self, address='meter', *, serial=SERIAL, legacy_status=False):
        self.address, self.serial, self.legacy_status = address, serial, legacy_status
        self.paired = {}            # host -> secret (None = migrated legacy selection)
        self.selected = None
        self.menu_nonce = 0
        self.connections = 0
        self.frames = []
        self.menu_name = False
        self.challenge = '00' * 16
    def status(self):
        if self.legacy_status:
            return {'protocol': 4, 'firmware': '2026.9.13', 'selected_host': self.selected or '',
                    'menu': bool(self.menu_nonce), 'discovery_nonce': self.menu_nonce}
        return {'protocol': 4, 'firmware': '2026.9.14', 'auth': 1, 'serial': self.serial,
                'selected': self.selected is not None,
                'secured': self.selected is not None and self.paired.get(self.selected) is not None,
                'challenge': self.challenge, 'menu': bool(self.menu_nonce), 'discovery_nonce': self.menu_nonce}
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
                elif op == b'P':
                    reply = 7
                    if self.state == 'open':
                        for host, secret in m.paired.items():
                            if secret is not None and pairing_proof(secret, m.challenge, m.serial, host) == data[1:]:
                                reply = 0 if host == m.selected else 9
                    self.state = 'authorized' if reply == 0 else 'closed'
                    self.callback(None, b'H' + bytes([reply]))
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
                elif op == b'T':
                    assert self.state == 'authorized'
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


def radio_for(folder, meters, *, hints=()):
    (Path(folder) / 'companion.json').write_text(json.dumps({'host_id': HOST}))
    radio = Bluetooth(folder, start=False)
    class Scanner:
        def __init__(self, detection_callback, **_):
            self.found = detection_callback
        async def __aenter__(self):
            for meter in meters:
                name = 'Sweetmeter-ABCD' + ('-PAIR' if meter.address in hints else '')
                self.found(SimpleNamespace(address=meter.address), SimpleNamespace(local_name=name))
        async def __aexit__(self, *_):
            pass
    radio.scanner_factory = Scanner
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
    def test_private_file_secret_reuse_and_forget(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PairingStore(folder)
            secret = store.secret_for('serial:' + SERIAL, 'addr-1')
            self.assertEqual(len(secret), 32)
            self.assertEqual(store.secret_for('serial:' + SERIAL, 'addr-2'), secret)
            self.assertFalse(store.trusted('serial:' + SERIAL, 'addr-2'))
            store.mark_paired('serial:' + SERIAL, 'addr-2')
            reloaded = PairingStore(folder)
            self.assertTrue(reloaded.trusted('serial:' + SERIAL, 'anything'))
            self.assertEqual(reloaded.secret('serial:' + SERIAL), secret)
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE((Path(folder) / 'pairings.json').stat().st_mode), 0o600)
            reloaded.forget_all()
            self.assertIsNone(PairingStore(folder).secret('serial:' + SERIAL))
    def test_legacy_pin_is_imported_once(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'bluetooth.json').write_text(json.dumps({'device_id': 'old-meter'}))
            store = PairingStore(folder)
            self.assertTrue(store.trusted('address:old-meter', 'old-meter'))
            store.forget_all()
            # After forgetting, the old pin cannot resurrect itself.
            self.assertFalse(PairingStore(folder).trusted('address:old-meter', 'old-meter'))
    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'pairings.json').write_text('{"schema":1,"meters":{"serial:x":{"secret":"zz"}}}')
            self.assertIsNone(PairingStore(folder).secret('serial:x'))


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
            secret = radio.store.secret('serial:' + SERIAL)
            self.assertEqual(meter.candidates[HOST], secret)
            self.assertEqual(events_of(radio)[-1], {'event': 'registered', 'name': radio.name})
            meter.select(HOST)
            self.assertEqual(await self.visit(radio, meter), 0)
            kinds = [e['event'] for e in events_of(radio)]
            self.assertEqual(kinds[:2], ['status', 'connected'])
            self.assertIn('disconnected', kinds)
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'x'))
            self.assertEqual(radio.pinned, 'meter')
            # The owner switches the meter to another paired computer.
            meter.paired[OTHER] = b'\x05' * 32
            meter.select(OTHER)
            delay = await self.visit(radio, meter)
            events = events_of(radio)
            self.assertTrue(events[0]['trusted'])
            self.assertEqual(events[-1]['reason'], 'other_computer')
            self.assertLessEqual(delay, 12)  # prompt return when switched back
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'x'))
            meter.select(HOST)
            self.assertEqual(await self.visit(radio, meter), 0)
            self.assertIn('connected', [e['event'] for e in events_of(radio)])

    async def test_removed_from_meter_loses_trust_and_backs_off(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            radio = radio_for(folder, [meter])
            radio.store.secret_for('serial:' + SERIAL, 'meter')
            radio.store.mark_paired('serial:' + SERIAL, 'meter')
            meter.paired[OTHER] = b'\x05' * 32
            meter.selected = OTHER
            delay = await self.visit(radio, meter)
            self.assertGreaterEqual(delay, 30)
            self.assertEqual(events_of(radio)[-1]['reason'], 'other_computer')
            self.assertFalse(radio.store.trusted('serial:' + SERIAL, 'meter'))

    async def test_meter_replacement_and_second_meter(self):
        with tempfile.TemporaryDirectory() as folder:
            old, new = FakeMeter('old', serial='000000000001'), FakeMeter('new', serial='000000000002')
            radio = radio_for(folder, [old, new], hints={'new'})
            radio.store.secret_for('serial:000000000001', 'old')
            radio.store.mark_paired('serial:000000000001', 'old')
            old.paired[HOST] = radio.store.secret('serial:000000000001')
            old.selected = HOST
            new.menu_nonce = 5
            await radio._cycle()
            self.assertIn(HOST, new.candidates)
            new.select(HOST)
            self.assertEqual(await self.visit(radio, new), 0)
            self.assertTrue(radio.store.trusted('serial:000000000002', 'new'))
            self.assertTrue(radio.store.trusted('serial:000000000001', 'old'))
            self.assertNotEqual(radio.store.secret('serial:000000000001'), radio.store.secret('serial:000000000002'))

    async def test_legacy_selection_migrates_with_one_hello_and_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            meter.paired[HOST] = None
            meter.selected = HOST
            radio = radio_for(folder, [meter])
            self.assertEqual(await self.visit(radio, meter), 0)
            secret = radio.store.secret('serial:' + SERIAL)
            self.assertEqual(meter.paired[HOST], secret)
            self.assertTrue(radio.store.trusted('serial:' + SERIAL, 'meter'))
            # Afterwards only the authenticated hello works.
            self.assertEqual(await self.visit(radio, meter), 0)

    async def test_legacy_migration_by_another_computer_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter()
            meter.paired[OTHER] = None
            meter.selected = OTHER
            radio = radio_for(folder, [meter])
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
            self.assertTrue(radio.store.trusted('address:meter', 'meter'))
            # Pre-secret firmware registers with the legacy body (no secret).
            meter.selected, meter.menu_nonce = None, 9
            await self.visit(radio, meter)
            self.assertIsNone(meter.candidates[HOST])

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
            self.assertEqual(events_of(radio)[-1], {'event': 'bluetooth_state', 'state': 'ok'})

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

    async def test_old_notices_are_dropped(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio.update_notice(2)
            radio.notices.put((3, time.monotonic() - 61))  # queued while disconnected
            client = FakeClient()
            session = Session(client, HOST, 'Test PC', radio.events.put)
            await session.subscribe()
            status = {'protocol': 4, 'firmware': '2026.9.14'}
            async def read(_):
                return json.dumps(status).encode()
            async def clock(_self):
                pass
            async def stop(_seconds):
                client.is_connected = False
            client.read_gatt_char, radio._sleep = read, stop
            with patch.object(Session, 'clock', clock):
                await radio._connected(session, 'meter', status, 'address:meter')
            self.assertEqual([p for c, p in client.writes if p[:1] == b'u'], [b'u\x02'])
            self.assertTrue(radio.notices.empty())
        with self.assertRaises(ValueError):
            radio.update_notice(6)

    async def test_forget_clears_pairings_and_announces(self):
        with tempfile.TemporaryDirectory() as folder:
            radio = radio_for(folder, [])
            radio.store.secret_for('serial:' + SERIAL, 'meter')
            radio.store.mark_paired('serial:' + SERIAL, 'meter')
            radio.pinned = 'meter'
            radio.forget()
            self.assertIsNone(radio.pinned)
            self.assertIsNone(radio.store.secret('serial:' + SERIAL))
            self.assertEqual(events_of(radio)[-1], {'event': 'forgotten'})

    async def test_first_binding_instruction_survives_discovery_disconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            meter = FakeMeter(legacy_status=True)
            radio = radio_for(folder, [meter])
            delays = []
            async def sleep(seconds):
                delays.append(seconds)
                if len(delays) == 2:
                    radio.stop.set()
            radio._sleep = sleep
            await radio._run()
            kinds = [e['event'] for e in events_of(radio)]
            self.assertEqual(kinds, ['bluetooth_state', 'status', 'selection_required'])
            self.assertLessEqual(delays[-1], 5)


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


if __name__ == '__main__':
    unittest.main()
