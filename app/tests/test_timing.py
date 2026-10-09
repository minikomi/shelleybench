import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import native, timeline


class RequestTimingTests(unittest.TestCase):
    def run_record(self):
        return {
            "id": "run", "version": "timed", "wall_seconds": 50, "passed": True,
            "actors": [{"id": "actor", "llm_calls": 3}],
            "events": [
                {"id": "read", "actor_id": "actor", "kind": "tool", "tool_name": "bash",
                 "start": 0, "end": 1, "detail": 'input: {"command":"cat file"}'},
                {"id": "first", "actor_id": "actor", "kind": "model", "label": "OpenAI",
                 "start": 1, "end": 20, "usage": {"output_tokens": 10}},
                {"id": "second", "actor_id": "actor", "kind": "model", "label": "OpenAI",
                 "start": 21, "end": 30, "usage": {"output_tokens": 10}},
                {"id": "old", "actor_id": "actor", "kind": "model", "label": "OpenAI",
                 "start": 35, "end": 35},
                {"id": "edit", "actor_id": "actor", "kind": "tool", "tool_name": "apply_patch",
                 "start": 40, "end": 41},
            ],
        }

    def test_activity_bars_match_recorded_text_and_unknown_replies_remain_points(self):
        run = self.run_record()
        replies = {
            "first": [{"Type": 3, "Thinking": "Inspecting the code"}],
            "second": [{"Type": 2, "Text": "Finished the change"}],
            "old": [{"Type": 2, "Text": "Earlier reply"}],
        }
        with patch("app.timeline.recorded_content", return_value=(replies, {})):
            html = timeline.render([run], {}, {}, {}, span=50)
        self.assertEqual(html.count('class="blk t-thinking'), 1)
        self.assertEqual(html.count('class="blk t-chat'), 2)
        self.assertIn('class="blk t-chat point"', html)
        self.assertIn("19.00s", html)
        self.assertIn("includes retries and backoff", html)
        self.assertIn("left:2.000%;width:38.000%", html)
        self.assertIn('class="activity-duration">Unknown</span>', html)
        self.assertNotIn("Model request ·", html)
        self.assertIn('class="legend-filter legend-clear"', html)
        self.assertIn('aria-pressed="false"', html)

    def test_tool_calls_from_one_response_remain_individual(self):
        events = [
            {"id": "request", "actor_id": "actor", "kind": "model", "start": 0, "end": 2},
            {"id": "actor:4:tool:a", "actor_id": "actor", "kind": "tool", "tool_name": "bash", "start": 2.01, "end": 2.03},
            {"id": "actor:4:tool:b", "actor_id": "actor", "kind": "tool", "tool_name": "bash", "start": 2.02, "end": 2.04},
        ]
        tools = {("actor", key): {"request_id": "request", "input": {"command": "cat file"}} for key in ("a", "b")}
        groups = timeline.activities({"events": events}, {}, tools)
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(group["tools"]) == 1 for group in groups))
        self.assertEqual([group["start"] for group in groups], [0, 2.02])
        self.assertEqual(events[1]["start"], 2.01)

    def test_each_timeline_has_collapsed_history_and_bar_opens_it(self):
        run = self.run_record()
        other = {**run, "id": "other", "version": "After"}
        with patch("app.timeline.recorded_content", return_value=({}, {})):
            html = timeline.render([run, other], {}, {}, {})
        self.assertEqual(html.count('class="activity-history"'), 2)
        self.assertNotIn('class="activity-history" open', html)
        self.assertLess(html.index('class="activity-history"'), html.index('>After</strong>'))
        self.assertIn("closest(&#x27;.activity-history&#x27;).open=true", html)

    def test_patch_rejection_markers_do_not_include_other_tool_errors(self):
        run = self.run_record()
        run["version"] = "Normal nested"
        run["events"][0]["status"] = "error"
        run["events"][-1]["status"] = "error"
        with patch("app.timeline.recorded_content", return_value=({}, {})):
            html = timeline.render([run], {}, {}, {}, patch_focus=True, roles={"Normal nested": "Baseline"})
        self.assertEqual(html.count(' err patch-rejected"'), 1)
        self.assertIn("1 rejected patch calls", html)
        self.assertIn("Baseline", html)
        self.assertIn("Open rejected patch at 40.0s", html)
        self.assertIn("patch rejected</span>", html)

    def test_horizontal_scroll_is_bound_to_each_timeline_group(self):
        run = self.run_record()
        other = {**run, "id": "other"}
        with patch("app.timeline.recorded_content", return_value=({}, {})):
            html = timeline.render([run, other], {}, {}, {}, group_labels={"run": "Pair one", "other": "Pair two"})
        self.assertEqual(html.count('data-on:scroll='), 2)
        self.assertIn("source.closest(&#x27;.tl-pair&#x27;)||source.closest(&#x27;.tl&#x27;)", html)
        self.assertIn("peer.scrollLeft!==source.scrollLeft", html)
        self.assertNotIn("peer.scrollTop", html)

    def test_fixed_lane_labels_are_outside_horizontal_plot(self):
        run = self.run_record()
        with patch("app.timeline.recorded_content", return_value=({}, {})):
            html = timeline.render([run], {}, {}, {})
        self.assertIn('class="lane-label" title="main"', html)
        self.assertLess(html.index('class="version-head"'), html.index('class="tl-scroll"'))
        self.assertLess(html.index('class="lane-labels"'), html.index('class="tl-scroll"'))
        plot = html.split('class="tl-scroll"', 1)[1].split('class="activity-history"', 1)[0]
        self.assertNotIn('class="lane-label"', plot)

    def test_native_and_imported_records_preserve_glm_thinking_and_tool_output(self):
        rows = [
            {"record_type": "message", "conversation_id": "actor", "sequence_id": 4, "type": "agent",
             "llm_data": {"Content": [{"Type": 3, "Thinking": "Inspect the source"},
                                     {"Type": 5, "ID": "call", "ToolName": "bash", "ToolInput": {"command": "cat file"}}]}},
            {"record_type": "message", "conversation_id": "actor", "sequence_id": 5, "type": "user",
             "llm_data": {"Content": [{"ToolUseID": "call", "ToolResult": [{"Text": "recorded file contents"}]}]}},
        ]
        for source in ("native", "import"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                directory = root / "imports" / "bench"
                directory.mkdir(parents=True)
                if source == "native":
                    (directory / "agent").mkdir()
                    (directory / "agent/trajectory.full.jsonl").write_text("\n".join(map(json.dumps, rows)))
                else:
                    snapshot = root / "import-trace-snapshots/bench/run.sqlite3"
                    snapshot.parent.mkdir(parents=True)
                    with sqlite3.connect(snapshot) as db:
                        db.execute("CREATE TABLE messages (conversation_id TEXT,sequence_id INTEGER,type TEXT,llm_data TEXT)")
                        db.executemany("INSERT INTO messages VALUES (?,?,?,?)",
                                       [(row["conversation_id"], row["sequence_id"], row["type"], json.dumps(row["llm_data"])) for row in rows])
                run = {"id": "run", "benchmark_id": "bench", "_dir": str(directory)}
                replies, tools = timeline.recorded_content(run)
                self.assertEqual(timeline.response_text(replies["actor:4:model"])[0]["Text"], "Inspect the source")
                tool = {"id": "actor:4:tool:call", "actor_id": "actor", "kind": "tool", "tool_name": "bash"}
                self.assertIn("recorded file contents", timeline.formatted(tool, tools))
                self.assertEqual(tools[("actor", "call")]["request_id"], "actor:4:model")

    def test_unknown_durations_are_not_reported_as_zero(self):
        for event in [
            {"kind": "model", "start": 1, "end": 1},
            {"kind": "model", "start": 1},
            {"kind": "model", "start": 2, "end": 1},
        ]:
            self.assertIsNone(timeline.request_duration(event))
            self.assertIn("duration is unknown", timeline.formatted(event))
        run = self.run_record()
        run["events"] = [run["events"][3]]
        with patch("app.timeline.recorded_content", return_value=({"old": [{"Type": 2, "Text": "Old reply"}]}, {})):
            html = timeline.render([run], {}, {}, {})
        self.assertIn('class="activity-duration">Unknown</span>', html)
        self.assertNotIn("0.00s summed request time", html)

    def test_usage_charts_record_tokens_at_completion(self):
        html = timeline.charts([self.run_record()], {}, 50)
        self.assertIn("L400.0,69.0 L600.0,6.0", html)
        # Context remains located at request start.
        self.assertIn("M20.0,", html)

    def test_extract_preserves_recorded_openai_request_times(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "shelley.db"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE conversations (conversation_id TEXT, parent_conversation_id TEXT, slug TEXT, model TEXT, created_at TEXT)")
                db.execute("INSERT INTO conversations VALUES ('actor', NULL, 'main', 'openai', '2026-10-09T00:00:00Z')")
                db.execute("CREATE TABLE messages (conversation_id TEXT, sequence_id INTEGER, type TEXT, llm_data TEXT, usage_data TEXT, other_usage_data TEXT, model_name TEXT, llm_api_url TEXT, forked_from_message_id TEXT, created_at TEXT)")
                for index, usage in enumerate([
                    {"start_time": "2026-10-09T00:00:01Z", "end_time": "2026-10-09T00:00:03.500Z"},
                    {},
                ]):
                    db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                               ("actor", index, "agent", "{}", json.dumps(usage), "[]",
                                "openai", "/responses", None, "2026-10-09T00:00:04Z"))
                db.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           ("actor", 2, "agent", json.dumps({"Role": 1, "Content": None, "EndOfTurn": True, "ExcludedFromContext": True}),
                            "{}", "[]", None, None, None, "2026-10-09T00:00:05Z"))
            origin = datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp()
            _, metrics, _, events, _ = native._extract(path, "actor", origin)
            self.assertEqual(metrics["llm_calls"], 2)
            self.assertEqual((events[0]["start"], events[0]["end"]), (1, 3.5))
            self.assertEqual(timeline.request_duration(events[0]), 2.5)
            self.assertIsNone(timeline.request_duration(events[1]))


if __name__ == "__main__":
    unittest.main()
