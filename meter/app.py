"""Companion lifecycle. Provider logs and SQLite are owned by one worker."""
from __future__ import annotations
import json
import logging
import math
import os
import secrets
import queue
import sqlite3
import threading
import time
from pathlib import Path
from PIL import Image
from .providers import NotSetUp, account_fingerprints, parse_claude, parse_codex, refresh
from .tokens import TokenIndex, is_corrupt
from .render import render, pack_frame
from .updater import save_json, UpdateService

# Codex quota is read by starting `codex app-server`, which is comparatively
# heavy. Its weekly quota only moves when Codex is used (which writes local
# session logs that the token scan already watches) or when it resets, so it
# is polled at the normal cadence only while there is local Codex activity or
# a reset is near; otherwise every CODEX_IDLE_INTERVAL (to still catch usage
# from other computers or the cloud). A forced refresh always polls it.
CODEX_IDLE_INTERVAL = 900
# A provider row is stale when its data is older than the expected poll plus
# this grace period, or its last poll failed.
STALE_GRACE = 300
# The meter accepts a frame and redraws the panel at the next minute boundary,
# so the frame is rendered a little ahead of that minute and shows its time.
RENDER_LEAD = 15
# Bounded restarts of an unexpectedly stopped Bluetooth worker.
BLUETOOTH_RESTARTS = 3
BLUETOOTH_RESTART_DELAYS = (5, 30, 120)
BLUETOOTH_HEALTHY_AFTER = 600
# Identical log lines are logged once and then summarised.
LOG_SUMMARY_AFTER = 600


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def display_time(now):
    """The minute at which a frame sent now will be shown by the meter."""
    return (math.floor(now / 60) + 1) * 60


def next_render(now, interval):
    """Wall-clock time to render the next frame: RENDER_LEAD seconds before
    the next ``interval`` boundary, never sooner than 5 seconds from now."""
    boundary = (math.floor(now / interval) + 1) * interval
    if boundary - RENDER_LEAD - now < 5:
        boundary += interval
    return boundary - RENDER_LEAD


