"""Bounded hardware observations; serial is diagnostic only, quotas remain BLE.

--reset-cycles deliberately pulses EN (hardware reset, NOT power removal).
With zero cycles this only observes serial and the companion's status/ACK files.
"""
import argparse
import json
import time
from pathlib import Path

import serial


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True, help='Diagnostic serial port for this device')
    parser.add_argument('--reset-cycles', type=int, default=0)
    parser.add_argument('--recovery-timeout', type=int, default=90,
                        help='Allows BLE reconnect plus the next 30s status read')
    parser.add_argument('--observe-seconds', type=int, default=60)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True,
                        help='State directory belonging to the running companion')
    args = parser.parse_args()
    state = args.state_dir
    expected_host = read_json(state / 'companion.json')['host_id']
    evidence = {'started_at': time.time(), 'resets': [], 'events': []}
    stream = serial.Serial(None, 115200, timeout=.1)
    stream.dtr = False
    stream.rts = False
    stream.port = args.port
    stream.open()
    seen_status = seen_ack = 0
    pending = bytearray()

    def record(kind, value):
        item = {'at': time.time(), 'kind': kind, 'value': value}
        evidence['events'].append(item)
        print(json.dumps(item), flush=True)

    def observe():
        nonlocal seen_status, seen_ack
        pending.extend(stream.read(max(1, stream.in_waiting)))
        while b'\n' in pending:
            line, _, remainder = pending.partition(b'\n')
            pending[:] = remainder
            text = line.decode('utf-8', 'replace').strip()
            if text:
                record('serial', text)
        status = read_json(state / 'bluetooth.json')
        if status.get('seen_at', 0) > seen_status:
            seen_status = status['seen_at']
            record('status', status)
        ack = read_json(state / 'last-ack.json')
        if ack.get('received_at', 0) > seen_ack:
            seen_ack = ack['received_at']
            record('ack', ack)
        return status, ack

    try:
        for cycle in range(args.reset_cycles):
            started = time.time()
            first_event = len(evidence['events'])
            record('reset', {'cycle': cycle + 1})
            stream.rts = True
            time.sleep(.1)
            stream.rts = False
            while time.time() - started < args.recovery_timeout:
                status, ack = observe()
                device = status.get('status', {})
                serial_lines = [event['value'] for event in evidence['events'][first_event:]
                                if event['kind'] == 'serial']
                if (any('panic\'ed' in line or 'Guru Meditation' in line for line in serial_lines) or
                        sum(('READY QM' in line or 'READY SWEETMETER' in line) for line in serial_lines) > 1):
                    result = {'cycle': cycle + 1, 'pass': False,
                              'reason': 'Panic or unexpected second boot during recovery'}
                    break
                if (status.get('seen_at', 0) > started and
                        device.get('clock_synced') and
                        (device.get('selected') is True if device.get('auth') == 1
                         else device.get('selected_host') == expected_host) and
                        device.get('uptime_ms', 9999999) < (time.time() - started + 3) * 1000 and
                        ack.get('received_at', 0) > started):
                    result = {'cycle': cycle + 1, 'pass': True,
                              'recovered_in_seconds': round(time.time() - started, 2),
                              'status': status, 'ack': ack}
                    break
            else:
                result = {'cycle': cycle + 1, 'pass': False,
                          'reason': f'No complete recovery within {args.recovery_timeout}s'}
            evidence['resets'].append(result)
            record('reset_result', result)
            if not result['pass']:
                break
        end = time.monotonic() + args.observe_seconds
        while time.monotonic() < end:
            observe()
    finally:
        stream.close()
        evidence['finished_at'] = time.time()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    if any(not result['pass'] for result in evidence['resets']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
