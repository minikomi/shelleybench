import copy
import json
import tempfile
import unittest
from pathlib import Path

from app import import_report, import_results, ui
from app.server import App
from app.store import Store


class ImportResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "archive"
        self.source = self.archive / "runs/micro/comparison.json"
        self.source.parent.mkdir(parents=True)
        self.rows = [
            {"id": "normal", "task": "single", "model": "glm", "repeat": 1, "variant": "nested",
             "passed": False, "content_correct": False, "patch_only_protocol": True,
             "one_shot_success": False, "patch_calls": 3, "rejected_patch_calls": 3,
             "input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30,
             "llm_calls": 5, "wall_seconds": 12.5,
             "model_usage_breakdown": {"editing_agent": {"input_tokens": 90, "cached_input_tokens": 20,
                                                        "output_tokens": 25, "model_calls": 4}}},
            {"id": "simple", "task": "single", "model": "glm", "repeat": 1, "variant": "simple",
             "passed": True, "patch_only_protocol": True, "one_shot_success": True,
             "patch_calls": 1, "rejected_patch_calls": 0, "wall_seconds": 1.25,
             "model_usage_breakdown": {"editing_agent": {"input_tokens": 10, "cached_input_tokens": 20,
                                                        "output_tokens": 5, "model_calls": 2}}},
        ]
        self.source.write_text(json.dumps({"rows": self.rows, "definitions": {"passed": "Exact bytes"}}))
        self.spec = {"id": "bench-import-test", "title": "Historical mechanics", "source": "runs/micro/comparison.json",
                     "sha256": import_results.digest(self.source.read_bytes()), "expected_rows": 2,
                     "usage_scope": "editing_agent", "study_type": "editing_microbenchmark",
                     "variant_labels": {"nested": "Normal nested", "simple": "Simple · no append"},
                     "measurement_note": "Editing only; timestamps not imported.",
                     "report": dict(hypothesis="Fewer errors?", abstract="One loss retained.", setup="Fresh trials.",
                                    conclusions="Narrow evidence.", followups="Repeat current schemas.")}
        self.manifest = self.root / "manifest.json"
        self.write_manifest()
        self.data = self.root / "data"
        self.store = Store(self.data / "bench.sqlite3")
        self.app = App.__new__(App)
        self.app.store = self.store

    def write_manifest(self):
        self.manifest.write_text(json.dumps({"studies": [self.spec]}))

    def prepare(self):
        return import_results.prepare(self.archive, self.root / "unused", self.manifest)

    def test_import_preserves_raw_metrics_and_excludes_auxiliary_usage(self):
        studies = self.prepare()
        import_results.persist(self.store, self.data, studies)
        runs = self.store.list("runs")
        run = runs[0]
        self.assertFalse(run["passed"])
        self.assertEqual(run["input_tokens"], 90)
        self.assertEqual(run["llm_calls"], 4)
        self.assertEqual(run["source_record"]["llm_calls"], 5)
        self.assertEqual(run["editing_metrics"]["rejected_patch_calls"], 3)
        self.assertIsNone(run["cache_write_tokens"])
        self.assertIsNone(run["cost_usd"])
        self.assertIsNone(run["subagents"])
        self.assertEqual(run["events"], [])
        self.assertEqual(run["actors"], [])
        self.assertEqual(import_report.summary(studies[0][0], runs)[0]["one_shot"], "0/1")
        self.assertEqual(Path(run["_dir"], "comparison.json").read_bytes(), self.source.read_bytes())

    def test_repeated_import_does_not_modify_existing_records(self):
        self.store.put("runs", {"id": "old", "passed": True})
        import_results.persist(self.store, self.data, self.prepare())
        bench = self.store.list("benchmarks")[0]
        self.store.update("benchmarks", bench["id"], report={**bench["report"], "abstract": "User edit"})
        before = self.store.list("runs"), self.store.list("benchmarks")
        import_results.persist(self.store, self.data, self.prepare())
        self.assertEqual(before, (self.store.list("runs"), self.store.list("benchmarks")))

    def test_source_hash_and_duplicate_rows_are_checked_before_writing(self):
        self.source.write_text("{}")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.prepare()
        self.source.write_text(json.dumps({"rows": [self.rows[0], self.rows[0]]}))
        self.spec["sha256"] = import_results.digest(self.source.read_bytes())
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.prepare()
        self.assertEqual(self.store.list("runs"), [])
        self.assertEqual(self.store.list("benchmarks"), [])

    def test_conflicting_import_does_not_overwrite_source_records(self):
        studies = self.prepare()
        import_results.persist(self.store, self.data, studies)
        before = self.store.list("runs")
        conflict = copy.deepcopy(studies)
        conflict[0][1][0]["source_record"]["passed"] = True
        with self.assertRaisesRegex(ValueError, "Conflicting source row"):
            import_results.persist(self.store, self.data, conflict)
        self.assertEqual(before, self.store.list("runs"))

    def test_imported_pages_show_editing_evidence_not_fake_timelines(self):
        import_results.persist(self.store, self.data, self.prepare())
        _, body, live = ui.benchmark_page(self.app, "bench-import-test", {})
        self.assertFalse(live)
        self.assertIn("Editing microbenchmark", body)
        self.assertIn("3/3", body)
        self.assertIn("12.500s", body)
        self.assertIn("SHA-256", body)
        self.assertIn("historical", body)
        self.assertIn("report_hypothesis", body)
        self.assertNotIn("parent + helpers", body)
        self.assertNotIn("Resume unfinished", body)
        run = self.store.list("runs")[0]
        _, body, live = ui.run_page(self.app, run["id"], {})
        self.assertFalse(live)
        self.assertIn("Original source record", body)
        self.assertIn("cannot be reconstructed", body)
        self.assertNotIn("<h2>Timeline</h2>", body)

    def test_unrecorded_one_shot_is_not_derived_from_zero_rejections(self):
        studies = self.prepare()
        runs = studies[0][1]
        runs[1]["editing_metrics"].pop("one_shot_success")
        self.assertEqual(import_report.summary(studies[0][0], runs)[1]["one_shot"], "Not recorded")

    def test_imports_cannot_accidentally_queue_inference(self):
        import_results.persist(self.store, self.data, self.prepare())
        with self.assertRaisesRegex(ValueError, "cannot be rerun"):
            self.app.resume_benchmark("bench-import-test")
        with self.assertRaisesRegex(ValueError, "cannot be extended"):
            self.app.extend_benchmark("bench-import-test", {"attempts": 2})

    def test_all_recorded_repetitions_are_inline_in_matched_cards(self):
        import_results.persist(self.store, self.data, self.prepare())
        runs = self.store.list("runs")
        for run in runs:
            event = {"id": run["id"] + "-patch", "actor_id": "parent", "kind": "tool",
                     "tool_name": "patch", "start": 1, "end": 1.2, "status": "complete",
                     "detail": 'input: {"path":"target.py"}'}
            self.store.update("runs", run["id"], events=[event], actors=[{"id": "parent"}])
            self.store.put("runs", {**run, "id": run["id"] + "-repeat", "attempt": 2,
                                   "events": [{**event, "id": event["id"] + "-repeat"}],
                                   "actors": [{"id": "parent"}]})
        _, body, _ = ui.benchmark_page(self.app, "bench-import-test", {})
        self.assertEqual(body.count('class="tl-pair"'), 2)
        self.assertIn("Matched repetition 1", body)
        self.assertIn("Matched repetition 2", body)
        self.assertIn('data-version="Normal nested"', body)
        self.assertIn('data-version="Simple · no append"', body)
        self.assertNotIn('name="trial"', body)
        self.assertNotIn("Show timeline", body)
        self.assertIn('<details class="report-source" id="provenance">', body)

if __name__ == "__main__":
    unittest.main()
