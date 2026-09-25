"""Release-hardening regressions: robustness of the local index, provider
scheduling, per-row staleness, rendering bounds and app/GUI event wiring.
Everything is synthetic; no network, Bluetooth or real account data."""
import isolation  # noqa: F401  (test sandbox; must be the first import)
import json
import logging
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from meter import providers
from meter.app import (Application, LogLimiter, ProviderWorker, display_snapshot,
                       display_time, next_render, BLUETOOTH_RESTARTS)
from meter.gui import (BLUETOOTH_HELP, Desktop, bluetooth_help, plain, provider_summary,
                       selection_text)
from meter.setup_flow import SetupFlow
from meter.providers import (NotSetUp, ProviderError, RateLimited, parse_claude, parse_codex,
                             refresh)
from meter.render import compact, fit, font, percent, printable, render
from meter.tokens import TokenIndex, is_corrupt


def claude_line(identity, timestamp=None, output=5):
    return json.dumps({'type': 'assistant', 'timestamp': timestamp or time.time(),
                       'message': {'id': identity, 'model': 'claude-test',
                                   'usage': {'input_tokens': 10, 'output_tokens': output}}}) + '\n'


# -- Item 2: a damaged index never blocks startup ---------------------------
class CorruptIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'tokens.sqlite3'

    def test_garbage_file_is_discarded_and_rebuilt(self):
        self.path.write_bytes(b'this is not a database' * 200)
        Path(str(self.path) + '-journal').write_bytes(b'junk')
        index = TokenIndex(self.path)
        self.addCleanup(index.close)
        self.assertTrue(index.recovered)
        self.assertEqual(index.db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertFalse(Path(str(self.path) + '-journal').exists())
        logs = self.root / 'logs'
        logs.mkdir()
        (logs / 'a.jsonl').write_text(claude_line('m1'))
        self.assertEqual(index.scan([(logs, 'claude')]), {'claude'})
        self.assertEqual(index.total('claude', 0, time.time() + 10), 15)

    def test_unwritable_location_falls_back_to_memory(self):
        blocker = self.root / 'file'
        blocker.write_text('x')
        index = TokenIndex(blocker / 'tokens.sqlite3')  # Parent is a file.
        self.addCleanup(index.close)
        self.assertEqual(index.total('claude', 0, 1), 0)

    def test_corruption_during_use_is_rebuilt_by_worker(self):
        worker = ProviderWorker(self.root, lambda event: None)
        index = Mock()
        index.scan.side_effect = sqlite3.DatabaseError('database disk image is malformed')
        result, available, ok = worker._scan(index)
        index.rebuild.assert_called_once()
        self.assertEqual((available, ok), (set(), False))
        index.reset_mock()
        index.scan.side_effect = sqlite3.OperationalError('database is locked')
        worker._scan(index)
        index.rebuild.assert_not_called()

    def test_corruption_classification(self):
        self.assertTrue(is_corrupt(sqlite3.DatabaseError('file is not a database')))
        self.assertFalse(is_corrupt(sqlite3.OperationalError('database is locked')))
        self.assertFalse(is_corrupt(ValueError()))

    def test_worker_emits_quotas_when_scan_fails_completely(self):
        events, done = [], threading.Event()
        holder = {}

        class Broken:
            activity = {}
            def __init__(self, path): self.db = self
            def scan(self): raise RuntimeError('boom')
            def total(self, *a, **k): raise AssertionError('not reached')
            def close(self): pass

        def emit(event):
            events.append(event)
            if event['event'] == 'snapshot':
                holder['worker'].stop.set()
                done.set()

        cache = {'providers': {'claude': {'rows': parse_claude({'five_hour': {'utilization': 40}}),
                                          'fetched_at': time.time(), 'next_poll': time.time() + 60}}}
        with patch('meter.app.TokenIndex', Broken), patch('meter.app.refresh', return_value=cache), \
                patch('meter.app.account_fingerprints', return_value={}) as accounts:
            worker = holder['worker'] = ProviderWorker(self.root, emit)
            worker.start()
            self.assertTrue(done.wait(3))
            worker.close()
        accounts.assert_called()
        snapshot = events[-1]['snapshot']
        self.assertEqual(snapshot['rows'][0]['used'], 40)
        self.assertIsNone(snapshot['rows'][0]['tokens'])


# -- Items 3 and 6: per-file errors and incremental scanning ---------------
class ScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / 'projects'
        self.logs.mkdir()
        self.index = TokenIndex(self.root / 'tokens.sqlite3')
        self.addCleanup(self.index.close)

    def test_unreadable_and_undecodable_files_do_not_abort_scan(self):
        (self.logs / 'good.jsonl').write_text(claude_line('good'))
        (self.logs / 'binary.jsonl').write_bytes(b'\xff\xfe"usage"\x80\n' + claude_line('after').encode())
        locked = self.logs / 'locked.jsonl'
        locked.write_text(claude_line('locked'))
        real_open = Path.open

        def guarded(path, *args, **kwargs):
            if path.name == 'locked.jsonl':
                raise PermissionError('synthetic')
            return real_open(path, *args, **kwargs)

        with patch.object(Path, 'open', guarded):
            self.assertEqual(self.index.scan([(self.logs, 'claude')]), {'claude'})
        self.assertEqual(self.index.skipped, 1)
        self.assertEqual(self.index.total('claude', 0, time.time() + 10), 30)
        # The skipped file is retried and counted once it becomes readable.
        self.index.scan([(self.logs, 'claude')])
        self.assertEqual(self.index.total('claude', 0, time.time() + 10), 45)

    def test_unchanged_files_are_not_reopened(self):
        (self.logs / 'a.jsonl').write_text(claude_line('a'))
        self.index.scan([(self.logs, 'claude')])
        with patch.object(Path, 'open', side_effect=AssertionError('reopened')):
            self.index.scan([(self.logs, 'claude')])
        with (self.logs / 'a.jsonl').open('a') as handle:
            handle.write(claude_line('b'))
        self.index.scan([(self.logs, 'claude')])
        self.assertEqual(self.index.total('claude', 0, time.time() + 10), 30)
        self.assertGreater(self.index.activity['claude'], 0)

    def test_codex_session_metadata_is_read_once_and_fork_order_kept(self):
        sessions = self.root / 'sessions'
        sessions.mkdir()
        now = time.time()
        usage = {'type': 'event_msg', 'timestamp': now, 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 100, 'output_tokens': 10},
            'total_token_usage': {'input_tokens': 100, 'output_tokens': 10}}}}
        # The child sorts first by name; its parent must still be scanned first.
        (sessions / 'a-child.jsonl').write_text('\n'.join(map(json.dumps, [
            {'type': 'session_meta', 'payload': {'id': 'child', 'forked_from_id': 'parent',
                                                 'instructions': 'x' * 100000}},
            {**usage, 'timestamp': now + 50}])) + '\n')
        (sessions / 'b-parent.jsonl').write_text('\n'.join(map(json.dumps, [
            {'type': 'session_meta', 'payload': {'id': 'parent'}}, usage])) + '\n')
        with patch('meter.tokens._session_meta', wraps=__import__('meter.tokens').tokens._session_meta) as meta:
            self.index.scan([(sessions, 'codex')])
            self.index.scan([(sessions, 'codex')])
        self.assertEqual(meta.call_count, 2)  # Once per file, not once per scan.
        self.assertEqual(self.index.total('codex', 0, now + 100), 110)
        self.assertEqual(self.index.total('codex', now + 10, now + 100), 0)

    def test_deep_fork_chain_does_not_recurse(self):
        sessions = self.root / 'sessions'
        sessions.mkdir()
        for i in range(1500):
            payload = {'id': f's{i}'}
            if i:
                payload['forked_from_id'] = f's{i - 1}'
            (sessions / f'{i}.jsonl').write_text(json.dumps({'type': 'session_meta', 'payload': payload}) + '\n')
        self.assertEqual(self.index.scan([(sessions, 'codex')]), {'codex'})

    def test_old_codex_date_folders_are_pruned_and_rows_forgotten(self):
        sessions = self.root / 'sessions'
        old = sessions / '2001/01/01'
        old.mkdir(parents=True)
        (old / 'x.jsonl').write_text('{}\n')
        (sessions / 'recent.jsonl').write_text('{}\n')
        with patch('meter.tokens.Path.stat', autospec=True, side_effect=Path.stat) as stat:
            self.index.scan([(sessions, 'codex')])
        self.assertNotIn(old / 'x.jsonl', [c.args[0] for c in stat.call_args_list])
        (sessions / 'recent.jsonl').unlink()
        self.index.scan([(sessions, 'codex')])
        self.assertEqual(self.index.db.execute('SELECT COUNT(*) FROM files').fetchone()[0], 0)


