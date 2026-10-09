import unittest
from app import results


class FindingsTests(unittest.TestCase):
    def run_record(self, version, **extra):
        return {'id': version, 'version': version, 'task_id': 'task', 'model': 'sol', 'attempt': 1,
                'status': 'complete', 'passed': True, 'wall_seconds': 360,
                'actors': [{'id': 'parent', 'llm_calls': 31}], 'llm_calls': 51,
                'subagents': 2, 'tool_errors': 0, **extra}

    def test_top_reports_tradeoffs_threads_and_matched_sample(self):
        baseline = self.run_record('main')
        candidate = self.run_record('threads', wall_seconds=344, subagents=1, tool_errors=2,
            actors=[{'id': 'parent', 'llm_calls': 27}],
            artifacts=[{'name': 'agent/threads/shared.md'}, {'name': 'agent/threads/.conv/id'}])
        unmatched = self.run_record('main', attempt=2, wall_seconds=9999)
        top, _ = results.comparison([baseline, candidate, unmatched], [{'label': 'main'}, {'label': 'threads'}])
        self.assertIn('Both versions passed every matched attempt.', top)
        self.assertIn('used a shared thread with 1 subagent', top)
        self.assertIn('16s lower actor time', top)
        self.assertIn('4 fewer parent calls', top)
        self.assertIn('2 more tool errors', top)
        self.assertIn('One matched attempt per version', top)
        self.assertIn('/artifacts/threads/agent/threads/shared.md', top)
        self.assertNotIn('.conv', top)
        self.assertNotIn('9999', top)

    def test_missing_measurements_are_not_reported_as_zero(self):
        baseline = self.run_record('main', wall_seconds=None, actors=[], tool_errors=None)
        candidate = self.run_record('candidate', passed=False)
        top, _ = results.comparison([baseline, candidate], [{'label': 'main'}, {'label': 'candidate'}])
        self.assertIn('main passed 1/1; candidate passed 0/1', top)
        self.assertNotIn('lower actor time', top)
        self.assertNotIn('parent calls', top)
        self.assertNotIn('more tool errors', top)

    def test_pending_runs_have_no_success_or_efficiency_claims(self):
        top, _ = results.comparison([self.run_record('main')], [{'label': 'main'}, {'label': 'candidate'}])
        self.assertIn('No matched completed results yet.', top)
        self.assertNotIn('Both versions passed', top)


if __name__ == '__main__':
    unittest.main()
