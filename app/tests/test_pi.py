import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app import pi, timeline


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = io.BytesIO(b"".join(json.dumps(line).encode() + b"\n" for line in lines))
        self.returncode = returncode
        self.pid = 123

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class PiAdapterTests(unittest.TestCase):
    def run_pi(self, lines, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, output = root / "work", root / "output"
            work.mkdir()
            with mock.patch("app.pi.native.sandbox_command", side_effect=lambda _w, _h, command, **_k: command) as sandbox, \
                 mock.patch("app.pi.subprocess.Popen", return_value=FakeProcess(lines)), \
                 mock.patch("app.pi.native._terminate"):
                result = pi.run_agent("/usr/local/bin/pi", work, output, "do work", "gpt", **kwargs)
            files = {path.name: path.read_bytes() for path in output.iterdir() if path.is_file()}
            return result, files, sandbox

    def test_normalizes_messages_tools_metrics_and_raw_evidence(self):
        lines = [
            {"type": "session", "id": "session-1", "timestamp": "2025-01-01T00:00:00Z"},
            {"type": "message_start", "message": {"id": "m1", "role": "assistant", "content": [], "timestamp": 1735689601000}},
            {"type": "message_update", "usage": {"input": 12, "output": 3, "cacheRead": 2, "cacheWrite": 1}},
            {"type": "tool_execution_start", "toolCallId": "t1", "toolName": "bash", "args": {"command": "pwd"}},
            {"type": "tool_execution_end", "toolCallId": "t1", "toolName": "bash", "result": {"content": [{"type": "text", "text": "/work"}]}, "isError": False, "durationMs": 7},
            {"type": "message_end", "message": {"id": "m1", "role": "assistant", "timestamp": 1735689602000, "content": [{"type": "text", "text": "done"}, {"type": "toolCall", "id": "t1", "name": "bash", "arguments": {"command": "pwd"}}]}},
            {"type": "agent_settled", "aborted": False},
        ]
        result, files, sandbox = self.run_pi(lines, options={"provider": "example", "extensions": []})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["text"], "done")
        self.assertEqual(result["conversation_id"], "session-1")
        self.assertEqual(result["metrics"], {"input_tokens": 12, "output_tokens": 3, "cache_read_tokens": 2, "cache_write_tokens": 1, "llm_calls": 1, "tool_calls": 1, "tool_errors": 0, "subagents": 0})
        command = sandbox.call_args.args[2]
        self.assertIn("--no-extensions", command)
        self.assertEqual(command[command.index("--provider") + 1], "example")
        tool = next(event for event in result["events"] if event["kind"] == "tool")
        self.assertEqual(tool["duration_ms"], 7)
        self.assertEqual(tool["duration_source"], "Pi tool execute() monotonic clock")
        self.assertIn(tool["timing_source"], ("Pi event timestamp", "observed JSONL receipt time"))
        self.assertIn("pi.events.raw.jsonl", files)
        self.assertIn("pi.session.json", files)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            agent = run_dir / "agent"
            agent.mkdir(parents=True)
            (agent / "trajectory.full.jsonl").write_bytes(files["trajectory.full.jsonl"])
            replies, tools = timeline.recorded_content({"_dir": str(run_dir)})
            model = next(event for event in result["events"] if event["kind"] == "model")
            self.assertIn(model["id"], replies)
            self.assertEqual(next(iter(replies.values()))[0]["Text"], "done")
            self.assertEqual(tools[("session-1", "t1")]["input"], {"command": "pwd"})
            self.assertEqual(tools[("session-1", "t1")]["output"]["ToolResult"][0]["Text"], "/work")

    def test_idless_messages_use_final_usage_and_observed_completion(self):
        message = {"role": "assistant", "timestamp": 1735689601000, "content": []}
        lines = [
            {"type": "session", "id": "session"},
            {"type": "message_start", "message": message},
            {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": "thinking"}},
            {"type": "message_end", "message": {**message, "content": [{"type": "thinking", "thinking": "Plan"}],
                                                "usage": {"input": 10, "output": 4, "cacheRead": 3}, "stopReason": "stop"}},
            {"type": "agent_settled", "aborted": False},
        ]
        result, files, _ = self.run_pi(lines)
        self.assertEqual(result["metrics"]["input_tokens"], 10)
        self.assertEqual(result["metrics"]["output_tokens"], 4)
        self.assertTrue(all(value == 0 for value in result["auxiliary"].values()))
        event = next(e for e in result["events"] if e["kind"] == "model")
        self.assertGreater(event["end"], event["start"])
        self.assertEqual(event["timing_source"], "observed JSONL receipt time")
        row = next(json.loads(line) for line in files["trajectory.full.jsonl"].splitlines())
        self.assertEqual(event["id"], f'session:{row["sequence_id"]}:model')
        self.assertIn("not provider HTTP request", timeline.formatted(event))

    def test_provider_error_is_not_a_completed_coding_run(self):
        lines = [
            {"type": "message_start", "message": {"role": "assistant", "content": []}},
            {"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "provider rejected request"}},
            {"type": "agent_settled", "aborted": False},
        ]
        result, _, _ = self.run_pi(lines)
        self.assertEqual(result["status"], "error")
        self.assertIn("provider rejected request", result["error"])

    def test_completed_reasoning_is_exported_before_progress_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/"work";output=root/"output";work.mkdir()
            lines=[
                {"type":"session","id":"s"},
                {"type":"message_start","message":{"role":"assistant","content":[]}},
                {"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"Recorded plan"}]}},
                {"type":"agent_settled","aborted":False},
            ]
            observed=[]
            def emit(event):
                if event.get("kind")=="progress":
                    observed.append((output/"trajectory.full.jsonl").read_text())
            with mock.patch("app.pi.native.sandbox_command",side_effect=lambda _w,_h,command:command),mock.patch("app.pi.subprocess.Popen",return_value=FakeProcess(lines)),mock.patch("app.pi.native._terminate"):
                result=pi.run_agent("pi",work,output,"task","model",emit=emit)
            self.assertEqual(result["status"],"completed")
            self.assertTrue(any("Recorded plan" in trace for trace in observed))
            self.assertFalse((output/"trajectory.full.jsonl.tmp").exists())

    def test_process_exit_without_settlement_is_an_error(self):
        result, _files, _sandbox = self.run_pi([{"type": "session", "id": "s"}])
        self.assertEqual(result["status"], "error")
        self.assertIn("agent_settled", result["error"])

    def test_aborted_settlement_is_an_error_and_tool_errors_are_counted(self):
        lines = [
            {"type": "session", "id": "s"},
            {"type": "tool_execution_start", "toolCallId": "x", "toolName": "edit", "args": {}},
            {"type": "tool_execution_end", "toolCallId": "x", "toolName": "edit", "result": {"content": []}, "isError": True},
            {"type": "agent_settled", "aborted": True},
        ]
        result, _files, _sandbox = self.run_pi(lines)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["metrics"]["tool_errors"], 1)
        self.assertIn("aborted", result["error"])

    def test_config_and_extension_paths_must_be_explicit_safe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, output = root / "work", root / "output"
            work.mkdir()
            with self.assertRaises(ValueError):
                pi.run_agent("pi", work, output, "x", "m", options={"config_dir": "relative"})
            with self.assertRaises(ValueError):
                pi.run_agent("pi", work, output, "x", "m", options={"extensions": ["/missing.ts"]})

    def test_cancelled_before_process_completion_is_reported(self):
        cancelled = threading.Event()
        cancelled.set()
        result, _files, _sandbox = self.run_pi([], cancel=cancelled)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"], "cancelled")

    def test_zero_deadline_is_reported_as_timeout(self):
        result, _files, _sandbox = self.run_pi([], timeout=0)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["error"], "Pi actor timeout")


if __name__ == "__main__":
    unittest.main()
