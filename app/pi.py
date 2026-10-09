"""Isolated Pi JSON-event adapter.

Pi's JSON stream is retained verbatim for audit.  The small normalized
``trajectory.full.jsonl`` export intentionally uses the message/content shape
consumed by :func:`app.timeline.recorded_content`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

from . import native, runtime

Emit = Callable[[dict[str, Any]], None]
_METRICS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "llm_calls", "tool_calls", "tool_errors", "subagents")


def _blank_metrics() -> dict[str, int]:
    return dict.fromkeys(_METRICS, 0)


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _event_time(value: Any, origin: float, observed: float) -> tuple[float, str]:
    """Return an origin-relative lifecycle time without inventing a duration."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Pi message timestamps are Unix milliseconds.
        return max(0.0, float(value) / 1000 - origin), "Pi event timestamp"
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - origin), "Pi event timestamp"
        except ValueError:
            pass
    return max(0.0, observed - origin), "observed JSONL receipt time"


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            return "\n".join(_text(item) for item in content if _text(item))
        for key in ("text", "content", "message", "error"):
            if value.get(key) is not None:
                return _text(value[key])
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value if _text(item))
    return ""


def _content(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content", [])
    if isinstance(content, str):
        return [{"Type": 2, "Text": content}]
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"Type": 2, "Text": item})
        elif isinstance(item, dict):
            kind = item.get("type")
            if kind == "text" and isinstance(item.get("text"), str):
                blocks.append({"Type": 2, "Text": item["text"]})
            elif kind == "thinking" and isinstance(item.get("thinking") or item.get("text"), str):
                blocks.append({"Type": 3, "Thinking": item.get("thinking") or item.get("text")})
            elif kind in ("toolCall", "tool_call"):
                blocks.append({"Type": 4, "ID": item.get("id"), "ToolName": item.get("name") or item.get("toolName"),
                               "ToolInput": item.get("arguments") if "arguments" in item else item.get("args", {})})
    return blocks


def _usage(value: Any) -> dict[str, int]:
    value = value if isinstance(value, dict) else {}
    return {
        "input_tokens": _number(value.get("input")),
        "output_tokens": _number(value.get("output")),
        "cache_read_tokens": _number(value.get("cacheRead")),
        "cache_write_tokens": _number(value.get("cacheWrite")),
    }


def _copy_config(source: Path, target: Path) -> None:
    """Copy an explicitly selected config tree, refusing links and traversal."""
    if not source.is_absolute() or not source.is_dir():
        raise ValueError("options.config_dir must be an existing absolute directory")
    for name in ("auth.json", "models.json", "settings.json"):
        path = source / name
        if path.is_symlink():
            raise ValueError("options.config_dir may not contain symlinks")
        if path.is_file():
            shutil.copy2(path, target / name)


def _copy_extension(source: Path, target: Path) -> None:
    """Copy an explicitly selected extension tree into the disposable home."""
    if not source.is_absolute() or not source.is_file():
        raise ValueError("options.extensions must contain existing absolute files")
    for path in source.parent.rglob("*"):
        if path.is_symlink():
            raise ValueError("options.extensions may not contain symlinks")
    shutil.copytree(source.parent, target, dirs_exist_ok=True)


def _artifacts(output: Path) -> list[dict[str, Any]]:
    return [{"name": str(path.relative_to(output)), "size": path.stat().st_size}
            for path in sorted(output.rglob("*")) if path.is_file() and path.relative_to(output).parts[0] != "home"]


def _reader(stream: Any, queue: Queue[bytes]) -> None:
    try:
        for line in iter(stream.readline, b""):
            queue.put(line)
    finally:
        queue.put(b"")


