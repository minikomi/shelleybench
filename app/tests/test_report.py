import unittest

from app import report


class ReportTests(unittest.TestCase):
    def run_record(self, version, task="one", title="One", model="sol", attempt=1, **extra):
        return {"id": f"{task}-{model}-{attempt}-{version}", "version": version, "task_id": task,
                "task_title": title, "model": model, "attempt": attempt, "status": "complete", **extra}

    def bench(self):
        return {"versions": [{"label": "first"}, {"label": "second"}],
                "frozen_tasks": [{"id": "one", "title": "One"}], "models": ["sol"], "attempts": 1}

    def test_groups_do_not_pool_tasks_models_or_attempts_and_use_configuration_order(self):
        runs = [self.run_record("second", task="two", title="Two", attempt=2), self.run_record("first"),
                self.run_record("second", model="other"), self.run_record("first", task="two", title="Two", attempt=2),
                self.run_record("unknown")]
        found = report.groups(self.bench(), runs)
        self.assertEqual([(g["task_id"], g["model"], g["attempt"]) for g in found],
                         [("one", "sol", 1), ("two", "sol", 2), ("one", "other", 1)])
        self.assertEqual([r["version"] for r in found[1]["runs"]], ["first", "second"])
        self.assertNotIn("unknown", [r["version"] for g in found for r in g["runs"]])

    def test_configured_missing_group_and_unknown_versions_are_honest(self):
        bench = {"versions": [{"label": "first"}, {"label": "second"}],
                 "frozen_tasks": [{"id": "one", "title": "One"}], "models": ["sol"], "attempts": 2}
        found = report.groups(bench, [self.run_record("unknown")])
        self.assertEqual(len(found), 2)
        self.assertEqual(found[1]["runs"], [])
        html = report.charts(found[1], bench["versions"])
        self.assertEqual(html.count("Pending"), 8)  # visible and accessible label, two metrics × two versions

    def test_charts_preserve_zero_missing_pending_and_token_categories(self):
        group = {"task_title": "One", "model": "sol", "attempt": 1, "runs": [
            self.run_record("first", wall_seconds=0, input_tokens=1, output_tokens=2, cache_read_tokens=3, cache_write_tokens=4),
            self.run_record("second", wall_seconds=None, input_tokens=1, output_tokens=None, cache_read_tokens=3, cache_write_tokens=4),
        ]}
        html = report.charts(group, self.bench()["versions"])
        self.assertIn("0s", html)
        self.assertIn("10 total", html)
        self.assertIn("input 1 · output 2 · cache read 3 · cache write 4", html)
        self.assertIn("Not recorded", html)
        self.assertNotIn(">0 total", html)
        self.assertIn('aria-valuenow="0"', html)

    def test_labels_are_escaped(self):
        group = {"task_title": '<task>', "model": 'm&"', "attempt": 1,
                 "runs": [self.run_record('x<script>', wall_seconds=1, input_tokens=1, output_tokens=1,
                                    cache_read_tokens=1, cache_write_tokens=1)]}
        html = report.charts(group, [{"label": 'x<script>'}])
        self.assertIn("&lt;task&gt;", html)
        self.assertIn("m&amp;&quot;", html)
        self.assertIn("x&lt;script&gt;", html)
        self.assertNotIn("<script>", html)

    def test_completed_retry_replaces_infrastructure_error_in_comparison(self):
        before = self.run_record("first", passed=True)
        failed = self.run_record("second", status="infrastructure_failure")
        retry = self.run_record("second", attempt=2, passed=True)
        originals = [before, failed, retry]
        visible, archived = report.current_runs(originals)
        self.assertEqual(archived, [failed])
        self.assertEqual([r["id"] for r in visible], [before["id"], retry["id"]])
        self.assertEqual(retry["attempt"], 2)
        self.assertNotIn("comparison_attempt", retry)
        bench = {**self.bench(), "attempts": 2}
        groups = report.groups(bench, visible)
        self.assertEqual(len(groups), 1)
        self.assertEqual([r["id"] for r in groups[0]["runs"]], [before["id"], retry["id"]])
        self.assertEqual(groups[0]["runs"][1]["attempt"], 2)
        from app import results
        self.assertEqual(results.pair_key(visible[0]), results.pair_key(visible[1]))

    def test_completed_repeats_and_unreplaced_failures_remain_visible(self):
        first = self.run_record("first")
        repeated = self.run_record("first", attempt=2)
        failed = self.run_record("second", status="infrastructure_failure")
        self.assertEqual(report.current_runs([first, repeated, failed]), ([first, repeated, failed], []))

    def test_summary_counts_completed_replacement_not_archived_error(self):
        from app.server import App
        runs = [self.run_record("second", status="infrastructure_failure"),
                self.run_record("second", attempt=2, passed=True, wall_seconds=42)]
        summary = App.__new__(App).summary(self.bench(), runs)["second"]
        self.assertEqual((summary["runs"], summary["passed"], summary["errors"]), (1, 1, 0))
        self.assertEqual(summary["wall_seconds"], 42)

    def test_normal_patch_baseline_precedes_experiment_and_counts_only_patch_errors(self):
        versions = [{"label": "Simple patch"}, {"label": "Normal nested"}]
        ordered = report.display_versions(versions)
        self.assertEqual([(v["label"], v["role"]) for v in ordered],
                         [("Normal nested", "Baseline"), ("Simple patch", "Experiment")])
        self.assertNotIn("role", versions[0])
        run = self.run_record("Normal nested", events=[
            {"kind": "tool", "tool_name": "patch", "status": "error"},
            {"kind": "tool", "tool_name": "bash", "status": "error"},
            {"kind": "tool", "tool_name": "patch", "status": "complete"},
        ])
        self.assertEqual(report.patch_counts(run), {"calls": 2, "rejected": 1})
        self.assertIsNone(report.patch_counts(self.run_record("Simple patch")))
        html = report.charts({"runs": [run]}, ordered, patch_focus=True)
        self.assertIn("1 / 2 calls", html)
        self.assertLess(html.index("Rejected patch calls"), html.index("Actor wall time"))
        self.assertLess(html.index("Normal nested"), html.index("Simple patch"))


if __name__ == "__main__":
    unittest.main()
