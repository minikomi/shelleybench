"""Isolated adapter for actual native Shelley actor runs.

Durable SQLite records are the source of truth for completion, usage, child
actors and telemetry; CLI stdout is only used to identify the root turn.
"""
from __future__ import annotations

from . import runtime

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

Emit = Callable[[dict[str, Any]], None]
def _json(value: Any, default: Any) -> Any:
    if not value:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _number(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _seconds(value: Any, origin: float) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, float(value) - origin)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - origin)
        except ValueError:
            pass
    return max(0.0, time.time() - origin)


def _existing_dirs(paths: Sequence[str]) -> list[str]:
    return [path for path in paths if os.path.isdir(path)]


def _toolchain_root(environment: str, executable: str) -> Path | None:
    """Find an operator-selected toolchain, falling back to PATH discovery."""
    configured = os.environ.get(environment)
    if configured:
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"{environment} must name an existing directory")
        return root
    found = shutil.which(executable)
    if not found:
        return None
    binary = Path(found).resolve()
    return binary.parent.parent if binary.parent.name == "bin" else binary.parent


def sandbox_command(work: Path, home: Path, command: Sequence[str] | str,
                    extra_readonly: Sequence[Path] | None = None) -> list[str]:
    """Return a bwrap command for an actor or grader.

    This is filesystem isolation, not hostile-code security: the actor keeps
    network access for any provider the operator configures.
    Only the supplied work directory and a fresh home are writable.  Host
    homes, root, tmp, run, var/lib and /opt are deliberately not mounted.
    """
    work, home = Path(work).resolve(), Path(home).resolve()
    if not work.is_dir() or not home.is_dir():
        raise ValueError("work and home must be existing directories")
    command = ["/bin/sh", "-lc", command] if isinstance(command, str) else list(command)
    if not command:
        raise ValueError("command must not be empty")
    argv = [
        shutil.which("bwrap") or "bwrap", "--die-with-parent", "--new-session", "--unshare-pid",
        "--unshare-ipc", "--unshare-uts", "--unshare-cgroup", "--proc", "/proc",
        "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/root",
        "--dir", "/run", "--dir", "/var", "--dir", "/var/lib", "--dir", "/work",
        "--dir", "/opt",
    ]
    # Do not mount / or /opt.  These are base runtime directories only.
    for directory in _existing_dirs(("/usr", "/bin", "/sbin", "/lib", "/lib64", "/nix")):
        argv += ["--ro-bind", directory, directory]
    for path in ("/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf"):
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    # Toolchains are either explicitly configured by the operator or discovered
    # from the service PATH. They are mounted read-only into fixed sandbox paths.
    node_root = _toolchain_root("SHELLEY_BENCH_NODE_ROOT", "node")
    go_root = _toolchain_root("SHELLEY_BENCH_GO_ROOT", "go")
    browsers = os.environ.get("SHELLEY_BENCH_PLAYWRIGHT_BROWSERS")
    if node_root:
        argv += ["--ro-bind", str(node_root), "/opt/bench-node"]
    if go_root:
        argv += ["--ro-bind", str(go_root), "/opt/bench-go"]
    if browsers:
        browser_path = Path(browsers).expanduser().resolve()
        if not browser_path.is_dir():
            raise ValueError("SHELLEY_BENCH_PLAYWRIGHT_BROWSERS must name an existing directory")
        argv += ["--ro-bind", str(browser_path), "/opt/bench-browsers",
                 "--setenv", "PLAYWRIGHT_BROWSERS_PATH", "/opt/bench-browsers"]
    for path in extra_readonly or ():
        resolved = Path(path).resolve()
        if not resolved.exists() or not resolved.is_absolute():
            raise ValueError(f"extra readonly path does not exist: {path}")
        if resolved in (work, home) or str(resolved).startswith(("/home/", "/root/", "/tmp/", "/run/", "/var/lib/", "/opt/")):
            raise ValueError(f"unsafe extra readonly path: {resolved}")
        argv += ["--ro-bind", str(resolved), str(resolved)]
    argv += [
        "--bind", str(work), "/work", "--bind", str(home), "/home/shelley",
        "--setenv", "HOME", "/home/shelley", "--setenv", "XDG_CONFIG_HOME", "/home/shelley/.config",
        "--setenv", "XDG_CACHE_HOME", "/home/shelley/.cache", "--setenv", "TMPDIR", "/tmp",
        "--setenv", "XDG_DATA_HOME", "/home/shelley/.local/share",
        "--setenv", "npm_config_store_dir", "/home/shelley/.local/share/pnpm/store",
        "--setenv", "PATH", "/home/shelley/bin:/opt/bench-node/bin:/opt/bench-go/bin:/usr/local/bin:/usr/bin:/bin",
        "--setenv", "GOPATH", "/home/shelley/go",
        "--setenv", "GOCACHE", "/home/shelley/go-cache", "--setenv", "GOFLAGS", "-p=1",
        "--setenv", "GOMAXPROCS", "1", "--chdir", "/work", "--", *command,
    ]
    if go_root:
        index = argv.index("--chdir")
        argv[index:index] = ["--setenv", "GOROOT", "/opt/bench-go"]
    return argv


