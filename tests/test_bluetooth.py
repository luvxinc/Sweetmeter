import asyncio
import json
import queue
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from meter.bluetooth import (Bluetooth, Session, companion_identity, parse_status,
                             value_budget, CONTROL_UUID, DATA_UUID, DiscoveryOpened)

HOST = '7a1e1000-ff1b-4d9f-a023-0123456789ab'

class FakeClient:
    mtu_size = 23
    is_connected = True
    def __init__(self):
        self.writes = []
        self.callback = None
        self.body = bytearray()
        self.frame = bytearray()
        self.result = 0
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
        elif data[:1] == b'H':
            self.callback(None, b'H' + bytes([self.result]))
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
    def test_safe_mtu_budget(self):
        self.assertEqual(value_budget(SimpleNamespace(mtu_size=23)), 20)
        self.assertEqual(value_budget(SimpleNamespace(mtu_size=512)), 182)
    def test_send_keeps_only_latest_frame_and_rejects_wrong_size(self):
        import threading
        radio = Bluetooth.__new__(Bluetooth)
        radio.frame_lock = threading.Lock()
        radio.send(b'x'*4000)
        radio.send(b'y'*4000)
        self.assertEqual(radio.latest_frame, b'y'*4000)
        with self.assertRaises(ValueError): radio.send(b'bad')

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_binding_instruction_survives_discovery_disconnect(self):
        radio = Bluetooth.__new__(Bluetooth)
        radio.pinned = None
        radio.host_id, radio.name = HOST, 'Test PC'
        radio.stop, radio.ready = threading.Event(), threading.Event()
        radio.events = queue.Queue()
        class Scanner:
            def __init__(self, detection_callback, **_): self.found = detection_callback
            async def __aenter__(self): self.found(SimpleNamespace(address='meter'), None)
            async def __aexit__(self, *_): pass
        class Client:
            def __init__(self, *_, **__): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def read_gatt_char(self, _):
                return b'{"protocol":4,"firmware":"2026.9.8","selected_host":""}'
        delays = []
        async def sleep(seconds):
            delays.append(seconds)
            if len(delays) == 2: radio.stop.set()
        radio.scanner_factory, radio.client_factory, radio._sleep = Scanner, Client, sleep
        with patch.object(Session, 'subscribe', new_callable=AsyncMock):
            await radio._run()
        events = []
        while not radio.events.empty(): events.append(radio.events.get()['event'])
        self.assertEqual(events, ['status', 'selection_required'])
        self.assertLessEqual(delays[-1], 5)

    async def asyncSetUp(self):
        self.client = FakeClient()
        self.events = []
        self.session = Session(self.client, HOST, 'Test PC', self.events.append)
        await self.session.subscribe()
    async def test_minimum_mtu_registration_exact_body(self):
        await self.session.register(123)
        self.assertEqual(bytes(self.client.body), HOST.encode()+b'\x07Test PC')
        self.assertTrue(all(len(packet)<=20 for _, packet in self.client.writes))
    async def test_rejected_registration_never_commits(self):
        self.client.result = 2
        with self.assertRaises(RuntimeError): await self.session.register(123)
        self.assertFalse(any(p[:1] == b'K' for _,p in self.client.writes))
    async def test_frame_requires_matching_application_ack(self):
        data = bytes(range(250))*16
        await self.session.frame(data)
        self.assertEqual(bytes(self.client.frame), data)
        self.assertEqual(self.events[-1]['ack'], 'FULL')
        self.assertTrue(all(len(p)<=20 for c,p in self.client.writes if c==DATA_UUID))
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
        await asyncio.sleep(0)
        self.assertFalse(self.client.is_connected)
        with self.assertRaises(DiscoveryOpened): await self.session.clock()
    async def test_nonselected_hello_refused(self):
        self.client.result = 7
        with self.assertRaises(PermissionError): await self.session.hello()
