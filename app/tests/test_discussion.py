import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app import assistant, ui
from app.server import App, Jobs, Server
from app.store import Store


class DiscussionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = self.app = App.__new__(App)
        app.data = Path(self.tmp.name)
        app.store = Store(app.data / 'bench.sqlite3')
        app.lock = threading.RLock()
        app.models = []
        app.store.put('benchmarks', {'id': 'bench-test', 'title': 'Fixture', 'versions': [],
                                     'status': 'complete', 'models': [], 'frozen_tasks': [], 'attempts': 1})
        app.jobs = Jobs(app)

    def test_analysis_is_saved_reconciled_and_never_runs_benchmarks(self):
        with patch.object(assistant, 'discuss', return_value='## Reliability\n- Two tool errors.') as discuss, \
             patch.object(self.app, 'execute') as execute:
            first = self.app.ask('bench-test', {'message': 'Why the errors?', 'request_id': 'same'})
            self.app.jobs.queues['prepare'].join()
            second = self.app.ask('bench-test', {'message': 'Why the errors?', 'request_id': 'same'})
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(second['status'], 'complete')
            self.assertEqual(discuss.call_count, 1)
            self.assertFalse(execute.called)
            self.assertEqual(self.app.store.list('runs'), [])
            rendered = ui.discussion(self.app, 'bench-test')
            self.assertIn('Why the errors?', rendered)
            self.assertIn('<h3>Reliability</h3>', rendered)
            self.assertNotIn('data-on-interval', rendered)
            with self.assertRaises(ValueError):
                self.app.ask('bench-test', {'message': 'Different', 'request_id': 'same'})
        with patch.object(assistant, 'discuss', return_value='Follow-up') as discuss:
            self.app.ask('bench-test', {'message': 'What next?'})
            self.app.jobs.queues['prepare'].join()
            self.assertEqual(discuss.call_args.args[3][0]['question'], 'Why the errors?')

    def test_pending_failure_and_recovery_are_visible(self):
        entered, release = threading.Event(), threading.Event()
        def fail(*args):
            entered.set()
            release.wait(5)
            raise ValueError('Provider unavailable')
        with patch.object(assistant, 'discuss', side_effect=fail):
            self.app.ask('bench-test', {'message': 'Question'})
            self.assertTrue(entered.wait(2))
            try:
                self.assertIn('data-on-interval__duration.2s', ui.discussion(self.app, 'bench-test'))
                with self.assertRaises(ValueError):
                    self.app.ask('bench-test', {'message': 'Another question'})
            finally:
                release.set()
                self.app.jobs.queues['prepare'].join()
        self.assertIn('Provider unavailable', ui.discussion(self.app, 'bench-test'))
        self.app.store.put('discussions', {'id': 'chat-interrupted', 'benchmark_id': 'bench-test',
                                          'question': 'Old question', 'status': 'running'})
        self.app.store.recover()
        self.assertEqual(self.app.store.require('discussions', 'chat-interrupted')['status'], 'interrupted')

    def test_evidence_is_bounded_and_assistant_has_no_tools(self):
        root = self.app.data / 'evidence'
        (root / 'agent/threads').mkdir(parents=True)
        (root / 'agent/threads/shared.md').write_text('Shared decisions: one tester.')
        run = {'id': 'run-one', 'version': 'main', 'model': 'sol', 'attempt': 1, 'task_id': 'task',
               'status': 'complete', 'passed': True, '_dir': str(root),
               'artifacts': [{'name': 'agent/threads/shared.md'}],
               'events': [{'id': 'error-one', 'kind': 'tool', 'tool_name': 'bash', 'status': 'error',
                           'start': 10, 'end': 20, 'detail': 'Test failed'}]}
        response = {'status': 'completed', 'text': json.dumps({'reply': 'One recorded error.'})}
        with patch.object(assistant.native, 'run_agent', return_value=response) as native:
            reply = assistant.discuss(self.app.store.settings(), {'id': 'bench-test', 'versions': [{'label': 'main', 'sha': 'sha'}]},
                                     [run], [], 'Why?', self.app.data / 'chat', threading.Event(), None)
        self.assertEqual(reply, 'One recorded error.')
        self.assertTrue(native.call_args.kwargs['no_tools'])
        prompt = native.call_args.args[3]
        self.assertIn('Shared decisions: one tester.', prompt)
        self.assertIn('error-one', prompt)
        self.assertIn('Never launch runs', prompt)

    def test_new_completed_results_refresh_page_but_unchanged_state_does_not(self):
        self.app.store.update('benchmarks', 'bench-test', versions=[
            {'label': 'main', 'sha': 'main-sha'}, {'label': 'candidate', 'sha': 'candidate-sha'}])
        server = Server(('127.0.0.1', 0), self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        revision = ui.benchmark_revision(self.app, 'bench-test')
        with urllib.request.urlopen(base + '/benchmarks/bench-test?fragment=poll&revision=' + revision) as response:
            self.assertEqual(response.status, 204)
        for version in ('main', 'candidate'):
            self.app.store.put('runs', {'id': 'run-' + version, 'benchmark_id': 'bench-test',
                'version': version, 'task_id': 'task', 'model': 'sol', 'attempt': 1,
                'status': 'complete', 'passed': True})
        with urllib.request.urlopen(base + '/benchmarks/bench-test?fragment=poll&revision=' + revision) as response:
            self.assertEqual(response.headers.get_content_type(), 'text/event-stream')
            html = response.read().decode()
            self.assertIn('What differed', html)
            self.assertIn('Both versions passed every matched attempt.', html)
            self.assertIn('Follow-up discussion', html)
            self.assertIn('data-revision=', html)
            self.assertIn('data-on-interval__duration.4s', html)


if __name__ == '__main__':
    unittest.main()