def available_models(binary: str) -> list[dict[str, Any]]:
    """Read Shelley's catalog without creating an inference request."""
    completed = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode:
        raise RuntimeError(f"Shelley models failed: {completed.stderr.strip() or completed.stdout.strip()}")
    models: list[dict[str, Any]] = []
    try:
        decoded = json.loads(completed.stdout)
        entries = decoded.get("models", decoded) if isinstance(decoded, dict) else decoded
        if isinstance(entries, list):
            for row in entries:
                if isinstance(row, str):
                    models.append({"id": row})
                elif isinstance(row, dict) and isinstance(row.get("id") or row.get("model"), str):
                    item = dict(row); item["id"] = item.get("id") or item.pop("model"); models.append(item)
            if models:
                return models
    except (json.JSONDecodeError, TypeError):
        pass
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or re.match(r"^(MODEL|ID|[-+| ]+$)", line, re.I):
            continue
        token = line.strip("| ").split("|")[0].strip().split()[0]
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$", token):
            models.append({"id": token})
    seen: set[str] = set()
    return [item for item in models if not (item["id"] in seen or seen.add(item["id"]))]


def _related(conn: sqlite3.Connection, root: str) -> list[str]:
    rows = conn.execute("""WITH RECURSIVE tree(id) AS (
        SELECT ? UNION ALL SELECT c.conversation_id FROM conversations c JOIN tree t
        ON c.parent_conversation_id=t.id) SELECT id FROM tree""", (root,)).fetchall()
    return [str(row[0]) for row in rows]


def _root_id(db: Path) -> str | None:
    """Fallback root lookup: never select a recent subagent as the root."""
    try:
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT conversation_id FROM conversations WHERE parent_conversation_id IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
            return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def _client_root(stdout: Path) -> str | None:
    try:
        raw = stdout.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in raw.splitlines():
        value = _json(line, {})
        if isinstance(value, dict) and isinstance(value.get("conversation_id"), str):
            return value["conversation_id"]
    match = re.search(r'"conversation_id"\s*:\s*"([^"\\]+)"', raw)
    return match.group(1) if match else None