def account_salt(state_dir):
    """Per-installation random salt for account fingerprints (not a secret
    by itself; it only keeps fingerprints unlinkable across computers)."""
    path = Path(state_dir) / 'account-salt'
    try:
        salt = path.read_bytes()
        if len(salt) == 32:
            return salt
    except OSError:
        pass
    salt = secrets.token_bytes(32)
    try:
        temp = path.with_suffix('.tmp')
        with open(temp, 'wb', opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
            handle.write(salt)
        temp.replace(path)
    except OSError:
        pass  # In-memory salt: cached quota is simply re-read after a restart.
    return salt


def display_snapshot(cache, index, available, now, totals=None):
    """Build display rows. Staleness is decided per provider and per row.

    Token totals come from this computer's local logs, which record no
    account. After a detected account switch they count only from the switch
    (``account_since``) so the previous account's local usage is not shown
    under the new one; before any switch they count the whole quota window.

    ``state`` per row is 'ok', 'stale' (last poll failed, data too old, or the
    window reset and a new value is not in yet) or 'absent' (not installed or
    not signed in; the row carries no values at all).
    """
    providers = cache.get('providers', {})
    if not isinstance(providers, dict):
        providers = {}
    rows, dates = [], []
    lookup = totals or (lambda *args, **kwargs: index.total(*args, **kwargs) if index is not None else None)
    for name, defaults in [('claude', parse_claude({})), ('codex', parse_codex({}))]:
        provider = providers.get(name)
        provider = provider if isinstance(provider, dict) else {}
        fetched = provider.get('fetched_at', 0)
        fetched = fetched if _number(fetched) else 0
        absent = provider.get('error_code') == NotSetUp.code
        if absent:
            state = 'absent'
        elif not provider:
            state = 'ok'  # Not polled yet: values are simply unknown ('--').
        else:
            next_poll = provider.get('next_poll', 0)
            expected = max(900, min(3600, next_poll - fetched) if _number(next_poll) and fetched else 0)
            old = not fetched or now - fetched > expected + STALE_GRACE
            state = 'stale' if provider.get('error') or old else 'ok'
            if fetched:
                dates.append(fetched)
        since = provider.get('account_since', 0)
        since = since if _number(since) else 0
        source = provider.get('rows') if not absent and isinstance(provider.get('rows'), list) else defaults
        for original in source:
            if not isinstance(original, dict):
                continue
            row = dict(original)
            row['tokens'] = None
            row['state'] = state
            if absent:
                row.update(used=None, reset=None, subscription_label='')
                rows.append(row)
                continue
            reset = row.get('reset')
            reset = reset if _number(reset) else None
            if reset and reset <= now:
                row['used'], row['state'] = None, 'stale'
            seconds = row.get('seconds')
            if reset and reset > now and name in available and _number(seconds):
                try:
                    row['tokens'] = lookup(name, max(reset - seconds, since), now, fable=row.get('key') == 'fable')
                except sqlite3.Error:
                    row['tokens'] = None
            rows.append(row)
    stale = any(row['state'] == 'stale' for row in rows)
    return dict(as_of=min(dates) if dates else now, stale=stale, rows=rows)


class ProviderWorker:
    def __init__(self, state_dir, emit, device=lambda: {}):
        self.state_dir, self.emit, self.device = Path(state_dir), emit, device
        self.stop, self.force = threading.Event(), threading.Event()
        # Render now without forcing provider polls (e.g. a meter reconnected).
        self.redraw = threading.Event()
        self.ready = threading.Event()
        self.startup_error = None
        self.index_error = None
        self.thread = threading.Thread(target=self.run, name='sweetmeter-providers', daemon=True)

    def start(self):
        self.thread.start()

    def _open_index(self):
        try:
            return TokenIndex(self.state_dir / 'tokens.sqlite3')
        except Exception as error:
            # Token totals are optional; quotas must still reach the meter.
            self.index_error = type(error).__name__
            logging.warning('Token index unavailable (%s); showing quotas without token totals',
                            self.index_error)
            return None

    def _scan(self, index):
        """Scan local logs. Returns (index, providers with logs, ok)."""
        if index is None:
            return None, set(), False
        try:
            return index, index.scan(), True
        except sqlite3.Error as error:
            if is_corrupt(error):
                logging.warning('Token index damaged during use; rebuilding it')
                try:
                    index.rebuild()
                except Exception:
                    index = None
            return index, set(), False
        except Exception as error:
            logging.warning('Token scan failed (%s); quotas are still shown', type(error).__name__)
            return index, set(), False

    @staticmethod
    def _codex_wake(cache, index, now):
        codex = cache.get('providers', {}).get('codex') or {}
        fetched = codex.get('fetched_at', 0) if _number(codex.get('fetched_at', 0)) else 0
        if index is not None and getattr(index, 'activity', {}).get('codex', 0) > fetched:
            return True
        for row in codex.get('rows') or []:
            reset = row.get('reset') if isinstance(row, dict) else None
            if _number(reset) and reset <= now + CODEX_IDLE_INTERVAL:
                return True
        return False

    def run(self):
        # Never construct SQLite on the UI/asyncio thread then use it here.
        index = None
        try:
            index = self._open_index()
            salt = account_salt(self.state_dir)
            try:
                cache = json.loads((self.state_dir / 'providers.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                cache = {}
            if not isinstance(cache, dict) or not isinstance(cache.get('providers', {}), dict):
                cache = {}
            self.ready.set()
            while not self.stop.is_set():
                forced = self.force.is_set()
                self.force.clear()
                self.redraw.clear()
                device = dict(self.device() or {})
                interval = 300 if device.get('interval') == 300 else 60
                if hasattr(time, 'tzset'):
                    time.tzset()  # Follow time-zone changes (travel, DST rules) while running.
                try:
                    now = time.time()
                    wake = {'codex'} if self._codex_wake(cache, index, now) else set()
                    # Checked every cycle: a switched account is noticed at once and
                    # its predecessor's rows, plan labels, errors and backoff dropped.
                    accounts = account_fingerprints(salt)
                    cache = refresh(cache, now, force=forced, interval=interval,
                                    relaxed={'codex': max(interval, CODEX_IDLE_INTERVAL)}, wake=wake,
                                    accounts=accounts)
                    save_json(self.state_dir / 'providers.json', cache)
                    index, available, _ok = self._scan(index)
                    now = time.time()
                    shown_at = display_time(now)
                    snapshot = display_snapshot(cache, index, available, shown_at)
                    snapshot.update(device=device, clock_at=shown_at)
                    save_json(self.state_dir / 'snapshot.json', snapshot)
                    screen = render(snapshot)
                    screen.save(self.state_dir / 'screen.png')
                    screen.resize((1000, 488), Image.Resampling.NEAREST).save(self.state_dir / 'screen-4x.png')
                    self.emit({'event': 'snapshot', 'frame': pack_frame(screen), 'snapshot': snapshot,
                               'providers': {name: {'error': value.get('error'), 'code': value.get('error_code')}
                                             for name, value in cache.get('providers', {}).items()
                                             if isinstance(value, dict)}})
                    wall_until = next_render(time.time(), interval)
                except Exception as error:
                    logging.warning('Local data refresh failed (%s); retrying', type(error).__name__)
                    self.emit({'event': 'provider_error', 'code': 'refresh_failed',
                               'error': 'Couldn’t update the dashboard from local data. Retrying in a few seconds.'})
                    wall_until = time.time() + 10
                self._wait(wall_until)
        except Exception as error:
            self.startup_error = type(error).__name__
            logging.error('Provider worker stopped (%s)', self.startup_error)
            self.emit({'event': 'provider_error', 'code': 'worker_stopped',
                       'error': 'Sweetmeter stopped reading usage data. Quit and reopen Sweetmeter.'})
        finally:
            self.ready.set()
            if index is not None:
                index.db.close()

    def _wait(self, wall_until):
        """Wait on both clocks: the monotonic clock may pause during system
        sleep and the wall clock may jump, so wake on whichever says so."""
        limit = time.monotonic() + max(0, min(wall_until - time.time(), 3600))
        while not self.stop.is_set():
            if time.time() >= wall_until or time.monotonic() >= limit:
                return
            if self.force.wait(.25) or self.redraw.is_set():
                return

    def close(self):
        self.stop.set()
        self.force.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)


class LogLimiter:
    """Log a message once, then only a count of identical repeats."""
    def __init__(self, clock=time.monotonic, summary_after=LOG_SUMMARY_AFTER):
        self.clock, self.summary_after = clock, summary_after
        self.last, self.repeats, self.since = None, 0, 0

    def log(self, level, message):
        now = self.clock()
        if message == self.last:
            self.repeats += 1
            if now - self.since >= self.summary_after:
                logging.log(level, '%s (repeated %d more times)', message, self.repeats)
                self.repeats, self.since = 0, now
            return
        self.flush()
        logging.log(level, '%s', message)
        self.last, self.repeats, self.since = message, 0, now

    def flush(self):
        if self.last is not None and self.repeats:
            logging.info('Previous message repeated %d more times: %s', self.repeats, self.last)
        self.last, self.repeats = None, 0


class Application:
    def __init__(self, state_dir, *, preview_only=False, bluetooth_factory=None):
        self.state_dir = Path(state_dir)
        self.events = queue.Queue()
        self.device = {}
        self.connected = False
        self.radio = None
        self.updates = None
        self.preview_only = preview_only
        self.pending_dashboard = None  # (version, device_id) after a verified OTA.
        self.device_id = None          # Meter whose trusted status is in self.device.
        self.connected_id = None       # Meter currently connected to this computer.
        self.last_frame = None
        self.closing = False
        self.bluetooth_factory = bluetooth_factory
        self.restarts = 0
        self.restart_at = None
        self.radio_started = None
        self.log_limit = LogLimiter()
        self.provider = ProviderWorker(state_dir, self.events.put, lambda: self.device)

    def _new_radio(self):
        if self.bluetooth_factory is None:
            from .bluetooth import Bluetooth
            self.bluetooth_factory = Bluetooth
        self.radio_started = time.monotonic()
        return self.bluetooth_factory(self.state_dir)

    def start(self):
        if not self.preview_only:
            self.radio = self._new_radio()
            self.updates = UpdateService(self.state_dir, self.radio, self.events.put)
            self.updates.start()
        self.provider.start()
        # Local data problems never block startup; the worker reports them.
        self.provider.ready.wait(10)
        if self.radio and (not self.radio.ready.wait(10) or self.radio.startup_error):
            raise RuntimeError('Bluetooth dependencies failed to initialize')

    def _remember_device(self, event, status):
        """Persist only the authenticated meter, merging with other keys."""
        path = self.state_dir / 'bluetooth.json'
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            saved = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            saved = {}
        saved.update(device_id=event['device_id'], status=status, seen_at=time.time())
        try:
            save_json(path, saved)
        except OSError as error:
            self.log_limit.log(logging.WARNING, 'Cannot save meter state (%s)' % type(error).__name__)

    def _radio_event(self, event):
        kind = event.get('event')
        if kind == 'status':
            if not event.get('trusted'):
                # Foreign or unauthenticated meters never affect update offers,
                # the render interval, or what is saved.
                return
            self.device = dict(event.get('status') or {})
            self.device_id = event.get('device_id')
            self._remember_device(event, self.device)
            self.updates.set_device(self.device, self.connected, event.get('device_id'))
        elif kind == 'connected':
            self.connected = True
            self.connected_id = event.get('device_id')
            if self.device_id is not None and self.device_id != self.connected_id:
                # Never render or update with another meter's status.
                self.device, self.device_id = {}, None
            self._remember_device(event, self.device)
            self.updates.set_device(self.device, True, event.get('device_id'))
            # The meter may have shown another computer meanwhile: send this
            # computer's dashboard with a current clock straight away.
            self.provider.redraw.set()
        elif kind == 'disconnected':
            self.connected, self.connected_id = False, None
            with self.updates.lock:
                self.updates.connected = False
        elif kind == 'forgotten':
            self.connected, self.device = False, {}
            self.device_id = self.connected_id = None
            self.pending_dashboard = None
            with self.updates.lock:
                self.updates.device_id = None
            self.updates.set_device({}, False)
        elif kind == 'refresh':
            self.provider.force.set()
        elif kind == 'update_request':
            logging.info('Button: firmware update check requested')
            self.updates.device_request()
        elif kind == 'ack':
            try:
                save_json(self.state_dir / 'last-ack.json', {**event, 'received_at': time.time()})
            except OSError:
                pass
            logging.info('Meter accepted dashboard %s %s %s', event.get('sequence'),
                         event.get('crc32'), event.get('ack'))
        if kind.startswith('ota_'):
            self.updates.transfer_event(event)

    def _bluetooth_exit(self, event):
        """Returns the events to show. Restarts a stopped worker a bounded
        number of times; only then is 'exit' passed on."""
        if self.closing:
            return [event]
        if self.radio_started is not None and time.monotonic() - self.radio_started > BLUETOOTH_HEALTHY_AFTER:
            self.restarts = 0  # It ran well for a while; this is a new incident.
        self.connected = False
        old, self.radio = self.radio, None
        try:
            old.close()
        except Exception:
            pass
        if self.restarts >= BLUETOOTH_RESTARTS:
            logging.error('Bluetooth stopped and could not be restarted')
            return [{'event': 'error', 'code': 'bluetooth_stopped',
                     'error': 'Bluetooth stopped and could not be restarted. Quit Sweetmeter and open it again; '
                              'if this keeps happening, restart the computer.'}, event]
        delay = BLUETOOTH_RESTART_DELAYS[min(self.restarts, len(BLUETOOTH_RESTART_DELAYS) - 1)]
        self.restarts += 1
        self.restart_at = time.monotonic() + delay
        logging.warning('Bluetooth stopped unexpectedly; restarting in %d s (attempt %d of %d)',
                        delay, self.restarts, BLUETOOTH_RESTARTS)
        return [{'event': 'error', 'code': 'bluetooth_restarting',
                 'error': 'Bluetooth stopped unexpectedly. Sweetmeter is restarting it…'}]

    def _restart_radio(self):
        self.restart_at = None
        try:
            radio = self._new_radio()
        except Exception as error:
            logging.error('Bluetooth restart failed (%s)', type(error).__name__)
            return self._bluetooth_exit({'event': 'exit'})
        self.radio = radio
        if self.updates:
            with self.updates.lock:
                self.updates.radio = radio
        if self.last_frame is not None:
            radio.send(self.last_frame)
        logging.info('Bluetooth worker restarted')
        return [{'event': 'bluetooth_restarted'}]

    def _log(self, event):
        kind = event['event']
        if kind in ('connected', 'disconnected', 'forgotten'):
            self.log_limit.log(logging.INFO, 'Meter %s %s' % (kind, event.get('device_id', '')))
        elif kind in ('error', 'update_error', 'ota_error', 'provider_error'):
            code = event.get('code')
            self.log_limit.log(logging.WARNING, '%s%s: %s' % (kind, ' [%s]' % code if code else '', event.get('error', '')))
        elif kind == 'bluetooth_state':
            self.log_limit.log(logging.INFO, 'Bluetooth state: %s' % event.get('state'))
        elif kind == 'selection_required':
            self.log_limit.log(logging.INFO, 'Meter needs this computer selected (%s)' % event.get('reason', ''))

    def pump(self):
        result = []
        if self.restart_at is not None and time.monotonic() >= self.restart_at and not self.closing:
            result.extend(self._restart_radio())
        if self.radio:
            while self.radio and (event := self.radio.poll(timeout=0)) is not None:
                if event.get('event') == 'exit':
                    for shown in self._bluetooth_exit(event):
                        self.events.put(shown)
                    break
                self._radio_event(event)
                self.events.put(event)
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._log(event)
            if event['event'] == 'snapshot':
                self.last_frame = event['frame']
                if self.radio:
                    self.radio.send(event['frame'])
            if event['event'] == 'firmware_verified':
                # Tied to the updated meter: an ACK from another meter does not count.
                self.pending_dashboard = (event['version'], self.device_id)
            elif event['event'] == 'ack' and self.pending_dashboard:
                version, target = self.pending_dashboard
                if target is None or target == self.connected_id:
                    result.append({'event': 'update_success', 'version': version})
                    self.pending_dashboard = None
            elif event['event'] in ('error', 'disconnected') and self.pending_dashboard:
                result.append({'event': 'update_notice', 'message': 'Firmware ' + self.pending_dashboard[0] +
                               ' passed boot checks. Dashboard connection/refresh is still pending.'})
            result.append(event)
        return result

    def forget_meter(self):
        """Stop using the paired meter. The Bluetooth worker confirms with
        a 'forgotten' event."""
        if self.radio is None:
            raise RuntimeError('Bluetooth is not running')
        self.radio.forget()

    def close(self):
        self.closing = True
        self.log_limit.flush()
        self.provider.close()
        if self.updates:
            self.updates.close()
        if self.radio:
            self.radio.close()
