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
from .providers import (NotSetUp, account_fingerprints, close_codex, in_backoff, parse_claude, parse_codex,
                        refresh)
from .tokens import TokenIndex, is_corrupt
from .render import render, pack_frame
from .updater import save_json, UpdateService
from .version import Version, get_version

# Claude and Codex quotas are both read every render interval (about once a
# minute). Codex is asked through one long-lived `codex app-server` process
# (see providers.CodexSession), so a poll is a single JSON-RPC request rather
# than a new process. A forced refresh polls both at once. Each provider is
# polled on its own thread; a frame is published once both answered or after
# FIRST_FRAME_WAIT seconds, whichever is first, so a slow or hung provider
# never delays the other one's rows (or the Refresh button's frame).
PROVIDERS = ('claude', 'codex')
FIRST_FRAME_WAIT = 3
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
    """Owns provider polling, the token index and frame rendering.

    Claude and Codex are polled on their own short-lived threads so neither
    can hold up the other: a frame is published as soon as both answered, or
    after FIRST_FRAME_WAIT seconds with whatever has arrived (a provider still
    being asked keeps its previous rows), and again when a late answer comes.
    A provider whose previous poll is still running is not asked again.
    """

    def __init__(self, state_dir, emit, device=lambda: {}):
        self.state_dir, self.emit, self.device = Path(state_dir), emit, device
        self.stop, self.force = threading.Event(), threading.Event()
        # Render now without forcing provider polls (e.g. a meter reconnected).
        self.redraw = threading.Event()
        self.ready = threading.Event()
        self.startup_error = None
        self.index_error = None
        self.results = queue.Queue()
        self.polling = {}  # Provider name -> thread still asking it.
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

    # -- provider polls (one thread per provider) ---------------------------
    def _poll(self, name, entry, now, forced, interval, salt):
        """Runs on the provider's own thread: confirm its account identity
        (a changed one is re-read after a short settle delay) and poll it."""
        try:
            # While Codex is in an error backoff its app-server is not asked
            # for the identity either; a hung server is not waited on again.
            probe = not in_backoff(entry, now, force=forced)
            known = {name: entry['account']} if entry.get('account') is not None else {}
            accounts = account_fingerprints(salt, known, names=(name,), probe=probe, wait=self.stop.wait)
            result = refresh({'providers': {name: dict(entry)}}, now, force=forced, interval=interval,
                             accounts={name: (accounts or {}).get(name)}, names=(name,))
            self.results.put((name, result.get('providers', {}).get(name)))
        except Exception as error:
            logging.warning('Reading %s usage failed (%s); retrying', name, type(error).__name__)
            self.results.put((name, error))

    def _start_polls(self, cache, now, forced, interval, salt):
        providers = cache.setdefault('providers', {})
        for name in PROVIDERS:
            running = self.polling.get(name)
            if running is not None and running.is_alive():
                continue  # Still waiting for its previous answer; keep its rows.
            entry = providers.get(name)
            entry = dict(entry) if isinstance(entry, dict) else {}
            thread = threading.Thread(target=self._poll, args=(name, entry, now, forced, interval, salt),
                                      name='sweetmeter-poll-' + name, daemon=True)
            self.polling[name] = thread
            thread.start()

    def _merge(self, cache, block_until=None):
        """Apply finished polls. With ``block_until`` (monotonic), first wait
        until every running poll finished or that time passed. Returns True
        when a poll failed unexpectedly."""
        failed = False
        while True:
            running = [name for name, thread in self.polling.items() if thread.is_alive()]
            timeout = None
            if block_until is not None and running:
                timeout = block_until - time.monotonic()
            try:
                if timeout is None or timeout <= 0:
                    name, entry = self.results.get_nowait()
                else:
                    name, entry = self.results.get(timeout=min(timeout, .25))
            except queue.Empty:
                if timeout is None or timeout <= 0 or self.stop.is_set():
                    return failed
                continue
            self.polling.pop(name, None)
            if isinstance(entry, Exception):
                failed = True
            elif isinstance(entry, dict):
                cache.setdefault('providers', {})[name] = entry

    def _publish(self, cache, index, device):
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
        return index

    def _refresh_failed(self):
        self.emit({'event': 'provider_error', 'code': 'refresh_failed',
                   'error': 'Couldn’t update the dashboard from local data. Retrying in a few seconds.'})

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
                    # Every cycle checks each account (a switched account is
                    # noticed and its predecessor's rows, plan labels, errors
                    # and backoff dropped) and polls the providers that are due.
                    self._start_polls(cache, time.time(), forced, interval, salt)
                    failed = self._merge(cache, block_until=time.monotonic() + FIRST_FRAME_WAIT)
                    index = self._publish(cache, index, device)
                    wall_until = next_render(time.time(), interval)
                    if failed:
                        self._refresh_failed()
                        wall_until = min(wall_until, time.time() + 10)
                except Exception as error:
                    logging.warning('Local data refresh failed (%s); retrying', type(error).__name__)
                    self._refresh_failed()
                    wall_until = time.time() + 10
                # A late answer (a slow provider) is shown as soon as it
                # arrives, without waiting for the next render time.
                while self._wait(wall_until) == 'result':
                    try:
                        if self._merge(cache):
                            self._refresh_failed()
                        index = self._publish(cache, index, device)
                    except Exception as error:
                        logging.warning('Local data refresh failed (%s); retrying', type(error).__name__)
        except Exception as error:
            self.startup_error = type(error).__name__
            logging.error('Provider worker stopped (%s)', self.startup_error)
            self.emit({'event': 'provider_error', 'code': 'worker_stopped',
                       'error': 'Sweetmeter stopped reading usage data. Quit and reopen Sweetmeter.'})
        finally:
            self.ready.set()
            close_codex()
            if index is not None:
                index.db.close()

    def _wait(self, wall_until):
        """Wait on both clocks: the monotonic clock may pause during system
        sleep and the wall clock may jump, so wake on whichever says so.
        Returns 'result' when a provider poll finished meanwhile."""
        limit = time.monotonic() + max(0, min(wall_until - time.time(), 3600))
        while not self.stop.is_set():
            if time.time() >= wall_until or time.monotonic() >= limit:
                return 'time'
            if self.force.wait(.25) or self.redraw.is_set():
                return 'wake'
            if not self.results.empty():
                return 'result'
        return 'stop'

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
        self.version = get_version()
        # Firmware versions already reported as newer than this app (one update check each).
        self.newer_firmware = set()

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

    def _meter_version(self, event):
        """A meter newer than this app needs an app update: check now and say so.

        Only the meter this computer is paired with, or one it is registering
        with while the owner has its computer list open, counts; a stranger's
        meter nearby (or a device imitating one) does not."""
        status = event.get('status') or {}
        store = getattr(self.radio, 'store', None)
        pairing = bool(status.get('menu')) and store is not None and store.related(event.get('device_id'))
        if not (event.get('trusted') or pairing):
            return
        firmware = status.get('firmware')
        try:
            newer = Version.parse(firmware) > Version.parse(self.version)
        except ValueError:
            return
        if not newer or firmware in self.newer_firmware:
            return
        self.newer_firmware.add(firmware)
        self.events.put({'event': 'meter_newer', 'firmware': firmware, 'companion': self.version})
        if self.updates:
            self.updates.check()

    def rename_meter(self, text):
        """Rename the connected meter (empty restores its default name).

        Raises ValueError with a user-facing reason for a name the meter would
        refuse; the outcome arrives as a ``renamed`` event."""
        from .naming import encode_meter_name
        data = encode_meter_name(text)
        if self.radio is None:
            raise RuntimeError('Bluetooth is not running')
        self.radio.rename(data)

    def _radio_event(self, event):
        kind = event.get('event')
        if kind == 'status':
            self._meter_version(event)
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
            detail = event.get('detail')
            self.log_limit.log(logging.WARNING, '%s%s: %s%s' % (kind, ' [%s]' % code if code else '', event.get('error', ''),
                                                              ' (%s)' % detail if detail else ''))
        elif kind == 'bluetooth_state':
            self.log_limit.log(logging.INFO, 'Bluetooth state: %s' % event.get('state'))
        elif kind == 'selection_required':
            self.log_limit.log(logging.INFO, 'Meter needs this computer selected (%s)' % event.get('reason', ''))
        elif kind == 'registration_failed':
            self.log_limit.log(logging.WARNING, 'Meter refused registration (%s)' % event.get('result'))
        elif kind == 'renamed':
            self.log_limit.log(logging.INFO, 'Meter rename %s' % ('stored' if event.get('ok') else 'failed'))
        elif kind == 'os_pairing_prompt':
            self.log_limit.log(logging.INFO, 'Waiting for the system Bluetooth pairing prompt')
        elif kind == 'meter_newer':
            self.log_limit.log(logging.WARNING, 'Meter firmware %s is newer than this app %s' %
                               (event.get('firmware'), event.get('companion')))

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

    @property
    def bluetooth_failed(self):
        """True once the Bluetooth worker failed to start or stopped for good
        (every bounded restart used up)."""
        if self.preview_only or self.closing:
            return False
        if self.radio is None:
            return self.restart_at is None and self.restarts >= BLUETOOTH_RESTARTS
        return bool(getattr(self.radio, 'startup_error', None))

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