def _completion(conn: sqlite3.Connection, root: str) -> tuple[str | None, tuple[Any, ...]]:
    """Return terminal state and a snapshot signature for quiescence checking."""
    ids = _related(conn, root)
    if not ids:
        return None, ()
    marks = ",".join("?" for _ in ids)
    conversations = conn.execute(
        f"SELECT conversation_id,agent_working,queued_messages FROM conversations WHERE conversation_id IN ({marks}) ORDER BY conversation_id", ids
    ).fetchall()
    if len(conversations) != len(ids):
        return None, ()
    busy = any(row[1] or _json(row[2], []) for row in conversations)
    jobs = 0
    try:
        jobs = int(conn.execute(f"SELECT COUNT(*) FROM background_jobs WHERE conversation_id IN ({marks}) AND NOT notified", ids).fetchone()[0])
    except sqlite3.Error:
        pass
    terminal: list[tuple[Any, ...]] = []
    states: list[str] = []
    for cid in ids:
        # Including user messages is important: a trailing queued/user turn is
        # not terminal merely because an earlier agent response ended a turn.
        row = conn.execute("SELECT sequence_id,type,llm_data FROM messages WHERE conversation_id=? AND type IN ('user','agent','error') ORDER BY sequence_id DESC LIMIT 1", (cid,)).fetchone()
        if not row:
            return None, ()
        data = _json(row[2], {})
        terminal.append((cid, row[0], row[1], bool(data.get("EndOfTurn"))))
        if row[1] == "error" and data.get("EndOfTurn") is True:
            states.append("error")
        elif row[1] == "agent" and data.get("EndOfTurn") is True:
            states.append("completed")
        else:
            states.append("pending")
    signature = (tuple((r[0], bool(r[1]), json.dumps(_json(r[2], []), sort_keys=True)) for r in conversations), jobs, tuple(terminal))
    if busy or jobs or "pending" in states:
        return None, signature
    return ("error" if "error" in states else "completed"), signature


