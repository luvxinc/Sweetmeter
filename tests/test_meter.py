import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from meter.__main__ import display_snapshot
from meter.providers import claude_subscription, parse_claude, parse_codex, refresh
from meter.render import pack_frame, render, reset_countdown
from meter.tokens import TokenIndex, parse_record


def cx_record(timestamp, total=110, last=110):
    info = {'total_token_usage': {'input_tokens': total - 10, 'output_tokens': 10,
                                 'cached_input_tokens': 80, 'total_tokens': total}}
    if last is not None:
        info['last_token_usage'] = {'input_tokens': last - 10, 'output_tokens': 10,
                                   'cached_input_tokens': 80, 'reasoning_output_tokens': 5,
                                   'total_tokens': last}
    return {'type': 'event_msg', 'timestamp': timestamp,
            'payload': {'type': 'token_count', 'info': info}}


class ProviderTests(unittest.TestCase):
    def test_claude_plan_uses_matching_login_metadata(self):
        credentials = {'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_20x',
                       'accessToken': 'test-secret'}
        rows = parse_claude({}, claude_subscription(credentials))
        self.assertEqual([r['subscription_label'] for r in rows], ['MAX 20×'] * 3)
        self.assertNotIn('test-secret', json.dumps(rows))
        self.assertEqual(claude_subscription({'subscriptionType': 'max'}), 'MAX')
        self.assertEqual(claude_subscription({'subscriptionType': 'pro',
                                             'rateLimitTier': 'default_claude_max_20x'}), 'PRO')
        self.assertEqual(claude_subscription({}), '--')

    def test_codex_plan_is_from_selected_quota_bucket(self):
        data = {'rateLimitsByLimitId': {'codex': {'planType': 'plus'}},
                'rateLimits': {'planType': 'pro'}}
        self.assertEqual(parse_codex(data)[0]['subscription_label'], 'PLUS')
        del data['rateLimitsByLimitId']['codex']['planType']
        data['rateLimitsByLimitId']['codex']['primary'] = {'windowDurationMins': 10080}
        self.assertEqual(parse_codex(data)[0]['subscription_label'], '--')
        self.assertEqual(parse_codex({'rateLimits': {'planType': 'pro'}})[0]['subscription_label'], 'PRO')

    def test_fable_is_independent_with_null_model_id(self):
        rows = parse_claude({'five_hour': {'utilization': .5},
                            'seven_day': {'utilization': 42},
                            'seven_day_sonnet': {'utilization': 99},
                            'limits': [dict(kind='weekly_scoped', percent=76,
                                            scope={'model': {'id': None, 'display_name': 'Fable'}})]})
        self.assertEqual([r['used'] for r in rows], [.5, 42, 76])

    def test_absent_fable_never_becomes_zero_or_sonnet(self):
        data = {'seven_day_sonnet': {'utilization': 80},
                'limits': [{'kind': 'weekly_scoped', 'scope': {'model': None}}]}
        self.assertIsNone(parse_claude(data)[2]['used'])

    def test_weekly_can_be_primary_and_other_buckets_are_ignored(self):
        data = {'rateLimitsByLimitId': {'codex': {
            'primary': {'usedPercent': 9, 'windowDurationMins': 10080}, 'secondary': None}},
            'rateLimits': {'secondary': {'usedPercent': 88, 'windowDurationMins': 10080}}}
        self.assertEqual(parse_codex(data)[0]['used'], 9)
        data['rateLimitsByLimitId']['codex']['primary']['windowDurationMins'] = 300
        self.assertIsNone(parse_codex(data)[0]['used'])

    def test_failure_retains_last_good_value_and_backs_off(self):
        cache = refresh({}, 1000, {'claude': lambda: parse_claude({'five_hour': {'utilization': 12}})})
        calls = []

        def fail():
            calls.append(1)
            raise RuntimeError('Claude HTTP 429')

        self.assertEqual(refresh(cache, 1030, {'claude': fail})['providers'], cache['providers'])
        self.assertEqual(calls, [])
        failed = refresh(cache, 1301, {'claude': fail})['providers']['claude']
        self.assertEqual(failed['rows'][0]['used'], 12)
        self.assertEqual(failed['next_poll'], 2201)

    def test_minute_polling_manual_refresh_and_backoff(self):
        calls = []
        def good():
            calls.append(1)
            return parse_claude({})
        cache = refresh({}, 1000, {'claude': good})
        self.assertEqual(cache['providers']['claude']['next_poll'], 1060)
        refresh(cache, 1030, {'claude': good})
        self.assertEqual(len(calls), 1)
        refresh(cache, 1030, {'claude': good}, force=True)
        self.assertEqual(len(calls), 2)
        cache['providers']['claude']['error'] = 'Claude HTTP 429'
        refresh(cache, 1030, {'claude': good}, force=True)
        self.assertEqual(len(calls), 2)


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.index = TokenIndex(self.root / 'index.db')

    def tearDown(self):
        self.index.db.close()
        self.tmp.cleanup()

    def test_codex_cache_and_reasoning_are_not_added_twice(self):
        state = {}
        event = parse_record(cx_record(1000), 'codex', state)
        self.assertEqual(sum(event[4:]), 110)
        self.assertIsNone(parse_record(cx_record(1001), 'codex', state))

    def test_codex_cumulative_only_uses_delta(self):
        state = {}
        first = parse_record(cx_record(1000, last=None), 'codex', state)
        second = parse_record(cx_record(1001, total=150, last=None), 'codex', state)
        self.assertEqual(sum(first[4:]), 110)
        self.assertEqual(sum(second[4:]), 40)

    def test_claude_streaming_duplicate_and_incomplete_record(self):
        path = self.root / 'claude.jsonl'
        row = {'type': 'assistant', 'timestamp': 1000, 'requestId': 'request',
               'message': {'id': 'message', 'model': 'claude-fable-5-1',
                           'usage': {'input_tokens': 10, 'output_tokens': 5,
                                     'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 100}}}
        path.write_text(json.dumps(row) + '\n')
        self.index.scan_file(path, 'claude')
        row['message']['usage']['output_tokens'] = 15
        with path.open('a') as file:
            file.write(json.dumps(row))
        self.index.scan_file(path, 'claude')
        self.assertEqual(self.index.total('claude', 999, 1001), 135)
        with path.open('a') as file:
            file.write('\n')
        self.index.scan_file(path, 'claude')
        self.index.scan_file(path, 'claude')
        self.assertEqual(self.index.total('claude', 999, 1001, fable=True), 145)
        self.assertEqual(self.index.total('claude', 1001, 2000), 0)

    def test_fork_replayed_usage_keeps_ancestor_time(self):
        parent = self.root / 'parent.jsonl'
        child = self.root / 'child.jsonl'
        parent.write_text('\n'.join(map(json.dumps, [
            {'type': 'session_meta', 'payload': {'id': 'parent'}}, cx_record(1000)])) + '\n')
        child.write_text('\n'.join(map(json.dumps, [
            {'type': 'session_meta', 'payload': {'id': 'child', 'forked_from_id': 'parent'}},
            cx_record(2000), cx_record(2010, total=230, last=120)])) + '\n')
        self.index.scan_file(parent, 'codex')
        self.index.scan_file(child, 'codex')
        self.assertEqual(self.index.total('codex', 0, 3000), 230)
        self.assertEqual(self.index.total('codex', 1500, 3000), 120)

    def test_expired_quota_is_unknown(self):
        rows = parse_claude({'five_hour': {'utilization': 50, 'resets_at': 900}})
        cache = {'providers': {'claude': {'rows': rows, 'fetched_at': 800}}}
        snapshot = display_snapshot(cache, self.index, {'claude'}, 1000)
        self.assertIsNone(snapshot['rows'][0]['used'])
        self.assertIsNone(snapshot['rows'][0]['tokens'])
        self.assertTrue(snapshot['stale'])


class FrameTests(unittest.TestCase):
    def test_reset_countdown_boundaries(self):
        now = 1789900000
        self.assertEqual(reset_countdown(now + 2 * 86400 + 5 * 3600 + 32 * 60 + 59, now), '02d 05h 32m')
        self.assertEqual(reset_countdown(now + 86400, now), '01d 00h 00m')
        self.assertEqual(reset_countdown(now + 86399, now), '00d 23h 59m')
        self.assertEqual(reset_countdown(now + 3600, now), '00d 01h 00m')
        self.assertEqual(reset_countdown(now + 3599, now), '00d 00h 59m')
        self.assertEqual(reset_countdown(now + 60, now), '00d 00h 01m')
        self.assertEqual(reset_countdown(now + 59, now), '00d 00h <1m')

    def test_missing_or_expired_reset_is_not_a_future_countdown(self):
        self.assertEqual(reset_countdown(None, 1000), '--d --h --m')
        self.assertEqual(reset_countdown(1000, 1000), 'reset pending')
        self.assertEqual(reset_countdown(999, 1000), 'reset pending')

    def test_panel_addressing_and_padding(self):
        screen = Image.new('1', (250, 122), 1)
        self.assertEqual(pack_frame(screen), b'\xff' * 4000)
        screen.putpixel((0, 0), 0)
        screen.putpixel((249, 121), 0)
        frame = pack_frame(screen)
        self.assertEqual(frame[0], 0x7f)
        self.assertEqual(frame[249 * 16 + 15], 0xbf)
        self.assertEqual(sum(8 - n.bit_count() for n in frame), 2)

    def test_four_quotas_render_on_one_page(self):
        rows = parse_claude({}) + parse_codex({})
        screen = render(dict(rows=rows, as_of=1789870000, stale=True))
        self.assertEqual(screen.size, (250, 122))
        self.assertEqual(len(pack_frame(screen)), 4000)


if __name__ == '__main__':
    unittest.main()