# -- Items 5, 6 and 7: provider scheduling and identification ---------------
class ProviderScheduleTests(unittest.TestCase):
    def test_forced_refresh_bypasses_error_backoff(self):
        calls = []

        def signed_out():
            calls.append(1)
            raise providers.SignInExpired('Claude Code sign-in expired. Open Claude Code once to renew it.')

        cache = refresh({}, 1000, {'claude': signed_out})
        self.assertEqual(cache['providers']['claude']['next_poll'], 1300)
        cache = refresh(cache, 1100, {'claude': lambda: parse_claude({})}, force=True)
        self.assertIsNone(cache['providers']['claude']['error'])

    def test_forced_refresh_respects_rate_limit_and_press_floor(self):
        def limited():
            raise RateLimited('Claude HTTP 429: slow down', retry_after=1200)
        cache = refresh({}, 1000, {'claude': limited})
        self.assertEqual(cache['providers']['claude']['next_poll'], 2200)
        good = Mock(return_value=parse_claude({}))
        refresh(cache, 1100, {'claude': good}, force=True)
        good.assert_not_called()
        cache = refresh({}, 1000, {'claude': good})
        refresh(cache, 1005, {'claude': good}, force=True)
        self.assertEqual(good.call_count, 1)

    def test_codex_idle_interval_and_wake(self):
        read = Mock(return_value=parse_codex({}))
        cache = refresh({}, 1000, {'codex': read}, relaxed={'codex': 900})
        self.assertEqual(cache['providers']['codex']['next_poll'], 1900)
        refresh(cache, 1100, {'codex': read}, relaxed={'codex': 900})
        self.assertEqual(read.call_count, 1)
        refresh(cache, 1100, {'codex': read}, relaxed={'codex': 900}, wake={'codex'})
        self.assertEqual(read.call_count, 2)

    def test_clock_moving_backwards_does_not_freeze_polling(self):
        read = Mock(return_value=parse_claude({}))
        cache = refresh({}, 10 ** 9, {'claude': read})
        refresh(cache, 10 ** 9 - 10 * 86400, {'claude': read})
        self.assertEqual(read.call_count, 2)

    def test_not_set_up_clears_rows(self):
        cache = refresh({}, 1000, {'codex': lambda: parse_codex({'rateLimits': {'planType': 'pro'}})})
        def missing():
            raise NotSetUp('Codex is not installed')
        cache = refresh(cache, 2000, {'codex': missing})
        entry = cache['providers']['codex']
        self.assertEqual(entry['error_code'], 'not_set_up')
        self.assertNotIn('rows', entry)

    def test_unexpected_errors_are_plain(self):
        import requests
        for error in (requests.ConnectionError('secret body'), ValueError('secret'), KeyError('x')):
            code, message = providers.describe_error('claude', error)
            self.assertNotIn('secret', message)
            self.assertNotRegex(message, r'Error|Exception')

    def test_user_agent_is_sweetmeter_and_token_stays_in_header(self):
        response = SimpleNamespace(status_code=200, headers={}, json=lambda: {'five_hour': {'utilization': 3}},
                                   close=lambda: None)
        with patch.object(providers, 'claude_credentials', return_value={'accessToken': 'synthetic-token'}), \
                patch.object(providers, 'get_version', return_value='2026.9.9'), \
                patch.object(providers.requests, 'get', return_value=response) as get:
            rows = providers.fetch_claude()
        headers = get.call_args.kwargs['headers']
        self.assertEqual(headers['User-Agent'], 'Sweetmeter/2026.9.9')
        self.assertNotIn('claude-code', json.dumps(headers).lower())
        self.assertEqual(headers['anthropic-beta'], 'oauth-2025-04-20')
        self.assertNotIn('synthetic-token', json.dumps(rows))

    def test_codex_signed_out_is_not_set_up(self):
        from test_provider_portability import FakeAppServer
        self.addCleanup(providers.close_codex)
        answers = {'account/rateLimits/read': {'error': {'message': 'synthetic'}},
                   'account/read': {'account': None, 'requiresOpenaiAuth': True}}
        with patch.object(providers, 'codex_command', return_value=['codex', 'app-server']), \
                patch.object(providers.sys, 'platform', 'linux'), \
                patch.object(providers.subprocess, 'Popen', side_effect=lambda *a, **k: FakeAppServer(answers)):
            with self.assertRaises(NotSetUp):
                providers.fetch_codex()

    def test_missing_codex_is_not_set_up(self):
        with patch.object(providers.shutil, 'which', return_value=None), \
                patch.object(providers, '_codex_candidates', return_value=iter(())), \
                patch.dict(os.environ, {'SWEETMETER_CODEX_PATH': ''}):
            with self.assertRaises(NotSetUp):
                providers.codex_command()

    def test_http_failures_map_to_plain_messages_without_token(self):
        for status, kind in ((401, providers.SignInExpired), (429, RateLimited), (503, ProviderError)):
            response = SimpleNamespace(status_code=status, headers={'Retry-After': '120'}, close=lambda: None)
            with self.subTest(status=status), \
                    patch.object(providers, 'claude_credentials', return_value={'accessToken': 'synthetic-token'}), \
                    patch.object(providers.requests, 'get', return_value=response):
                with self.assertRaises(kind) as caught:
                    providers.fetch_claude()
                self.assertNotIn('synthetic-token', str(caught.exception))


