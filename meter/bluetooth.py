"""Persistent native CoreBluetooth connection; no USB serial access."""
import base64
import json
import queue
import socket
import subprocess
import threading
import uuid
from pathlib import Path


def companion_identity(state_dir):
    path = state_dir / 'companion.json'
    try:
        identity = json.loads(path.read_text())
        valid = str(uuid.UUID(identity['host_id']))
        if not valid.startswith('7a1e1000-ff1b-4d9f-a023-'):
            raise ValueError('Wrong discovery namespace')
    except (OSError, ValueError, KeyError, TypeError):
        identity = {'host_id': '7a1e1000-ff1b-4d9f-a023-' + uuid.uuid4().hex[-12:]}
        path.write_text(json.dumps(identity) + '\n')
        path.chmod(0o600)
    name = socket.gethostname().split('.')[0].encode('ascii', 'ignore').decode()[:20]
    return identity['host_id'], name or 'Quota Mac'


class Bluetooth:
    def __init__(self, state_dir):
        helper = Path.home() / 'Library/Application Support/QuotaMeter/Quota Meter Bluetooth.app/Contents/MacOS/QuotaMeterBluetooth'
        if not helper.exists():
            raise RuntimeError('Bluetooth helper is missing; run scripts/install_agent.py')
        host_id, name = companion_identity(state_dir)
        args = [str(helper), '--host', host_id, '--name', name]
        try:
            device = json.loads((state_dir / 'bluetooth.json').read_text()).get('device_id')
            if device:
                uuid.UUID(device)
                args += ['--device', device]
        except (OSError, ValueError, TypeError):
            pass
        self.events = queue.Queue()
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                pass
        self.events.put({'event': 'exit'})

    def poll(self, timeout=1):
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def send(self, frame):
        if len(frame) != 4000:
            raise ValueError('Expected 4000-byte frame')
        self.process.stdin.write(json.dumps({'frame': base64.b64encode(frame).decode()}) + '\n')
        self.process.stdin.flush()

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=3)
        self.thread.join(timeout=1)
        self.process.stdout.close()
