import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('controller', Path(__file__).with_name('controller.py'))
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class QueueTests(unittest.TestCase):
    def test_in_progress_workflow_still_has_queued_jobs(self):
        broker = controller.Broker({'golden': 'sweetmeter-ci-golden', 'work': 'sweetmeter-ci-work',
                                    'repository': 'example/meter', 'actor': 'example'})
        requests = []
        def api(path):
            requests.append(path)
            if '/jobs?' in path:
                return {'jobs': [{'status': 'queued', 'labels': controller.LABELS}]}
            if 'status=queued' in path:
                return {'workflow_runs': []}
            return {'workflow_runs': [{'id': 123, 'event': 'push', 'actor': {'login': 'example'},
                     'triggering_actor': {'login': 'example'}, 'head_branch': 'main',
                     'head_repository': {'full_name': 'example/meter'}}]}
        broker.api = api
        self.assertTrue(broker.queued())
        self.assertTrue(any('status=in_progress' in p for p in requests))

    def test_controller_never_routes_external_pull_requests(self):
        broker = controller.Broker({'golden': 'sweetmeter-ci-golden', 'work': 'sweetmeter-ci-work',
                                    'repository': 'example/meter', 'actor': 'example'})
        broker.api = lambda path: {'workflow_runs': [{'id': 123, 'event': 'pull_request'}]}
        self.assertFalse(broker.queued())

    def test_distinct_owned_vm_names_required(self):
        for work in ('unrelated-project', 'sweetmeter-ci-golden'):
            with self.assertRaises(ValueError):
                controller.Broker({'golden': 'sweetmeter-ci-golden', 'work': work,
                                   'repository': 'example/meter', 'actor': 'example'})


if __name__ == '__main__':
    unittest.main()
