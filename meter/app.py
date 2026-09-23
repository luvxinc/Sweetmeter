"""Companion lifecycle. Provider logs and SQLite are owned by one worker."""
from __future__ import annotations
import json
import logging
import queue
import threading
import time
from pathlib import Path
from PIL import Image
from .providers import parse_claude, parse_codex, refresh
from .tokens import TokenIndex
from .render import render, pack_frame
from .updater import save_json, UpdateService

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
                row['used'], stale = None, True
            if reset and reset > now and name in available:
                row['tokens'] = index.total(name, reset-row['seconds'], now, fable=row['key'] == 'fable')
            rows.append(row)
    return dict(as_of=min(dates) if dates else now, stale=stale, rows=rows)

class ProviderWorker:
    def __init__(self, state_dir, emit, device=lambda: {}):
        self.state_dir, self.emit, self.device = Path(state_dir), emit, device
        self.stop, self.force = threading.Event(), threading.Event()
        self.ready = threading.Event()
        self.startup_error = None
        self.thread = threading.Thread(target=self.run, name='sweetmeter-providers', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        # Never construct SQLite on the UI/asyncio thread then use it here.
        index = None
        try:
            index = TokenIndex(self.state_dir / 'tokens.sqlite3')
            try:
                cache = json.loads((self.state_dir / 'providers.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                cache = {}
            if not isinstance(cache, dict):
                cache = {}
            self.ready.set()
            while not self.stop.is_set():
                forced = self.force.is_set()
                self.force.clear()
                device = dict(self.device())
                interval = 300 if device.get('interval') == 300 else 60
                try:
                    cache = refresh(cache, time.time(), force=forced, interval=interval)
                    save_json(self.state_dir / 'providers.json', cache)
                    available = index.scan()
                    snapshot = display_snapshot(cache, index, available, time.time())
                    snapshot.update(device=device, clock_at=time.time())
                    save_json(self.state_dir / 'snapshot.json', snapshot)
                    screen = render(snapshot)
                    screen.save(self.state_dir / 'screen.png')
                    screen.resize((1000, 488), Image.Resampling.NEAREST).save(self.state_dir / 'screen-4x.png')
                    self.emit({'event': 'snapshot', 'frame': pack_frame(screen), 'snapshot': snapshot,
                               'providers': {name: value.get('error') for name, value in cache['providers'].items()}})
                except Exception as error:
                    interval = 10
                    self.emit({'event': 'provider_error', 'error': 'Local data refresh failed; retrying: ' + type(error).__name__})
                until = time.monotonic() + interval
                while not self.stop.is_set() and not self.force.wait(min(.25, max(0, until-time.monotonic()))):
                    if time.monotonic() >= until:
                        break
        except Exception as error:
            self.startup_error = type(error).__name__
            self.emit({'event': 'provider_error', 'error': 'Provider worker: ' + type(error).__name__})
        finally:
            self.ready.set()
            if index is not None:
                index.db.close()

    def close(self):
        self.stop.set()
        self.force.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)

class Application:
    def __init__(self, state_dir, *, preview_only=False):
        self.state_dir = Path(state_dir)
        self.events = queue.Queue()
        self.device = {}
        self.connected = False
        self.radio = None
        self.updates = None
        self.preview_only = preview_only
        self.pending_dashboard = None
        self.provider = ProviderWorker(state_dir, self.events.put, lambda: self.device)

    def start(self):
        if not self.preview_only:
            from .bluetooth import Bluetooth
            self.radio = Bluetooth(self.state_dir)
            self.updates = UpdateService(self.state_dir, self.radio, self.events.put)
            self.updates.start()
        self.provider.start()
        if not self.provider.ready.wait(10) or self.provider.startup_error:
            raise RuntimeError('Local token index failed to initialize')
        if self.radio and (not self.radio.ready.wait(10) or self.radio.startup_error):
            raise RuntimeError('Bluetooth dependencies failed to initialize')

    def pump(self):
        if self.radio:
            while (event := self.radio.poll(timeout=0)) is not None:
                kind = event['event']
                if kind == 'status':
                    self.device = event['status']
                    # Save only the pinned/authorized device, never a foreign probe.
                    if event['device_id'] == self.radio.pinned:
                        save_json(self.state_dir / 'bluetooth.json', {**event, 'seen_at': time.time()})
                    self.updates.set_device(self.device, self.connected, event['device_id'])
                elif kind == 'connected':
                    self.connected = True
                    save_json(self.state_dir / 'bluetooth.json', {**event, 'status': self.device, 'seen_at': time.time()})
                    self.updates.set_device(self.device, True, event['device_id'])
                elif kind == 'disconnected':
                    self.connected = False
                    self.updates.connected = False
                elif kind == 'refresh':
                    self.provider.force.set()
                elif kind == 'update_request':
                    logging.info('Button: firmware update check requested')
                    self.updates.device_request()
                elif kind == 'ack':
                    save_json(self.state_dir / 'last-ack.json', {**event, 'received_at': time.time()})
                    logging.info('BLE ACK %s %s %s', event['sequence'], event['crc32'], event['ack'])
                if kind.startswith('ota_'):
                    self.updates.transfer_event(event)
                self.events.put(event)
        result = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event['event'] in ('connected', 'disconnected', 'error', 'exit', 'update_error', 'ota_error'):
                # The desktop window shows these too, but only the log survives it.
                logging.info('Event %s %s', event['event'], event.get('error', event.get('device_id', '')))
            if event['event'] == 'snapshot' and self.radio:
                self.radio.send(event['frame'])
            if event['event'] == 'firmware_verified':
                self.pending_dashboard = event['version']
            elif event['event'] == 'ack' and self.pending_dashboard:
                result.append({'event': 'update_success', 'version': self.pending_dashboard})
                self.pending_dashboard = None
            elif event['event'] in ('error', 'disconnected') and self.pending_dashboard:
                result.append({'event': 'update_notice', 'message': 'Firmware ' + self.pending_dashboard +
                               ' passed boot checks. Dashboard connection/refresh is still pending.'})
            result.append(event)
        return result

    def close(self):
        self.provider.close()
        if self.updates:
            self.updates.close()
        if self.radio:
            self.radio.close()
