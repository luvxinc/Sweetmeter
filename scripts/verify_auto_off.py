"""Exercise real BLE loss/recovery and the 30s sleep rule on the attached meter.

Temporarily stops only this meter's companion. Uses EN reset to restore the meter
after observing indefinite sleep; this does not claim a physical button wake.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import serial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', default='com.sweetmeter.companion')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        raise SystemExit('This diagnostic controls launchd and currently supports macOS only.')
    state, report = args.state_dir, args.output
    domain = f'gui/{os.getuid()}'
    evidence = {'started_at': time.time(), 'events': [], 'passed': False}
    stream = serial.Serial(None, 115200, timeout=.1)
    stream.dtr = stream.rts = False
    stream.port = args.port
    stream.open()
    pending = bytearray()
    running = True

    def read(name):
        try:
            return json.loads((state / name).read_text())
        except (OSError, ValueError):
            return {}

    def record(kind, value):
        item = {'at': time.time(), 'kind': kind, 'value': value}
        evidence['events'].append(item)
        print(json.dumps(item), flush=True)

    def pump():
        pending.extend(stream.read(max(1, stream.in_waiting)))
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending[:] = rest
            text = line.decode('utf-8', 'replace').strip()
            if text:
                record('serial', text)
                if 'Guru Meditation' in text or "panic'ed" in text:
                    raise RuntimeError('Firmware panic during the hardware test')

    def wait_for(predicate, timeout, description):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            pump()
            result = predicate()
            if result:
                return result
        raise RuntimeError('Timed out: ' + description)

    def observe(seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            pump()

    def message(prefix, since):
        return next((event for event in evidence['events'] if event['at'] >= since
                     and event['kind'] == 'serial' and event['value'].startswith(prefix)), None)

    def stop():
        nonlocal running
        at = time.time()
        subprocess.run(['launchctl', 'bootout', domain + '/' + args.label], check=True)
        running = False
        record('companion', 'stopped')
        return at

    def start():
        nonlocal running
        subprocess.run(['launchctl', 'bootstrap', domain,
                        str(Path.home() / 'Library/LaunchAgents' / (args.label + '.plist'))], check=True)
        running = True
        record('companion', 'started')

    def recovered(since):
        status, ack = read('bluetooth.json'), read('last-ack.json')
        device = status.get('status', {})
        if (status.get('seen_at', 0) > since and device.get('firmware')
                and device.get('clock_synced') and ack.get('received_at', 0) > since):
            return {'status': status, 'ack': ack}

    def reset():
        at = time.time()
        stream.rts = True
        time.sleep(.1)
        stream.rts = False
        record('reset', 'EN reset for test recovery, not button wake')
        return at

    try:
        evidence['baseline'] = wait_for(lambda: recovered(evidence['started_at'] - 60),
                                        90, 'initial connection and clock')
        short_at = stop()
        lost = wait_for(lambda: message('POWER LINK_LOST', short_at), 20, 'short outage disconnect')
        observe(5)
        start()
        cancelled = wait_for(lambda: message('POWER AUTO_OFF_CANCELLED', short_at), 28,
                             'selected target reconnect cancels countdown')
        observe(max(0, lost['at'] + 36 - time.time()))
        if message('POWER AUTO_OFF disconnected_ms=', short_at) or message('POWER SLEEP', short_at):
            raise RuntimeError('Meter slept despite target reconnecting during grace')
        evidence['short_outage'] = {'pass': True, 'lost': lost, 'cancelled': cancelled,
                                    'recovery': wait_for(lambda: recovered(short_at), 35, 'short outage recovery')}
        record('result', 'Short outage cancelled; meter stayed awake beyond the old deadline')

        long_at = stop()
        lost = wait_for(lambda: message('POWER LINK_LOST', long_at), 20, 'long outage disconnect')
        off = wait_for(lambda: message('POWER AUTO_OFF disconnected_ms=', long_at), 36, '30-second shutdown')
        elapsed = int(re.search(r'disconnected_ms=(\d+)', off['value']).group(1))
        assert 30000 <= elapsed <= 32500, elapsed
        asleep = wait_for(lambda: message('POWER SLEEP timer=0', long_at), 10, 'indefinite deep sleep')
        observe(12)
        start()
        observe(10)
        if message('READY QM', asleep['at']):
            raise RuntimeError('Meter restarted or woke without a button/reset')
        if read('last-ack.json').get('received_at', 0) > asleep['at']:
            raise RuntimeError('Unexpected BLE frame acknowledgment while supposedly asleep')
        evidence['long_outage'] = {'pass': True, 'lost': lost, 'off': off, 'sleep': asleep,
                                   'shutdown_started_after_ms': elapsed,
                                   'stayed_asleep_through_companion_return': True}
        record('result', 'Long outage entered deep sleep; returning Mac did not wake it')
        reset_at = reset()
        evidence['final_recovery'] = wait_for(lambda: recovered(reset_at), 90, 'final reset recovery')
        evidence['passed'] = True
    except BaseException as error:
        evidence['error'] = str(error)
        raise
    finally:
        if not running:
            start()
        if not evidence['passed']:
            reset()  # Leave the device able to reconnect even after a failed test.
        stream.close()
        evidence['finished_at'] = time.time()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({'passed': evidence['passed'], 'report': str(report)}), flush=True)


if __name__ == '__main__':
    main()