# -- Item 4: staleness per provider/row --------------------------------------
class RowStateTests(unittest.TestCase):
    def test_missing_codex_is_absent_and_claude_is_not_stale(self):
        now = 10_000
        cache = {'providers': {
            'claude': {'rows': parse_claude({'five_hour': {'utilization': 20, 'resets_at': now + 3600}}),
                       'fetched_at': now - 30, 'next_poll': now + 30, 'error': None},
            'codex': {'error': 'Codex is not installed', 'error_code': 'not_set_up', 'next_poll': now + 300}}}
        snapshot = display_snapshot(cache, None, set(), now)
        states = [row['state'] for row in snapshot['rows']]
        self.assertEqual(states, ['ok', 'ok', 'ok', 'absent'])
        self.assertFalse(snapshot['stale'])
        self.assertIsNone(snapshot['rows'][3]['used'])
        self.assertEqual(snapshot['rows'][3]['subscription_label'], '')

    def test_error_marks_only_that_provider_stale(self):
        now = 10_000
        cache = {'providers': {
            'claude': {'rows': parse_claude({}), 'fetched_at': now - 30, 'next_poll': now + 30},
            'codex': {'rows': parse_codex({}), 'fetched_at': now - 60, 'next_poll': now + 240,
                      'error': 'Codex did not answer in time.'}}}
        states = [row['state'] for row in display_snapshot(cache, None, set(), now)['rows']]
        self.assertEqual(states, ['ok', 'ok', 'ok', 'stale'])

    def test_relaxed_codex_poll_is_not_stale(self):
        now = 10_000
        cache = {'providers': {'codex': {'rows': parse_codex({}), 'fetched_at': now - 1000,
                                         'next_poll': now - 100 + 900}}}
        self.assertEqual(display_snapshot(cache, None, set(), now)['rows'][-1]['state'], 'ok')
        cache['providers']['codex']['fetched_at'] = now - 5000
        self.assertEqual(display_snapshot(cache, None, set(), now)['rows'][-1]['state'], 'stale')

    def test_render_marks_only_stale_rows(self):
        rows = parse_claude({}) + parse_codex({})
        for row in rows:
            row['state'] = 'ok'
        rows[3]['state'] = 'absent'
        clean = render(dict(rows=rows, as_of=1789870000, stale=True))
        rows[0]['state'] = 'stale'
        marked = render(dict(rows=rows, as_of=1789870000, stale=False))
        diff = [(x, y) for x in range(250) for y in range(15, 42)
                if clean.getpixel((x, y)) != marked.getpixel((x, y))]
        self.assertTrue(diff)
        self.assertTrue(all(y < 30 for _x, y in diff))


# -- Item 8: rendering bounds -------------------------------------------------
class RenderBoundsTests(unittest.TestCase):
    def test_compact_rolls_over_units(self):
        self.assertEqual(compact(999_999), '1M')
        self.assertEqual(compact(999_999_999), '1B')
        self.assertEqual(compact(999_499), '999.5K')
        self.assertEqual(compact(999), '999')
        self.assertEqual(compact(1_280_000), '1.28M')
        self.assertEqual(compact(182_600_000), '182.6M')
        self.assertEqual(compact(-5), '0')
        self.assertEqual(compact(float('nan')), '--')
        self.assertEqual(compact(True), '--')

    def test_percent_format(self):
        self.assertEqual(percent(38), '38%')
        self.assertEqual(percent(.5), '0.5%')
        self.assertEqual(percent(99.96), '100%')
        self.assertEqual(percent(150.25), '150%')
        self.assertEqual(percent(12345), '999+%')
        self.assertEqual(percent(None), '--')

    def test_large_usage_never_touches_bar(self):
        for used in (100, 150.25, 999, 12345, 99.95):
            rows = parse_claude({}) + parse_codex({})
            rows[0]['used'] = used
            screen = render(dict(rows=rows, as_of=1789870000, stale=False))
            # Columns between the bar outline (x<=201) and the percent box (x>=205).
            gap = [screen.getpixel((x, y)) for x in range(202, 205) for y in range(15, 40)]
            self.assertTrue(all(gap), used)

    def test_long_firmware_and_plan_stay_in_boxes(self):
        rows = parse_claude({}, 'ENTERPRISE WITH A VERY LONG PLAN NAME 日本語') + parse_codex({})
        device = {'firmware': '2026.12.4294967295-' + 'x' * 200 + '‮\x00'}
        screen = render(dict(rows=rows, as_of=1789870000, stale=False, device=device))
        # Header is black; firmware text (white) must end before "BT" at x=90.
        self.assertTrue(all(screen.getpixel((x, y)) == 0 for x in range(87, 90) for y in range(0, 12)))
        # The plan label ends before the bar at x=80.
        self.assertTrue(all(screen.getpixel((x, y)) for x in range(77, 80) for y in range(28, 40)))

    def test_printable_and_fit(self):
        self.assertEqual(printable('MAX 20×\x00日本'), 'MAX 20×')
        draw = ImageDraw.Draw(Image.new('1', (10, 10)))
        text, face = fit(draw, 'W' * 50, 40, (9, 8))
        self.assertLessEqual(draw.textlength(text, font=face), 40)
        self.assertTrue(text.endswith('…'))

    def test_display_time_is_next_minute_and_render_leads_it(self):
        self.assertEqual(display_time(120.0), 180)
        self.assertEqual(display_time(179.9), 180)
        self.assertEqual(next_render(100, 60), 105)
        self.assertEqual(next_render(118, 60), 165)
        self.assertEqual(next_render(100, 300), 285)


# -- Items 1 and 9: app wiring -----------------------------------------------
class FakeRadio:
    instances = []

    def __init__(self, state_dir):
        self.events = queue.Queue()
        self.ready = threading.Event()
        self.ready.set()
        self.startup_error = None
        self.health = 'ok'
        self.sent, self.forgot, self.closed = [], 0, False
        FakeRadio.instances.append(self)

    def poll(self, timeout=0):
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    def send(self, frame):
        self.sent.append(frame)

    def forget(self):
        self.forgot += 1
        self.events.put({'event': 'forgotten'})

    def close(self):
        self.closed = True

    def update_notice(self, code):
        pass

    def rename(self, data):
        self.renamed = getattr(self, 'renamed', []) + [data]

    class store:
        """This computer is registering with meter AA only."""
        @staticmethod
        def related(address):
            return address == 'AA'


class AppWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        FakeRadio.instances = []
        self.app = Application(self.tmp.name, bluetooth_factory=FakeRadio)
        self.app.provider = Mock()
        self.app.updates = Mock()
        self.app.updates.lock = threading.Lock()
        self.app.radio = self.app._new_radio()

    def status(self, trusted, **status):
        return {'event': 'status', 'device_id': 'AA', 'trusted': trusted,
                'status': {'protocol': 4, 'firmware': '2026.9.1', **status}}

    def test_untrusted_status_is_ignored_for_device_updates_and_disk(self):
        self.app.radio.events.put(self.status(False, interval=300))
        events = self.app.pump()
        self.assertEqual(self.app.device, {})
        self.app.updates.set_device.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / 'bluetooth.json').exists())
        self.assertEqual(events[0]['event'], 'status')

    def test_trusted_status_is_saved_and_merged(self):
        path = Path(self.tmp.name) / 'bluetooth.json'
        path.write_text(json.dumps({'device_id': 'AA', 'pairing': 'kept'}))
        self.app.radio.events.put(self.status(True, interval=300))
        self.app.pump()
        self.assertEqual(self.app.device['interval'], 300)
        self.app.updates.set_device.assert_called_once()
        saved = json.loads(path.read_text())
        self.assertEqual(saved['pairing'], 'kept')
        self.assertEqual(saved['status']['interval'], 300)

    def test_meter_newer_than_this_app_checks_for_an_update_once(self):
        self.app.version = '2026.9.19'
        self.app.radio.events.put(self.status(True, firmware='2026.9.25'))
        self.app.radio.events.put(self.status(True, firmware='2026.9.25'))
        events = self.app.pump()
        newer = [e for e in events if e['event'] == 'meter_newer']
        self.assertEqual(newer, [{'event': 'meter_newer', 'firmware': '2026.9.25', 'companion': '2026.9.19'}])
        self.app.updates.check.assert_called_once_with()

    def test_only_the_paired_meter_or_one_being_paired_counts_as_newer(self):
        self.app.version = '2026.9.19'
        self.app.radio.events.put(self.status(False, firmware='2026.9.25'))  # a stranger's meter nearby
        self.app.radio.events.put(self.status(True, firmware='2026.9.19'))   # same version
        self.app.radio.events.put(self.status(True, firmware='garbage'))
        self.assertFalse([e for e in self.app.pump() if e['event'] == 'meter_newer'])
        self.app.updates.check.assert_not_called()
        # An open computer list alone is not enough: anyone nearby can advertise one.
        stranger = self.status(False, firmware='2026.9.26', menu=True)
        stranger['device_id'] = 'BB'
        self.app.radio.events.put(stranger)
        self.assertFalse([e for e in self.app.pump() if e['event'] == 'meter_newer'])
        # The meter this computer is registering with while its list is open counts.
        self.app.radio.events.put(self.status(False, firmware='2026.9.25', menu=True))
        self.assertTrue([e for e in self.app.pump() if e['event'] == 'meter_newer'])
        self.app.updates.check.assert_called_once_with()

    def test_rename_meter_encodes_the_name_or_explains(self):
        self.app.rename_meter(' 书房 ')
        self.assertEqual(self.app.radio.renamed, ['书房'.encode()])
        self.app.rename_meter('')
        self.assertEqual(self.app.radio.renamed[-1], b'')
        with self.assertRaises(ValueError):
            self.app.rename_meter('x' * 17)
        self.app.radio = None
        with self.assertRaises(RuntimeError):
            self.app.rename_meter('Desk')

    def test_forget_clears_device(self):
        self.app.device = {'firmware': 'x'}
        self.app.forget_meter()
        self.app.pump()
        self.assertEqual(self.app.radio.forgot, 1)
        self.assertEqual(self.app.device, {})
        self.app.updates.set_device.assert_called_with({}, False)

    def test_unexpected_exit_restarts_a_bounded_number_of_times(self):
        self.app.last_frame = b'\0' * 4000
        shown = []
        for attempt in range(BLUETOOTH_RESTARTS):
            self.app.radio.events.put({'event': 'exit'})
            shown += self.app.pump()
            self.assertIsNone(self.app.radio)
            self.app.restart_at = 0
            shown += self.app.pump()
            self.assertIs(self.app.updates.radio, self.app.radio)
            self.assertEqual(self.app.radio.sent, [b'\0' * 4000])
        self.app.radio.events.put({'event': 'exit'})
        shown += self.app.pump()
        kinds = [event['event'] for event in shown]
        self.assertEqual(kinds.count('bluetooth_restarted'), BLUETOOTH_RESTARTS)
        self.assertEqual(kinds[-1], 'exit')
        self.assertEqual(len(FakeRadio.instances), BLUETOOTH_RESTARTS + 1)
        self.assertTrue(all(radio.closed for radio in FakeRadio.instances))

    def test_bluetooth_failed_only_after_restarts_are_used_up(self):
        self.assertFalse(self.app.bluetooth_failed)
        for _ in range(BLUETOOTH_RESTARTS):
            self.app.radio.events.put({'event': 'exit'})
            self.app.pump()
            self.assertFalse(self.app.bluetooth_failed)  # Restarting: still waiting.
            self.app.restart_at = 0
            self.app.pump()
        self.app.radio.events.put({'event': 'exit'})
        self.app.pump()
        self.assertTrue(self.app.bluetooth_failed)
        self.app.radio = FakeRadio(self.tmp.name)
        self.app.radio.startup_error = 'ImportError'
        self.assertTrue(self.app.bluetooth_failed)

    def test_exit_during_shutdown_is_passed_through(self):
        self.app.closing = True
        self.app.radio.events.put({'event': 'exit'})
        self.assertEqual([e['event'] for e in self.app.pump()], ['exit'])
        self.assertEqual(len(FakeRadio.instances), 1)

    def test_repeated_errors_are_logged_once_then_counted(self):
        with self.assertLogs(level='INFO') as logs:
            for _ in range(20):
                self.app.radio.events.put({'event': 'error', 'code': 'scan', 'error': 'Meter not found'})
                self.app.pump()
            self.app.radio.events.put({'event': 'connected', 'device_id': 'AA'})
            self.app.pump()
        joined = '\n'.join(logs.output)
        self.assertEqual(joined.count('Meter not found'), 2)  # First, then the summary.
        self.assertIn('repeated 19 more times', joined)

    def test_log_limiter_summarises_long_runs(self):
        clock = iter(range(0, 10_000, 100)).__next__
        limiter = LogLimiter(clock=clock, summary_after=600)
        with self.assertLogs(level='INFO') as logs:
            for _ in range(20):
                limiter.log(logging.WARNING, 'same')
        self.assertLess(len(logs.output), 6)


