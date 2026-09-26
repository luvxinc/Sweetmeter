"""Offline recovery criterion of scripts/verify_device.py; no device access."""
import isolation  # noqa: F401  (test sandbox; must be the first import)
import unittest

from scripts.verify_device import recovery_result

HOST = '7a1e1000-ff1b-4d9f-a023-0123456789ab'


def serial(at, text):
    return {'at': at, 'kind': 'serial', 'value': text}


def saved(seen_at, **status):
    return {'seen_at': seen_at, 'status': {'auth': 1, 'selected': True, 'clock_synced': True, **status}}


class RecoveryCriterionTests(unittest.TestCase):
    READY = 'READY SWEETMETER 2026.9.15 protocol=4 paired=1 wake=0 reset=1'

    def test_requires_ready_after_reset_then_fresh_trusted_status_and_ack(self):
        lines = [serial(101, 'ESP-ROM:esp32s3'), serial(103, self.READY)]
        result = recovery_result(1, 100, lines, saved(140), {'received_at': 141}, HOST)
        self.assertTrue(result['pass'])
        self.assertEqual(result['ready_after_seconds'], 3)
        # Still waiting: no READY yet, or status/ACK saved before this boot.
        self.assertIsNone(recovery_result(1, 100, lines[:1], saved(140), {'received_at': 141}, HOST))
        self.assertIsNone(recovery_result(1, 100, lines, saved(102), {'received_at': 141}, HOST))
        self.assertIsNone(recovery_result(1, 100, lines, saved(140), {'received_at': 102}, HOST))
        # A status without a synchronized clock or selection is not recovery.
        self.assertIsNone(recovery_result(1, 100, lines, saved(140, clock_synced=False), {'received_at': 141}, HOST))
        self.assertIsNone(recovery_result(1, 100, lines, saved(140, selected=False), {'received_at': 141}, HOST))

    def test_no_uptime_field_is_needed_and_legacy_status_names_the_host(self):
        lines = [serial(103, self.READY)]
        legacy = {'seen_at': 140, 'status': {'selected_host': HOST, 'clock_synced': True}}
        self.assertTrue(recovery_result(1, 100, lines, legacy, {'received_at': 141}, HOST)['pass'])
        other = {'seen_at': 140, 'status': {'selected_host': HOST[:-1] + 'f', 'clock_synced': True}}
        self.assertIsNone(recovery_result(1, 100, lines, other, {'received_at': 141}, HOST))

    def test_panic_or_second_boot_fails(self):
        panic = [serial(103, self.READY), serial(110, "Guru Meditation Error: Core 0 panic'ed")]
        self.assertFalse(recovery_result(1, 100, panic, saved(140), {'received_at': 141}, HOST)['pass'])
        twice = [serial(103, self.READY), serial(120, self.READY)]
        self.assertFalse(recovery_result(1, 100, twice, saved(140), {'received_at': 141}, HOST)['pass'])


if __name__ == '__main__':
    unittest.main()