def _terminate(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def _time(value: Any, origin: float, fallback: float) -> float:
    return _seconds(value, origin) if isinstance(value, str) and value else fallback


def _cost(usage: dict[str, Any]) -> float | None:
    for key in ("cost_usd", "total_cost_usd", "cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _tool_detail(block: dict[str, Any]) -> str:
    payload = {key: block[key] for key in ("Text", "Data", "ToolResult", "ToolError") if block.get(key) not in (None, "", False)}
    return json.dumps(payload, ensure_ascii=False)[:2000] if payload else "tool completed"


def _extract(db: Path, root: str, origin: float) -> tuple[str, dict[str, int], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    metrics = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0, "tool_calls": 0, "tool_errors": 0, "subagents": 0}
    aux = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0}
    actors: list[dict[str, Any]] = []; events: list[dict[str, Any]] = []; final = ""
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        ids = _related(conn, root)
        metrics["subagents"] = max(0, len(ids) - 1)
        if not ids:
            return final, metrics, actors, events, aux
        marks = ",".join("?" for _ in ids)
        conversations = {r["conversation_id"]: r for r in conn.execute(f"SELECT conversation_id,parent_conversation_id,slug,model,created_at FROM conversations WHERE conversation_id IN ({marks})", ids)}
        buckets = {cid: {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0} for cid in ids}
        rows = conn.execute(f"SELECT conversation_id,sequence_id,type,llm_data,usage_data,other_usage_data,model_name,llm_api_url,forked_from_message_id,created_at FROM messages WHERE conversation_id IN ({marks}) ORDER BY created_at,conversation_id,sequence_id", ids).fetchall()
        open_tools: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            data, usage = _json(row["llm_data"], {}), _json(row["usage_data"], {})
            own = row["forked_from_message_id"] is None
            real = own and row["type"] == "agent" and bool(row["model_name"] or row["llm_api_url"] or any(_number(usage.get(k)) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")))
            at = _seconds(row["created_at"], origin)
            if real:
                values = {"input_tokens": _number(usage.get("input_tokens")), "output_tokens": _number(usage.get("output_tokens")), "cache_read_tokens": _number(usage.get("cache_read_input_tokens")), "cache_write_tokens": _number(usage.get("cache_creation_input_tokens")), "llm_calls": 1}
                for key, value in values.items():
                    metrics[key] += value; buckets[row["conversation_id"]][key] += value
                events.append({"id": f"{row['conversation_id']}:{row['sequence_id']}:model", "actor_id": row["conversation_id"], "kind": "model", "label": row["model_name"] or "model", "start": _time(usage.get("start_time"), origin, at), "end": _time(usage.get("end_time"), origin, at), "detail": "Shelley model response" if usage.get("start_time") else "Shelley model response (request start unavailable)", "status": "complete", "usage": values, "cost_usd": _cost(usage)})
            if own:
                for item in _json(row["other_usage_data"], []):
                    if isinstance(item, dict):
                        aux["llm_calls"] += 1
                        aux["input_tokens"] += _number(item.get("input_tokens")); aux["output_tokens"] += _number(item.get("output_tokens"))
                        aux["cache_read_tokens"] += _number(item.get("cache_read_input_tokens")); aux["cache_write_tokens"] += _number(item.get("cache_creation_input_tokens"))
            content = (data.get("Content") or []) if isinstance(data, dict) else []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if real and block.get("ToolName"):
                    metrics["tool_calls"] += 1
                    tool_id = str(block.get("ID") or f"{row['sequence_id']}:{metrics['tool_calls']}")
                    detail = json.dumps(block.get("ToolInput", {}), ensure_ascii=False)[:1000]
                    event = {"id": f"{row['conversation_id']}:{row['sequence_id']}:tool:{tool_id}", "actor_id": row["conversation_id"], "kind": "tool", "label": str(block["ToolName"]), "tool_name": str(block["ToolName"]), "start": at, "end": None, "detail": f"input: {detail}", "status": "running"}
                    events.append(event); open_tools[(row["conversation_id"], tool_id)] = event
                result_id = block.get("ToolUseID")
                if own and result_id:
                    event = open_tools.get((row["conversation_id"], str(result_id)))
                    if event:
                        event["start"] = _time(block.get("ToolUseStartTime"), origin, event["start"])
                        event["end"] = _time(block.get("ToolUseEndTime"), origin, at)
                        failed = bool(block.get("ToolError"))
                        event["status"] = "error" if failed else "complete"
                        event["detail"] += "\nresult: " + _tool_detail(block)
                        if failed:
                            metrics["tool_errors"] += 1
            if row["conversation_id"] == root and row["type"] == "agent" and isinstance(data, dict):
                text = "".join(str(b.get("Text", "")) for b in content if isinstance(b, dict) and b.get("Type") == 2)
                if text:
                    final = text
        for cid in ids:
            c = conversations.get(cid)
            if c:
                actors.append({"id": cid, "parent_id": c["parent_conversation_id"], "slug": c["slug"] or cid, "model": c["model"] or "", "started_at": c["created_at"], **buckets[cid]})
    events.sort(key=lambda event: (event.get("start", 0), event["id"]))
    return final, metrics, actors, events, aux


def _backup_db(source: Path, destination: Path) -> None:
    """SQLite backup includes committed WAL contents unlike shutil.copy2."""
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dest:
        src.backup(dest)


def _dump_trajectory(db: Path, root: str, destination: Path) -> None:
    with sqlite3.connect(db) as conn, destination.open("w", encoding="utf-8") as out:
        conn.row_factory = sqlite3.Row
        for cid in _related(conn, root):
            conversation = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (cid,)).fetchone()
            if conversation:
                out.write(json.dumps({"record_type": "conversation", **dict(conversation)}, ensure_ascii=False) + "\n")
            for row in conn.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY sequence_id", (cid,)):
                item = dict(row)
                for key in ("llm_data", "user_data", "usage_data", "other_usage_data", "display_data"):
                    if key in item:
                        item[key] = _json(item[key], None)
                out.write(json.dumps({"record_type": "message", **item}, ensure_ascii=False) + "\n")


def _artifacts(output: Path) -> list[dict[str, Any]]:
    # The fresh runtime home can contain caches/WAL files; expose the compact
    # evidence set, including the backed-up durable database, rather than it.
    return [{"name": str(path.relative_to(output)), "size": path.stat().st_size}
            for path in sorted(output.rglob("*")) if path.is_file() and path.relative_to(output).parts[0] != "home"]


def run_agent(binary: str, work: Path, output: Path, prompt: str, model: str,
              reasoning: str = "medium", timeout: int = 1800,
              cancel: threading.Event | None = None, emit: Emit | None = None,
              no_tools: bool = False, runtime_profile: dict | None = None) -> dict[str, Any]:
    """Run one isolated native Shelley root turn and capture its actor tree."""
    work, output = Path(work).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "actor_prompt.txt").write_text(prompt, encoding="utf-8")
    home = output / "home"; (home / ".config" / "shelley").mkdir(parents=True, exist_ok=True)
    (home / "go").mkdir(exist_ok=True); (home / "go-cache").mkdir(exist_ok=True)
    socket_dir = tempfile.TemporaryDirectory(prefix="bench-socket-", dir="/tmp") if no_tools else None
    runtime.install(home, runtime_profile)
    db = home / "shelley.db"
    socket = Path(socket_dir.name) / "shelley.sock" if socket_dir else home / ".config" / "shelley" / "shelley.sock"
    status = "running"; root: str | None = None; error = ""; server = client = None
    actor_started: float | None = None; actor_finished: float | None = None
    def note(payload: dict[str, Any]) -> None:
        if emit:
            emit(payload)
    virtual = not no_tools
    db_arg = "/home/shelley/shelley.db" if virtual else str(db)
    home_arg = "/home/shelley" if virtual else str(home)
    work_arg = "/work" if virtual else str(work)
    socket_arg = str(socket) if no_tools else f"{home_arg}/.config/shelley/shelley.sock"
    server_base = [binary, "-db", db_arg, "serve", "-port", "0", "-port-file", f"{home_arg}/port", "-socket", socket_arg]
    if no_tools:
        request_file = output / "client_request.json"
        request_file.write_text(json.dumps({"message": prompt, "model": model, "cwd": work_arg,
            "conversation_options": {"disable_all_tools": True, "disable_notifications": True, "thinking_level": reasoning}}))
        client_base = ["/usr/bin/curl", "--silent", "--show-error", "--fail-with-body", "--max-time", str(timeout),
                       "--unix-socket", socket_arg, "-H", "Content-Type: application/json",
                       "--data-binary", f"@{request_file}", "http://localhost/api/conversations/new"]
    else:
        client_base = [binary, "client", "-url", f"unix://{socket_arg}", "chat", "-cwd", work_arg, "-model", model, "-reasoning", reasoning, "-disable-notifications", "-p", prompt]
    env = os.environ.copy()
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"), "GOPATH": str(home / "go"), "GOCACHE": str(home / "go-cache"), "GOFLAGS": "-p=1", "GOMAXPROCS": "1"})
    stdout_path, stderr_path = output / "client.stdout.log", output / "client.stderr.log"
    server_out = (output / "server.stdout.log").open("w"); server_err = (output / "server.stderr.log").open("w")
    try:
        deadline = time.monotonic() + timeout
        server_cmd = server_base if no_tools else sandbox_command(work, home, server_base)
        server = subprocess.Popen(server_cmd, stdin=subprocess.DEVNULL, stdout=server_out, stderr=server_err, start_new_session=True, env=env if no_tools else None)
        while not socket.exists() and server.poll() is None:
            if cancel and cancel.is_set():
                raise InterruptedError("cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("timeout starting isolated Shelley")
            time.sleep(.05)
        if server.poll() is not None:
            raise RuntimeError("isolated Shelley server exited before creating its socket")
        client_cmd = client_base if no_tools else sandbox_command(work, home, client_base)
        actor_started = time.time()
        note({"kind": "message", "status": "running", "label": "actor launched", "start": actor_started})
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            client = subprocess.Popen(client_cmd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True, env=env if no_tools else None)
            while client.poll() is None:
                if cancel and cancel.is_set():
                    raise InterruptedError("cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("actor timeout")
                time.sleep(.1)
        if client.returncode:
            detail = stderr_path.read_text(errors="replace").strip()[-2000:]
            response = stdout_path.read_text(errors="replace").strip()[-2000:]
            raise RuntimeError(f"Shelley client exited {client.returncode}: {detail or response}")
        root = _client_root(stdout_path) or _root_id(db)
        if not root:
            raise RuntimeError("Shelley did not persist a root conversation")
        note({"kind": "message", "status": "running", "label": "actor started", "conversation_id": root, "start": actor_started})
        stable: tuple[str, tuple[Any, ...]] | None = None; next_live = time.monotonic()
        while time.monotonic() < deadline:
            if cancel and cancel.is_set():
                raise InterruptedError("cancelled")
            try:
                with sqlite3.connect(db) as conn:
                    current, snapshot = _completion(conn, root)
                if current and stable == (current, snapshot):
                    status = current; actor_finished = time.time(); break
                stable = (current, snapshot) if current else None
                if actor_started and time.monotonic() >= next_live:
                    text, metrics, actors, events, auxiliary = _extract(db, root, actor_started)
                    note({"kind": "progress", "status": "running", "conversation_id": root, "wall_seconds": round(time.time() - actor_started, 3), "metrics": metrics, "actors": actors, "events": events, "auxiliary": auxiliary, "text": text})
                    next_live = time.monotonic() + 1
            except sqlite3.Error:
                stable = None
            time.sleep(.1)
        if status not in {"completed", "error"}:
            raise TimeoutError("timeout waiting for Shelley descendants/background work")
        if status == "error":
            error = "Shelley recorded a terminal provider error"
    except InterruptedError as exc:
        status = "cancelled"; error = str(exc); actor_finished = actor_finished or time.time()
    except TimeoutError as exc:
        status = "timeout"; error = str(exc); actor_finished = actor_finished or time.time()
    except Exception as exc:
        status = "error"; error = f"{type(exc).__name__}: {exc}"; actor_finished = actor_finished or time.time()
    finally:
        _terminate(client); _terminate(server); server_out.close(); server_err.close()
        if socket_dir:
            socket_dir.cleanup()
    origin = actor_started or actor_finished or time.time()
    wall = round(max(0.0, (actor_finished or origin) - origin), 3)
    if db.exists() and root:
        try:
            _backup_db(db, output / "shelley.db")
            _dump_trajectory(db, root, output / "trajectory.full.jsonl")
            text, metrics, actors, events, auxiliary = _extract(db, root, origin)
        except sqlite3.Error as exc:
            text, metrics, actors, events, auxiliary = "", {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0, "tool_calls": 0, "tool_errors": 0, "subagents": 0}, [], [], {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0}
            status, error = "error", f"SQLite evidence export failed: {exc}"
    else:
        text, metrics, actors, events, auxiliary = "", {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0, "tool_calls": 0, "tool_errors": 0, "subagents": 0}, [], [], {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0}
    runtime.export(output, home)
    (output / "native_request.json").write_text(json.dumps({"binary": binary, "model": model, "reasoning": reasoning, "no_tools": no_tools, "runtime_profile": runtime_profile, "work": str(work), "actor_started_at": actor_started, "actor_finished_at": actor_finished}, indent=2) + "\n")
    (output / "result.json").write_text(json.dumps({"status": status, "conversation_id": root, "metrics": metrics, "auxiliary": auxiliary, "error": error}, indent=2) + "\n")
    result = {"text": text, "status": status, "error": error or None, "wall_seconds": wall, "metrics": metrics, "auxiliary": auxiliary, "actors": actors, "events": events, "artifacts": _artifacts(output), "conversation_id": root}
    note({"kind": "message" if status == "completed" else "error", "status": status, "label": f"actor {status}", "conversation_id": root, "wall_seconds": wall, "metrics": metrics, "actors": actors, "events": events})
    return result