# -- Items 1 and 10: GUI texts ------------------------------------------------
class GuiTextTests(unittest.TestCase):
    def test_bluetooth_help_is_os_specific(self):
        for state in ('off', 'unauthorized', 'error'):
            texts = {bluetooth_help(state, platform) for platform in ('darwin', 'win32', 'linux')}
            self.assertEqual(len(texts), 3)
        self.assertIn('Privacy & Security', bluetooth_help('unauthorized', 'darwin'))
        self.assertIn('Bluetooth & devices', bluetooth_help('off', 'win32'))
        self.assertEqual(bluetooth_help('ok', 'darwin'), '')
        self.assertEqual(bluetooth_help('weird', 'linux'), BLUETOOTH_HELP['error']['linux'])

    def test_plain_hides_exception_names(self):
        self.assertEqual(plain('Bluetooth: BleakError', 'fallback'), 'fallback')
        self.assertEqual(plain('Update check failed: ValueError', 'fallback'), 'fallback')
        self.assertEqual(plain('Meter is out of range.'), 'Meter is out of range.')

    def test_selection_reasons(self):
        heading, text = selection_text('other_computer', 'Studio')
        self.assertIn('another computer', heading)
        self.assertIn('Studio', text)
        self.assertIn('this computer', selection_text('unpaired', None)[1])

    def test_provider_summary(self):
        text = provider_summary({'codex': {'error': 'x', 'code': 'not_set_up'},
                                 'claude': {'error': 'Claude HTTP 503: unavailable', 'code': 'error'}})
        self.assertIn('Codex: not set up', text)
        self.assertIn('Claude Code: Claude HTTP 503', text)
        self.assertEqual(provider_summary({'claude': {'error': None}}), '')

    def test_desktop_events_without_window(self):
        desktop = Desktop.__new__(Desktop)
        for name in ('connection', 'setup_heading', 'setup_help', 'provider_status', 'update_status'):
            setattr(desktop, name, Mock())
        desktop.progress_window = None
        desktop.event({'event': 'bluetooth_state', 'state': 'off'})
        self.assertIn('Bluetooth', desktop.setup_help.set.call_args.args[0])
        desktop.event({'event': 'selection_required', 'reason': 'other_computer', 'name': 'Mac'})
        self.assertIn('another computer', desktop.setup_heading.set.call_args.args[0])
        desktop.event({'event': 'error', 'code': 'x', 'error': 'Bluetooth: TimeoutError'})
        self.assertNotIn('TimeoutError', desktop.connection.set.call_args.args[0])
        desktop.event({'event': 'update_error', 'error': 'Update check failed: JSONDecodeError'})
        self.assertNotIn('JSONDecodeError', desktop.update_status.set.call_args.args[0])
        desktop.event({'event': 'status', 'trusted': False, 'status': {'protocol': 3}})
        desktop.update_status.set.assert_called_once()

    def test_desktop_explains_naming_and_pairing_results_without_window(self):
        desktop = Desktop.__new__(Desktop)
        for name in ('connection', 'setup_heading', 'setup_help', 'provider_status', 'update_status'):
            setattr(desktop, name, Mock())
        desktop.progress_window = None
        desktop.setup = SetupFlow('Mac')
        desktop.setup_window = None
        desktop.event({'event': 'connected', 'device_id': 'AA', 'name': 'Sweetmeter-CF24'})
        self.assertIn('Connected to Sweetmeter-CF24', desktop.connection.set.call_args.args[0])
        desktop.event({'event': 'renamed', 'ok': True, 'name': '书房'})
        self.assertEqual(desktop.connection.set.call_args.args[0], 'Meter renamed to 书房.')
        desktop.event({'event': 'renamed', 'ok': False, 'error': 'The meter could not save the name. Try again.'})
        self.assertIn('could not save', desktop.connection.set.call_args.args[0])
        desktop.event({'event': 'registration_failed', 'result': 5, 'error': 'The meter’s computer list is full.'})
        self.assertIn('list is full', desktop.connection.set.call_args.args[0])
        desktop.event({'event': 'meter_newer', 'firmware': '2026.9.25', 'companion': '2026.9.19'})
        self.assertIn('2026.9.25', desktop.update_status.set.call_args.args[0])
        self.assertEqual(desktop.setup.newer_firmware, ('2026.9.25', '2026.9.19'))  # the setup flow saw it too

    def test_desktop_rename_checks_connection_capability_and_name(self):
        desktop = Desktop.__new__(Desktop)
        desktop.root, desktop.connection = Mock(), Mock()
        desktop.setup = SetupFlow('Mac')
        desktop.app = SimpleNamespace(connected=False, rename_meter=Mock())
        with patch('meter.gui.messagebox') as box:
            self.assertFalse(desktop.rename('Desk'))
            self.assertIn('connected', box.showinfo.call_args.args[1])
            desktop.app.connected = True
            desktop.setup.handle({'event': 'connected', 'device_id': 'AA', 'name': 'Sweetmeter-CF24'})
            self.assertFalse(desktop.rename('Desk'))  # firmware without the name command
            self.assertIn('firmware update', box.showinfo.call_args.args[1])
            desktop.setup.handle({'event': 'status', 'trusted': True, 'status': {'protocol': 4, 'rename': 1}})
            desktop.app.rename_meter.side_effect = ValueError('The name is too long.')
            self.assertFalse(desktop.rename('x' * 17))
            self.assertEqual(box.showerror.call_args.args[1], 'The name is too long.')
            desktop.app.rename_meter.side_effect = None
            self.assertTrue(desktop.rename('Desk'))
        desktop.app.rename_meter.assert_called_with('Desk')
        self.assertTrue(desktop.setup.saving)
        self.assertTrue(desktop.setup.view().saving)

    def test_poll_survives_a_bad_event(self):
        desktop = Desktop.__new__(Desktop)
        desktop.running = True
        desktop.app = SimpleNamespace(state_dir=Path(tempfile.gettempdir()) / 'no-such-sweetmeter',
                                      pump=lambda: [{'event': 'registered'}, {'event': 'ack'}])
        desktop.root = Mock()
        desktop.event = Mock(side_effect=[KeyError('name'), None])
        with self.assertLogs(level='ERROR'):
            desktop.poll()
        self.assertEqual(desktop.event.call_count, 2)
        desktop.root.after.assert_called_once()


