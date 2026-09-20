import argparse
import fcntl
import json
import logging
import os
import time
from pathlib import Path

from PIL import Image

from .providers import parse_claude, parse_codex, refresh
from .render import pack_frame, render
from .tokens import TokenIndex
from .bluetooth import Bluetooth


def display_snapshot(cache, index, available, now):
    providers = cache.get('providers', {})
    rows, dates, stale = [], [], False
    for name, defaults in [('claude', parse_claude({})), ('codex', parse_codex({}))]:
        provider = providers.get(name, {})
        fetched = provider.get('fetched_at', 0)
        if fetched:
            dates.append(fetched)
        stale |= bool(provider.get('error')) or now - fetched > 900
        for original in provider.get('rows', defaults):
            row = dict(original)
            row['tokens'] = None
            reset = row['reset']
            if reset and reset <= now:
                row['used'] = None
                stale = True
            if reset and reset > now and name in available:
                row['tokens'] = index.total(name, reset - row['seconds'], now,
                                            fable=row['key'] == 'fable')
            rows.append(row)
    return dict(as_of=min(dates) if dates else now, stale=stale, rows=rows)


def save_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description='CrowPanel single-page quota meter')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--preview-only', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'state')
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    lock = (args.state_dir / 'meter.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Quota meter is already running')
    try:
        cache = json.loads((args.state_dir / 'providers.json').read_text())
    except (OSError, ValueError):
        cache = {}
    index = TokenIndex(args.state_dir / 'tokens.sqlite3')
    device_status = {}
    radio = None if args.preview_only else Bluetooth(args.state_dir)
    poll_at = 0
    forced = False
    connected = False
    frame_due = True
    started = time.time()
    try:
        while True:
            now = time.time()
            event = radio.poll(timeout=0.25) if radio else None
            if event:
                kind = event.get('event')
                if kind == 'status':
                    device_status = event['status']
                    save_json(args.state_dir / 'bluetooth.json', {**event, 'seen_at': now})
                elif kind == 'connected':
                    connected = True
                    frame_due = True
                    logging.info('BLE connected; clock sync and automatic reconnect active')
                elif kind == 'disconnected':
                    connected = False
                elif kind == 'refresh':
                    forced = True
                    logging.info('Button: immediate provider/token refresh requested')
                elif kind == 'ack':
                    logging.info('BLE ACK %s %s %s', event['sequence'], event['crc32'], event['ack'])
                    save_json(args.state_dir / 'last-ack.json', {**event, 'received_at': now})
                    if args.once:
                        return 0
                elif kind == 'error':
                    logging.warning('Bluetooth: %s', event['error'])
                elif kind == 'advertising':
                    logging.info('Computer discovery is advertising as %s', event['name'])
                elif kind == 'exit':
                    raise RuntimeError('Bluetooth helper exited; launchd will restart the companion')
            if now >= poll_at or forced:
                interval = 300 if device_status.get('interval') == 300 else 60
                cache = refresh(cache, now, force=forced, interval=interval)
                save_json(args.state_dir / 'providers.json', cache)
                for provider, status in cache['providers'].items():
                    if status.get('error'):
                        logging.warning('%s: %s; preserving last known quotas', provider, status['error'])
                available = index.scan()
                snapshot = display_snapshot(cache, index, available, time.time())
                snapshot['device'] = device_status
                snapshot['clock_at'] = time.time()
                save_json(args.state_dir / 'snapshot.json', snapshot)
                screen = render(snapshot)
                screen.save(args.state_dir / 'screen.png')
                screen.resize((1000, 488), Image.Resampling.NEAREST).save(args.state_dir / 'screen-4x.png')
                frame = pack_frame(screen)
                poll_at = now + interval
                forced = False
                frame_due = True
                if args.preview_only and args.once:
                    return 0
            if radio and connected and frame_due and not device_status.get('critical'):
                radio.send(frame)
                frame_due = False
            if args.once and now - started > 90:
                logging.error('Timed out waiting for a display acknowledgment')
                return 1
            if not radio:
                time.sleep(1)
    finally:
        if radio:
            radio.close()
        index.db.close()


if __name__ == '__main__':
    raise SystemExit(main())
