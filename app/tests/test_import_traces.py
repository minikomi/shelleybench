import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import import_traces
from app.store import Store


class TraceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.store = Store(self.data / "bench.sqlite3")
        self.micro = self.root / "micro"
        self.source = self.micro / "trial/runtime/agent.db"
        self.source.parent.mkdir(parents=True)
        with sqlite3.connect(self.source) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE conversations (conversation_id TEXT)")
            db.execute("INSERT INTO conversations VALUES ('actor')")
        artifact_root = self.data / "imports/study"
        artifact_root.mkdir(parents=True)
        self.run = self.store.put("runs", {
            "id": "run", "benchmark_id": "bench-import-example",
            "source_record": {"id": "trial", "conversation_id": "actor",
                              "started_at": "2024-01-01T00:00:00Z"},
            "_dir": str(artifact_root), "passed": False, "llm_calls": 2, "wall_seconds": 3,
            "input_tokens": 7, "output_tokens": 2, "cache_read_tokens": 5,
            "tool_calls": 1, "tool_errors": 1, "events": [], "actors": [], "artifacts": [],
        })
        self.metrics = {k: self.run[k] for k in ("llm_calls", "input_tokens", "output_tokens",
                                                "cache_read_tokens", "tool_calls", "tool_errors")}
        self.events = [{"id": "tool", "kind": "tool", "tool_name": "patch", "start": 1,
                        "end": 1.1, "status": "error", "detail": "Wrong argument format"}]

    def extracted(self):
        return "Done", self.metrics, [{"id": "actor"}], self.events, {}

    def test_recovery_preserves_results_and_is_idempotent(self):
        with patch.object(import_traces.native, "_extract", return_value=self.extracted()) as extract:
            self.assertEqual(import_traces.recover(self.data, self.micro, "bench-import-example"), (1, 1))
            recovered = self.store.require("runs", "run")
            for field in ("passed", "wall_seconds", "source_record", *self.metrics):
                self.assertEqual(recovered[field], self.run[field])
            self.assertEqual(recovered["events"], self.events)
            trace = Path(recovered["_dir"], recovered["artifacts"][0]["path"])
            self.assertEqual(json.loads(trace.read_text())["final_reply"], "Done")
            snapshot = extract.call_args.args[0]
            self.assertNotIn(Path(recovered["_dir"]), snapshot.parents)
            self.assertEqual(import_traces.recover(self.data, self.micro, "bench-import-example"), (1, 1))
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(self.store.require("runs", "run"), recovered)

    def test_mismatched_metrics_do_not_change_recorded_run(self):
        result = list(copy.deepcopy(self.extracted()))
        result[1]["tool_calls"] = 2
        with patch.object(import_traces.native, "_extract", return_value=result):
            with self.assertRaisesRegex(ValueError, "Metrics mismatch"):
                import_traces.recover(self.data, self.micro, "bench-import-example")
        self.assertEqual(self.store.require("runs", "run"), self.run)


if __name__ == "__main__":
    unittest.main()