class StartAtLoginCheckboxTests(unittest.TestCase):
    """Changing "Start at login" never blocks the Tk thread."""

    def desktop(self, wanted):
        desktop = Desktop.__new__(Desktop)
        desktop.root = Mock()
        desktop.start_at_login = Mock(get=Mock(return_value=wanted))
        desktop.start_at_login_box = Mock()
        return desktop

    def drain(self, desktop, limit=5):
        """Run the Tk after() callbacks the handler scheduled (as Tk would)."""
        deadline = time.monotonic() + limit
        while desktop.root.after.call_args_list and time.monotonic() < deadline:
            delay, callback = desktop.root.after.call_args_list.pop(0).args
            time.sleep(delay / 1000)
            callback()

    def test_slow_change_runs_on_a_worker_and_the_ui_is_updated_later(self):
        release, threads = threading.Event(), []

        def slow(enabled):
            threads.append(threading.get_ident())
            release.wait(5)
        desktop = self.desktop(False)
        with patch('meter.installation.set_start_at_login', side_effect=slow):
            started = time.monotonic()
            desktop.toggle_start_at_login()
            self.assertLess(time.monotonic() - started, .5)  # Returned at once.
            desktop.start_at_login_box.configure.assert_called_with(state='disabled')
            desktop.toggle_start_at_login()  # A second click meanwhile is ignored.
            release.set()
            self.drain(desktop)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        desktop.start_at_login_box.configure.assert_called_with(state='normal')
        desktop.start_at_login.set.assert_not_called()

    def test_failure_restores_the_checkbox_and_explains_on_the_tk_thread(self):
        from meter.installation import InstallError
        desktop = self.desktop(True)
        with patch('meter.installation.set_start_at_login', side_effect=InstallError('Another setup is running.')), \
                patch('meter.gui.messagebox') as box:
            desktop.toggle_start_at_login()
            self.drain(desktop)
        desktop.start_at_login.set.assert_called_once_with(False)
        self.assertIn('Another setup is running.', box.showerror.call_args.args[1])


class AccountSwitchTests(unittest.TestCase):
    def test_changed_account_drops_cache_and_polls_immediately(self):
        read = Mock(return_value=parse_codex({'rateLimits': {'planType': 'pro'}}))
        cache = refresh({}, 1000, {'codex': read}, accounts={'codex': 'id:a'})
        entry = cache['providers']['codex']
        self.assertEqual((entry['account'], entry['account_since']), ('id:a', 0))
        # Same account inside the poll interval: not polled.
        refresh(cache, 1030, {'codex': read}, accounts={'codex': 'id:a'})
        self.assertEqual(read.call_count, 1)
        cache = refresh(cache, 1200, {'codex': read}, force=True, accounts={'codex': 'id:a'})
        cache['providers']['codex'].update(error='Codex HTTP 429', error_code='rate_limited', next_poll=5000)
        new = Mock(return_value=parse_codex({'rateLimits': {'planType': 'plus'}}))
        # A (confirmed) other account switches in the same cycle, dropping
        # the old backoff: the owner requirement is one refresh cycle.
        cache = refresh(cache, 1260, {'codex': new}, accounts={'codex': 'id:b'})
        entry = cache['providers']['codex']
        new.assert_called_once()
        self.assertEqual(entry['rows'][0]['subscription_label'], 'PLUS')
        self.assertIsNone(entry['error'])
        self.assertEqual((entry['account'], entry['account_since']), ('id:b', 1260))

    def test_switch_discards_rows_even_when_the_new_poll_fails(self):
        cache = refresh({}, 1000, {'claude': lambda: parse_claude({'five_hour': {'utilization': 90}}, 'MAX')},
                        accounts={'claude': 'id:a'})
        def offline():
            raise ProviderError("Can't reach Claude.")
        cache = refresh(cache, 1060, {'claude': offline}, accounts={'claude': 'id:b'})
        cache = refresh(cache, 1120, {'claude': offline}, accounts={'claude': 'id:b'})
        entry = cache['providers']['claude']
        self.assertNotIn('rows', entry)
        self.assertEqual(entry['account'], 'id:b')
        snapshot = display_snapshot(cache, None, set(), 1130)
        self.assertTrue(all(row['used'] is None and row['subscription_label'] == '--'
                            for row in snapshot['rows'][:3]))

    def test_unknown_identity_and_token_rotation(self):
        read = Mock(return_value=parse_claude({}))
        cache = refresh({}, 1000, {'claude': read}, accounts={'claude': 'token:1'})
        refresh(cache, 1010, {'claude': read}, accounts={'claude': None})
        self.assertEqual(read.call_count, 1)  # Unknown identity is not a switch.
        cache = refresh(cache, 1020, {'claude': read}, accounts={'claude': 'token:2'})
        cache = refresh(cache, 1030, {'claude': read}, accounts={'claude': 'token:2'})
        self.assertEqual(read.call_count, 2)  # Quota is re-read...
        self.assertEqual(cache['providers']['claude']['account_since'], 0)  # ...tokens not cut.

    def test_unknown_identity_never_switches_and_a_brief_other_kind_keeps_the_count(self):
        read = Mock(return_value=parse_claude({'five_hour': {'utilization': 40}}, 'MAX'))
        cache = refresh({}, 1000, {'claude': read}, accounts={'claude': 'id:a'})
        cache['providers']['claude']['account_since'] = 500
        cache['providers']['claude']['account_candidate'] = 'id:z'  # Written by an older version.
        cache = refresh(cache, 1060, {'claude': read}, accounts={'claude': None})
        entry = cache['providers']['claude']
        self.assertEqual((entry['account'], entry['account_since']), ('id:a', 500))
        self.assertNotIn('account_candidate', entry)
        # A confirmed token fingerprint (config lost its ids) re-reads the
        # quota; the ids coming back resume the original token count.
        cache = refresh(cache, 1120, {'claude': read}, accounts={'claude': 'token:x'})
        cache = refresh(cache, 1180, {'claude': read}, accounts={'claude': 'id:a'})
        entry = cache['providers']['claude']
        self.assertEqual((entry['account'], entry['account_since']), ('id:a', 500))
        self.assertIn('rows', entry)

    def test_changed_fingerprint_is_confirmed_within_the_same_cycle(self):
        readings = {'claude': ['id:b', 'id:b'], 'codex': ['app:2', 'app:3']}
        waits = []

        def reader(name):
            return lambda salt, fresh=False, probe=True: readings[name].pop(0)
        with patch.dict(providers.ACCOUNT_READERS, {'claude': reader('claude'), 'codex': reader('codex')}):
            result = providers.account_fingerprints(b's' * 32, {'claude': 'id:a', 'codex': 'app:1'},
                                                    wait=waits.append)
        # One short settle delay, not a whole cycle; a stable reading is the
        # new account, a reading that changed again (mid-write) is unknown.
        self.assertEqual(waits, [providers.ACCOUNT_SETTLE])
        self.assertLessEqual(providers.ACCOUNT_SETTLE, 5)
        self.assertEqual(result, {'claude': 'id:b', 'codex': None})

    def test_unchanged_fingerprint_needs_no_second_read(self):
        calls = []

        def read(salt, fresh=False, probe=True):
            calls.append(fresh)
            return 'id:a'
        with patch.dict(providers.ACCOUNT_READERS, {'claude': read}):
            result = providers.account_fingerprints(b's' * 32, {'claude': 'id:a'}, names=('claude',),
                                                    wait=lambda seconds: self.fail('no wait expected'))
        self.assertEqual((result, calls), ({'claude': 'id:a'}, [False]))

    def test_half_written_config_is_re_read_without_the_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / '.claude.json'
            config.write_text(json.dumps({'oauthAccount': {'accountUuid': 'a'}}))
            salt = b'h' * 32
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=home):
                known = providers.claude_account(salt)

                def rewrite(seconds):
                    # The CLI finishes writing another account during the settle delay.
                    config.write_text(json.dumps({'oauthAccount': {'accountUuid': 'ccc'}}))
                config.write_text(json.dumps({'oauthAccount': {'accountUuid': 'bb'}}))
                result = providers.account_fingerprints(salt, {'claude': known}, names=('claude',),
                                                        wait=rewrite)
                self.assertIsNone(result['claude'])  # Changed again while settling: unknown.
                result = providers.account_fingerprints(salt, {'claude': known}, names=('claude',),
                                                        wait=lambda seconds: None)
                self.assertNotIn(result['claude'], (None, known))  # Stable: the new account.

    def test_sign_out_is_immediate_and_signing_back_in_keeps_the_count(self):
        read = Mock(return_value=parse_claude({}))
        cache = refresh({}, 1000, {'claude': read}, accounts={'claude': 'id:a'})
        cache['providers']['claude']['account_since'] = 400
        cache = refresh(cache, 1060, {'claude': read}, accounts={'claude': 'signed-out'})
        self.assertEqual(cache['providers']['claude']['account'], 'signed-out')
        cache = refresh(cache, 1120, {'claude': read}, accounts={'claude': 'id:a'})
        entry = cache['providers']['claude']
        self.assertEqual((entry['account'], entry['account_since']), ('id:a', 400))
        # Another account after sign-out counts from its own start.
        cache = refresh(cache, 1180, {'claude': read}, accounts={'claude': 'signed-out'})
        cache = refresh(cache, 1240, {'claude': read}, accounts={'claude': 'id:b'})
        self.assertEqual(cache['providers']['claude']['account_since'], 1240)

    def test_tokens_count_from_the_switch(self):
        now = 100_000
        rows = parse_claude({'five_hour': {'utilization': 5, 'resets_at': now + 3600}})
        cache = {'providers': {'claude': {'rows': rows, 'fetched_at': now, 'next_poll': now + 60,
                                          'account': 'id:b', 'account_since': now - 600}}}
        starts = []
        display_snapshot(cache, None, {'claude'}, now,
                         totals=lambda name, start, end, fable=False: starts.append(start) or 0)
        self.assertEqual(starts[0], now - 600)  # Not the window start (now - 14400).

    def test_fingerprints_are_salted_and_contain_no_ids_or_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / '.claude.json').write_text(json.dumps({'oauthAccount': {
                'accountUuid': 'acct-123', 'organizationUuid': 'org-9', 'emailAddress': 'a@example.invalid'}}))
            codex = home / '.codex'
            codex.mkdir()
            (codex / 'auth.json').write_text(json.dumps({'tokens': {
                'account_id': 'chatgpt-acct', 'refresh_token': 'synthetic-refresh'}}))
            with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=home), \
                    patch.object(providers, 'codex_command', side_effect=NotSetUp('Codex is not installed')):
                first = providers.account_fingerprints(b'a' * 32)
                self.assertEqual(first, providers.account_fingerprints(b'a' * 32))
                self.assertNotEqual(first, providers.account_fingerprints(b'b' * 32))
                blob = json.dumps(first)
                for secret in ('acct-123', 'org-9', 'chatgpt-acct', 'synthetic-refresh', 'example'):
                    self.assertNotIn(secret, blob)
                (home / '.claude.json').write_text(json.dumps({'oauthAccount': {
                    'accountUuid': 'acct-456', 'organizationUuid': 'org-9'}}))
                (codex / 'auth.json').unlink()
                second = providers.account_fingerprints(b'a' * 32)
                self.assertNotEqual(first['claude'], second['claude'])
                self.assertEqual(second['codex'], 'signed-out')  # Not installed, no login file.

    def test_worker_persists_salt_privately(self):
        from meter.app import account_salt
        with tempfile.TemporaryDirectory() as folder:
            salt = account_salt(folder)
            self.assertEqual(salt, account_salt(folder))
            if os.name == 'posix':
                self.assertEqual((Path(folder) / 'account-salt').stat().st_mode & 0o077, 0)


