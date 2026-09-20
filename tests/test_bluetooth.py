import base64
import io
import json
import queue
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from meter.bluetooth import Bluetooth, companion_identity


class BluetoothTests(unittest.TestCase):
    def test_identity_is_stable_and_recovers_corrupt_file(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            one, name = companion_identity(state)
            self.assertEqual(str(uuid.UUID(one)), one)
            self.assertTrue(one.startswith('7a1e1000-ff1b-4d9f-a023-'))
            self.assertEqual(companion_identity(state)[0], one)
            (state / 'companion.json').write_text('broken')
            self.assertNotEqual(companion_identity(state)[0], one)

    def test_send_exact_frame_over_persistent_pipe(self):
        bridge = Bluetooth.__new__(Bluetooth)
        bridge.process = SimpleNamespace(stdin=io.StringIO())
        bridge.send(b'\xab' * 4000)
        self.assertEqual(base64.b64decode(json.loads(bridge.process.stdin.getvalue())['frame']), b'\xab' * 4000)
        with self.assertRaises(ValueError):
            bridge.send(b'bad frame')

    def test_reader_handles_events_and_parent_can_detect_helper_exit(self):
        bridge = Bluetooth.__new__(Bluetooth)
        bridge.events = queue.Queue()
        bridge.process = SimpleNamespace(stdout=io.StringIO('garbled\n{"event":"refresh"}\n{"event":"disconnected"}\n'))
        bridge._read()
        self.assertEqual(bridge.poll()['event'], 'refresh')
        self.assertEqual(bridge.poll()['event'], 'disconnected')
        self.assertEqual(bridge.poll()['event'], 'exit')
        self.assertIsNone(bridge.poll(timeout=0))

    def test_pinned_meter_is_preserved_on_reconnection(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            pinned = str(uuid.uuid4())
            (state / 'bluetooth.json').write_text(json.dumps({'device_id': pinned}))
            process = SimpleNamespace(stdout=io.StringIO(''), stdin=io.StringIO())
            with patch('meter.bluetooth.subprocess.Popen', return_value=process) as start, patch.object(Path, 'exists', return_value=True):
                bridge = Bluetooth(state)
                bridge.thread.join(timeout=1)
            args = start.call_args.args[0]
            self.assertEqual(args[args.index('--device') + 1], pinned)
            self.assertEqual(args[args.index('--host') + 1], companion_identity(state)[0])
