import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import ui
from app.server import App
from app.store import Store


class ReportPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = App.__new__(App)
        self.app.store = Store(Path(self.temp.name) / 'bench.sqlite3')
        self.bench = {'id': 'bench', 'title': 'Does guidance help?', 'group': 'example',
                      'status': 'complete', 'reasoning': 'medium', 'attempts': 1,
                      'versions': [{'label': 'before', 'sha': 'abc'}, {'label': 'after', 'sha': 'abc'}],
                      'models': ['sol'], 'frozen_tasks': [{'id': 'task', 'title': 'Citation history'}],
                      'report': {'hypothesis': 'Better handoffs reduce waste.',
                                 'abstract': '**Observed:** both passed.',
                                 'conclusions': 'No general winner.', 'followups': '- Repeat matched attempts.'}}
        self.app.store.put('benchmarks', self.bench)
        for version, seconds in [('before', 120), ('after', 180)]:
            self.app.store.put('runs', {'id': version, 'benchmark_id': 'bench', 'version': version,
                'task_id': 'task', 'task_title': 'Citation history', 'model': 'sol', 'attempt': 1,
                'status': 'complete', 'passed': True, 'wall_seconds': seconds,
                'input_tokens': 10, 'output_tokens': 20, 'cache_read_tokens': 30,
                'cache_write_tokens': 40, 'llm_calls': 2, 'subagents': 0,
                'actors': [{'id': 'parent', 'llm_calls': 2}],
                'events': [{'id': 'read', 'actor_id': 'parent', 'kind': 'tool', 'tool_name': 'bash',
                            'start': 2, 'end': 3, 'detail': 'input: {"command":"sed -n 1,5p file"}'}]})

    def test_paper_sections_charts_and_raw_drilldowns(self):
        title, body, live = ui.benchmark_page(self.app, 'bench', {})
        self.assertEqual(title, self.bench['title'])
        self.assertFalse(live)
        self.assertLess(body.index('Hypothesis being tested'), body.index('Abstract'))
        positions = [body.index('id="' + x + '"') for x in ['setup', 'runs', 'results', 'details', 'conclusions']]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('Better handoffs reduce waste.', body)
        self.assertIn('<strong>Observed:</strong>', body)
        self.assertIn('report-bar-fill', body)
        self.assertIn('/runs/before', body)
        self.assertIn('data-row=', body)
        self.assertIn('class="tl-pair"', body)
        self.assertIn('task × parent model × attempt', body)
        self.assertIn('<strong>History:</strong>', body)
        self.assertIn('each tool call has its own block', body)
        self.assertIn('No general winner.', body)
        self.assertIn('Follow-up discussion', body)
        self.assertIn('report_hypothesis', body)

    def test_legacy_findings_are_visible_without_fabricating_a_hypothesis(self):
        self.app.store.update('benchmarks', 'bench', report={}, findings='## Verdict\n**Both passed.**\n## Caveats\n- One attempt.\n## Next\n- Test another task.')
        _, body, _ = ui.benchmark_page(self.app, 'bench', {})
        self.assertIn('no explicit hypothesis was recorded', body)
        self.assertIn('<strong>Both passed.</strong>', body)
        self.assertIn('Test another task.', body)
        self.assertIn('Original generated findings', body)

    def test_pending_comparison_is_not_presented_as_success(self):
        self.app.store.update('benchmarks', 'bench', report={}, status='running')
        self.app.store.update('runs', 'after', status='running', passed=None, wall_seconds=None)
        _, body, live = ui.benchmark_page(self.app, 'bench', {})
        self.assertTrue(live)
        self.assertIn('1/2 recorded runs complete', body)
        self.assertNotIn('Both versions passed every matched attempt.', body)
        self.assertIn('No matched completed results yet.', body)

    def test_report_updates_merge_validate_and_do_not_change_runs(self):
        before = self.app.store.list('runs')
        with patch.object(self.app, 'queue_runs') as queue:
            updated = self.app.update_benchmark('bench', {'report': {'abstract': 'New readout.'}})
        self.assertFalse(queue.called)
        self.assertEqual(updated['report']['hypothesis'], 'Better handoffs reduce waste.')
        self.assertEqual(updated['report']['abstract'], 'New readout.')
        self.assertEqual(self.app.store.list('runs'), before)
        for invalid in [[], {'unknown': 'x'}, {'abstract': 4}, {'hypothesis': 'x' * 12001}]:
            with self.subTest(invalid=type(invalid)):
                with self.assertRaises(ValueError):
                    self.app.update_benchmark('bench', {'report': invalid})

    def test_report_prose_and_labels_are_escaped(self):
        self.app.store.update('benchmarks', 'bench', title='<script>alert(1)</script>',
                              report={'hypothesis': '<script>bad</script>', 'abstract': '<img src=x onerror=bad>'})
        _, body, _ = ui.benchmark_page(self.app, 'bench', {})
        self.assertNotIn('<script>', body)
        self.assertNotIn('<img ', body)
        self.assertIn('&lt;script&gt;', body)


if __name__ == '__main__':
    unittest.main()