class SlowProviderTests(unittest.TestCase):
    """A hung Codex never delays Claude's rows or the Refresh button's frame."""

    def test_hung_codex_never_delays_claude_or_the_refresh_button(self):
        from meter import app as app_module
        release, snapshots = threading.Event(), queue.Queue()
        codex_calls = []

        def codex():
            codex_calls.append(time.monotonic())
            release.wait(20)
            return parse_codex({'rateLimits': {'primary': {'windowDurationMins': 10080, 'usedPercent': 44}}})
        claude = Mock(return_value=parse_claude({'five_hour': {'utilization': 7}}))
        with tempfile.TemporaryDirectory() as folder:
            now = time.time()
            old_codex = parse_codex({'rateLimits': {'primary': {'windowDurationMins': 10080, 'usedPercent': 33}}})
            (Path(folder) / 'providers.json').write_text(json.dumps({'providers': {'codex': {
                'rows': old_codex, 'fetched_at': now - 90, 'next_poll': now - 30}}}))
            with patch.object(providers, 'fetch_claude', claude), patch.object(providers, 'fetch_codex', codex), \
                    patch('meter.app.account_fingerprints', return_value={}), patch('meter.app.close_codex'), \
                    patch.object(app_module, 'FIRST_FRAME_WAIT', .5):
                worker = ProviderWorker(folder, lambda event: event['event'] == 'snapshot' and snapshots.put(
                    (time.monotonic(), event['snapshot'])))
                started = time.monotonic()
                worker.start()
                try:
                    self._exercise(worker, release, snapshots, started, codex_calls)
                finally:
                    # Close inside the folder: Windows cannot delete an open SQLite file.
                    release.set()
                    worker.close()

    def _exercise(self, worker, release, snapshots, started, codex_calls):
        shown_at, first = snapshots.get(timeout=5)
        self.assertLess(shown_at - started, 2.5)
        rows = {row['key']: row for row in first['rows']}
        self.assertEqual(rows['five_hour']['used'], 7)  # Claude's fresh rows...
        self.assertEqual(rows['codex']['used'], 33)     # ...with Codex's previous ones.
        # Refresh button while Codex still hangs: a frame at once, and
        # the hung provider is not asked a second time.
        pressed = time.monotonic()
        worker.force.set()
        shown_at, _ = snapshots.get(timeout=5)
        self.assertLess(shown_at - pressed, 2.5)
        self.assertEqual(len(codex_calls), 1)
        # The late answer is shown as soon as it arrives.
        answered = time.monotonic()
        release.set()
        shown_at, late = snapshots.get(timeout=5)
        self.assertLess(shown_at - answered, 2)
        self.assertEqual({row['key']: row for row in late['rows']}['codex']['used'], 44)

    def test_identity_probe_is_skipped_while_codex_is_in_backoff(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as folder, \
                patch('meter.app.account_fingerprints', return_value={}) as accounts, \
                patch('meter.app.refresh', return_value={'providers': {}}):
            worker = ProviderWorker(folder, lambda event: None)
            backoff = {'error': 'Codex did not answer in time.', 'attempted_at': now - 60, 'next_poll': now + 240}
            worker._poll('codex', backoff, now, False, 60, b's' * 32)
            self.assertIs(accounts.call_args.kwargs['probe'], False)
            worker._poll('codex', backoff, now, True, 60, b's' * 32)  # Refresh button polls it anyway.
            self.assertIs(accounts.call_args.kwargs['probe'], True)
            worker._poll('codex', {'next_poll': now - 1, 'account': 'app:1'}, now, False, 60, b's' * 32)
            self.assertIs(accounts.call_args.kwargs['probe'], True)
            self.assertEqual(accounts.call_args.args[1], {'codex': 'app:1'})
        with patch.object(providers, 'codex_session', side_effect=AssertionError('no app-server')):
            self.assertIsNone(providers.codex_account(b's' * 32, probe=False))


class PollCadenceTests(unittest.TestCase):
    """The owner requirement: both quotas refresh about once a minute."""

    def run_worker_once(self, folder, device=None):
        done, holder = threading.Event(), {}

        def emit(event):
            if event['event'] == 'snapshot':
                holder['worker'].stop.set()
                done.set()

        claude = Mock(return_value=parse_claude({'five_hour': {'utilization': 1}}))
        codex = Mock(return_value=parse_codex({'rateLimits': {'primary': {'windowDurationMins': 10080,
                                                                           'usedPercent': 2}}}))
        with patch.object(providers, 'fetch_claude', claude), patch.object(providers, 'fetch_codex', codex), \
                patch('meter.app.account_fingerprints', return_value={}), \
                patch('meter.app.close_codex') as close:
            worker = holder['worker'] = ProviderWorker(folder, emit, lambda: device or {})
            worker.start()
            self.assertTrue(done.wait(5))
            worker.close()
        close.assert_called_once()  # The long-lived app-server stops with the worker.
        return json.loads((Path(folder) / 'providers.json').read_text())['providers'], claude, codex

    def test_codex_is_polled_every_minute_like_claude(self):
        with tempfile.TemporaryDirectory() as folder:
            entries, claude, codex = self.run_worker_once(folder)
        claude.assert_called_once()
        codex.assert_called_once()
        for name in ('claude', 'codex'):
            self.assertEqual(entries[name]['next_poll'] - entries[name]['fetched_at'], 60, name)

    def test_idle_codex_is_polled_again_after_a_minute(self):
        read = Mock(return_value=parse_codex({}))
        cache = refresh({}, 1000, {'codex': read}, interval=60)
        refresh(cache, 1030, {'codex': read}, interval=60)
        self.assertEqual(read.call_count, 1)
        refresh(cache, 1061, {'codex': read}, interval=60)  # No local activity needed.
        self.assertEqual(read.call_count, 2)
        refresh(cache, 1065, {'codex': read}, interval=60, force=True)  # Refresh button: at once.
        self.assertEqual(read.call_count, 3)


class MultiComputerTests(AppWiringTests):
    def test_other_meter_status_is_not_reused_on_connect(self):
        self.app.radio.events.put(self.status(True, interval=300))
        self.app.pump()
        self.app.radio.events.put({'event': 'connected', 'device_id': 'BB'})
        self.app.pump()
        self.assertEqual(self.app.device, {})
        self.app.updates.set_device.assert_called_with({}, True, 'BB')
        self.app.provider.redraw.set.assert_called()

    def test_reconnect_to_same_meter_redraws_current_dashboard(self):
        self.app.radio.events.put(self.status(True))
        self.app.radio.events.put({'event': 'connected', 'device_id': 'AA'})
        self.app.radio.events.put({'event': 'disconnected'})
        self.app.radio.events.put({'event': 'connected', 'device_id': 'AA'})
        self.app.pump()
        self.assertEqual(self.app.device['firmware'], '2026.9.1')
        self.assertEqual(self.app.provider.redraw.set.call_count, 2)

    def test_update_success_needs_ack_from_updated_meter(self):
        self.app.radio.events.put(self.status(True))
        self.app.pump()
        self.app.events.put({'event': 'firmware_verified', 'version': '2026.9.2'})
        self.app.pump()
        self.app.radio.events.put({'event': 'connected', 'device_id': 'BB'})
        self.app.radio.events.put({'event': 'ack', 'sequence': 1, 'crc32': '0', 'ack': 'FULL'})
        self.assertNotIn('update_success', [e['event'] for e in self.app.pump()])
        self.app.radio.events.put({'event': 'connected', 'device_id': 'AA'})
        self.app.radio.events.put({'event': 'ack', 'sequence': 2, 'crc32': '0', 'ack': 'FULL'})
        self.assertIn('update_success', [e['event'] for e in self.app.pump()])


if __name__ == '__main__':
    unittest.main()


class SetupWindowOpeningTests(unittest.TestCase):
    """The setup window opens until a meter proved a pairing secret, even in the background."""
    def desktop(self, meters, *, background=True):
        from meter.bluetooth import PairingStore
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = PairingStore(Path(folder.name))
        store.meters = meters
        desktop = Desktop.__new__(Desktop)
        desktop.background = background
        desktop.app = SimpleNamespace(radio=SimpleNamespace(store=store, name='Mac'), preview_only=False)
        desktop.open_setup, desktop.poll, desktop.root = Mock(), Mock(), Mock()
        return desktop

    def test_after_an_automatic_update_without_a_secret_pairing(self):
        legacy_only = {'address:AA': {'legacy': True, 'address': 'AA'}}
        for meters in ({}, legacy_only, {'serial:x': {'paired': False, 'pending': {'AA': {'secret': '00'}}}}):
            with self.subTest(meters=meters):
                desktop = self.desktop(meters)
                desktop.run()
                desktop.open_setup.assert_called_once()

    def test_not_once_a_meter_proved_its_secret(self):
        desktop = self.desktop({'serial:x': {'paired': True, 'secret': 'ab' * 32, 'address': 'AA'}}, background=False)
        desktop.run()
        desktop.open_setup.assert_not_called()