def run_agent(binary: str, work: Path, output: Path, prompt: str, model: str, *,
              reasoning: str = "medium", timeout: int = 1800,
              cancel: threading.Event | None = None, emit: Emit | None = None,
              runtime_profile: dict | None = None, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one Pi print-mode process in a fresh sandbox and normalize its JSONL.

    ``options`` is deliberately operator-owned: ``provider``, ``model_id``,
    ``extensions`` (a list of absolute extension files), and ``config_dir``
    (an absolute config directory copied into the fresh home) are supported.
    No ambient Pi home, provider, model, or extension is selected by this adapter.
    """
    work, output = Path(work).resolve(), Path(output).resolve()
    options = dict(options or {})
    output.mkdir(parents=True, exist_ok=True)
    (output / "actor_prompt.txt").write_text(prompt, encoding="utf-8")
    home = output / "home"
    (home / ".pi" / "agent").mkdir(parents=True, exist_ok=True)
    runtime.install(home, runtime_profile)
    config_dir = options.get("config_dir")
    if config_dir is not None:
        _copy_config(Path(config_dir).expanduser(), home / ".pi" / "agent")
    provider = options.get("provider")
    model_id = options.get("model_id", model)
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("model or options.model_id must be a non-empty string")
    if provider is not None and (not isinstance(provider, str) or not provider):
        raise ValueError("options.provider must be a non-empty string")
    extensions = options.get("extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(path, str) for path in extensions):
        raise ValueError("options.extensions must be a list of absolute file paths")
    extension_sources = [Path(path).expanduser().resolve() for path in extensions]
    if any(not path.is_absolute() or not path.is_file() for path in extension_sources):
        raise ValueError("options.extensions must contain existing absolute files")
    extension_paths = []
    for index, source in enumerate(extension_sources):
        destination = home / ".pi" / "agent" / "bench-extensions" / str(index)
        _copy_extension(source, destination)
        extension_paths.append(Path("/home/shelley/.pi/agent/bench-extensions") / str(index) / source.name)
    command = [binary, "--mode", "json", "--print", "--no-extensions", "--no-skills", "--no-prompt-templates",
               "--session-dir", "/home/shelley/.pi/agent/sessions", "--thinking", reasoning]
    if provider:
        command += ["--provider", provider]
    command += ["--model", model_id]
    for path in extension_paths:
        command += ["--extension", str(path)]
    command.append(prompt)
    request = {"binary": binary, "model": model_id, "provider": provider, "reasoning": reasoning,
               "extensions": [str(path) for path in extension_paths], "config_dir": bool(config_dir),
               "work": str(work)}
    (output / "pi_request.json").write_text(json.dumps(request, indent=2) + "\n")

    status, error, conversation_id = "running", "", None
    metrics, events, records = _blank_metrics(), [], []
    actors: list[dict[str, Any]] = []
    text, started, finished, settled = "", None, None, False
    process = None
    raw_path, stderr_path = output / "pi.events.raw.jsonl", output / "pi.stderr.log"
    open_tools: dict[str, dict[str, Any]] = {}
    message_events: dict[str, dict[str, Any]] = {}
    latest_usage: dict[str, int] = {}
    active_message: dict[str, Any] | None = None
    sequence = 0

    def write_trace():
        target = output / "trajectory.full.jsonl"
        temporary = output / "trajectory.full.jsonl.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        temporary.replace(target)

    def note(payload: dict[str, Any]) -> None:
        if emit:
            emit(payload)

    def save_message(message: Any) -> int | None:
        nonlocal sequence, text
        if not isinstance(message, dict):
            return
        role = message.get("role")
        if role not in ("assistant", "user"):
            return
        sequence += 1
        content = _content(message)
        records.append({"record_type": "message", "conversation_id": conversation_id or "pi",
                        "sequence_id": sequence, "type": "agent" if role == "assistant" else "user",
                        "llm_data": {"Content": content}, "pi_message": message})
        if role == "assistant":
            reply = "".join(str(block.get("Text", "")) for block in content if block.get("Type") == 2)
            if reply:
                text = reply
        return sequence

    def save_tool_result(tool_id: str, result: Any, failed: bool) -> None:
        nonlocal sequence
        sequence += 1
        result_block: dict[str, Any] = {"ToolUseID": tool_id, "ToolResult": [{"Type": 2, "Text": _text(result)}]}
        if failed:
            result_block["ToolError"] = True
        records.append({"record_type": "message", "conversation_id": conversation_id or "pi",
                        "sequence_id": sequence, "type": "tool_result",
                        "llm_data": {"Content": [result_block]}})

    def handle(event: dict[str, Any], observed: float) -> None:
        nonlocal conversation_id, settled, error, active_message
        kind = event.get("type")
        if kind == "session":
            conversation_id = event.get("id") if isinstance(event.get("id"), str) else conversation_id
            (output / "pi.session.json").write_text(json.dumps(event, indent=2) + "\n")
        if kind == "message_update" and isinstance(event.get("usage"), dict):
            latest_usage.update(_usage(event.get("usage")))
        message = event.get("message")
        key = str(message.get("id") or len(message_events)) if isinstance(message, dict) else ""
        at, source = _event_time(event.get("timestamp"), started or observed, observed)
        if kind == "message_start" and isinstance(message, dict) and message.get("role") == "assistant":
            model_event = {"id": f"{conversation_id or 'pi'}:{key}:model", "actor_id": conversation_id or "pi",
                           "kind": "model", "label": model_id, "start": at, "end": None, "status": "running",
                           "detail": "Pi assistant message lifecycle", "timing_source": source}
            events.append(model_event); message_events[key] = model_event; metrics["llm_calls"] += 1
            active_message = model_event
        elif kind == "message_end" and isinstance(message, dict):
            saved_sequence = save_message(message)
            model_event = active_message if message.get("role") == "assistant" else None
            if model_event:
                if isinstance(message.get("usage"), dict):
                    latest_usage.update(_usage(message["usage"]))
                failed = message.get("stopReason") in {"error", "aborted"}
                if failed:
                    error = str(message.get("errorMessage") or f'Pi assistant {message["stopReason"]}')
                model_event.update({"id": f"{conversation_id or 'pi'}:{saved_sequence}:model",
                                    "end": at, "status": "error" if failed else "complete", "timing_source": source,
                                    "usage": dict(latest_usage)})
                for name, value in latest_usage.items():
                    metrics[name] += value
                latest_usage.clear()
                active_message = None
        elif kind == "tool_execution_start":
            tool_id = str(event.get("toolCallId") or len(open_tools))
            tool = {"id": f"{conversation_id or 'pi'}:tool:{tool_id}", "actor_id": conversation_id or "pi",
                    "kind": "tool", "label": str(event.get("toolName") or "tool"), "tool_name": str(event.get("toolName") or "tool"),
                    "start": at, "end": None, "status": "running", "detail": "input: " + json.dumps(event.get("args", {}), ensure_ascii=False),
                    "timing_source": source}
            events.append(tool); open_tools[tool_id] = tool; metrics["tool_calls"] += 1
        elif kind == "tool_execution_end":
            tool_id = str(event.get("toolCallId") or "")
            tool = open_tools.get(tool_id)
            if tool:
                failed = bool(event.get("isError"))
                tool.update({"end": at, "status": "error" if failed else "complete",
                             "detail": tool["detail"] + "\nresult: " + _text(event.get("result")),
                             "timing_source": source})
                if isinstance(event.get("durationMs"), (int, float)) and not isinstance(event["durationMs"], bool):
                    tool["duration_ms"] = event["durationMs"]
                    tool["duration_source"] = "Pi tool execute() monotonic clock"
                if failed:
                    metrics["tool_errors"] += 1
                save_tool_result(tool_id, event.get("result"), failed)
        elif kind == "agent_settled":
            settled = True
            if event.get("aborted"):
                error = "Pi reported an aborted session"

    try:
        started = time.time()
        note({"kind": "message", "status": "running", "label": "actor launched", "start": started})
        invocation = native.sandbox_command(work, home, command)
        with raw_path.open("wb") as raw, stderr_path.open("wb") as stderr:
            env = {**os.environ, "PI_CODING_AGENT_DIR": "/home/shelley/.pi/agent"}
            process = subprocess.Popen(invocation, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=stderr, start_new_session=True, env=env)
            assert process.stdout is not None
            queue: Queue[bytes] = Queue()
            threading.Thread(target=_reader, args=(process.stdout, queue), daemon=True).start()
            deadline = time.monotonic() + timeout
            closed = False
            while not closed or process.poll() is None:
                if cancel and cancel.is_set():
                    raise InterruptedError("cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Pi actor timeout")
                try:
                    line = queue.get(timeout=.05)
                except Empty:
                    continue
                if not line:
                    closed = True
                    continue
                raw.write(line); raw.flush()
                try:
                    item = json.loads(line.decode("utf-8").rstrip("\r\n"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"invalid Pi JSONL: {exc}") from exc
                if not isinstance(item, dict):
                    raise RuntimeError("invalid Pi JSONL record: expected object")
                handle(item, time.time())
                if item.get("type") in {"message_start", "message_end", "tool_execution_start", "tool_execution_end"}:
                    write_trace()
                    live_actors = [{"id": conversation_id or "pi", "parent_id": None, "slug": "main", "model": model_id,
                                    **{key: metrics[key] for key in _METRICS[:5]}}]
                    note({"kind": "progress", "status": "running", "wall_seconds": round(time.time() - started, 3),
                          "metrics": dict(metrics), "events": events, "actors": live_actors})
            if process.returncode:
                detail = stderr_path.read_text(errors="replace").strip()[-2000:]
                raise RuntimeError(f"Pi exited {process.returncode}: {detail}")
            if not settled:
                raise RuntimeError("Pi exited without agent_settled evidence")
            status = "error" if error else "completed"
        finished = time.time()
    except InterruptedError as exc:
        status, error, finished = "cancelled", str(exc), time.time()
    except TimeoutError as exc:
        status, error, finished = "timeout", str(exc), time.time()
    except Exception as exc:
        status, error, finished = "error", f"{type(exc).__name__}: {exc}", time.time()
    finally:
        native._terminate(process)
    origin = started or finished or time.time()
    wall = round(max(0.0, (finished or origin) - origin), 3)
    if conversation_id:
        actors.append({"id": conversation_id, "parent_id": None, "slug": conversation_id, "model": model_id,
                       "started_at": started, **{key: metrics[key] for key in _METRICS[:5]}})
    events.sort(key=lambda item: (item.get("start", 0), item["id"]))
    write_trace()
    runtime.export(output, home)
    sessions = home / ".pi" / "agent" / "sessions"
    if sessions.is_dir():
        shutil.copytree(sessions, output / "pi-sessions", dirs_exist_ok=True)
    auxiliary = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "llm_calls"), 0)
    (output / "result.json").write_text(json.dumps({"status": status, "conversation_id": conversation_id,
        "metrics": metrics, "auxiliary": auxiliary, "error": error or None}, indent=2) + "\n")
    result = {"text": text, "status": status, "error": error or None, "wall_seconds": wall, "metrics": metrics,
              "auxiliary": auxiliary, "actors": actors, "events": events, "artifacts": _artifacts(output),
              "conversation_id": conversation_id}
    note({"kind": "message" if status == "completed" else "error", "status": status,
          "label": f"actor {status}", "conversation_id": conversation_id, "wall_seconds": wall,
          "metrics": metrics, "actors": actors, "events": events})
    return result
