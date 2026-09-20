import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('guard', Path(__file__).with_name('job_guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'repository': 'example/meter', 'actor': 'example'}
        self.env = {'GITHUB_REPOSITORY': 'example/meter', 'GITHUB_ACTOR': 'example',
                    'GITHUB_TRIGGERING_ACTOR': 'example', 'GITHUB_EVENT_NAME': 'push',
                    'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': 'a' * 40}
        self.event = {'repository': {'full_name': 'example/meter', 'fork': False},
                      'sender': {'login': 'example'}, 'ref': 'refs/heads/main',
                      'after': 'a' * 40, 'deleted': False}

    def test_owner_push_and_manual(self):
        self.assertTrue(guard.authorize(self.env, self.event, self.policy))
        self.env.update(GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/codex/test')
        self.assertTrue(guard.authorize(self.env, self.event, self.policy))

    def test_untrusted_context(self):
        for field, value in [('GITHUB_REPOSITORY', 'evil/meter'), ('GITHUB_ACTOR', 'evil'),
                             ('GITHUB_TRIGGERING_ACTOR', 'evil'),
                             ('GITHUB_EVENT_NAME', 'pull_request'),
                             ('GITHUB_EVENT_NAME', 'pull_request_target'),
                             ('GITHUB_REF', 'refs/tags/v1'), ('GITHUB_REF', 'refs/heads/main-evil'),
                             ('GITHUB_REF', 'refs/heads/codex/'), ('GITHUB_SHA', '')]:
            with self.subTest(field=field, value=value):
                env = dict(self.env, **{field: value})
                self.assertFalse(guard.authorize(env, self.event, self.policy))

    def test_payload_agrees_with_context(self):
        for field, value in [('repository', {'full_name': 'evil/meter', 'fork': False}),
                             ('repository', {'full_name': 'example/meter', 'fork': True}),
                             ('sender', {'login': 'evil'}), ('ref', 'refs/heads/evil'),
                             ('after', 'b' * 40), ('deleted', True)]:
            with self.subTest(field=field):
                event = dict(self.event, **{field: value})
                self.assertFalse(guard.authorize(self.env, event, self.policy))


if __name__ == '__main__':
    unittest.main()
