"""Frozen task controls and paired Shelley benchmark execution."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import shlex
import subprocess
import threading
import time
from typing import Any, Callable

from . import gitops, grading, native

Emit = Callable[[dict[str, Any]], None]
_SUCCESS = {"complete", "completed", "passed", "success"}


def _emit(emit: Emit | None, **event: Any) -> None:
    if emit:
        emit(event)


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, text: str) -> None:
    _mkdir(path.parent)
    path.write_text(text)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def _tail(path: Path, limit: int = 16_384) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - limit))
        return f.read().decode(errors="replace")


def _cancel_process(p: subprocess.Popen[Any]) -> None:
    if p.poll() is not None:
        return
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        p.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(timeout=2)


def _shell(work: Path, command: str, timeout: int, cancel: threading.Event | None, log: Path) -> dict[str, Any]:
    """Run setup/grading in bwrap without PIPE backpressure deadlocks."""
    _mkdir(log.parent)
    if cancel and cancel.is_set():
        _write(log, "cancelled before command started\n")
        return {"status": "cancelled", "returncode": None, "wall_seconds": 0.0, "stdout": "", "stderr": "", "log": str(log)}
    home = _mkdir(log.parent / "home")
    argv = native.sandbox_command(work=work, home=home, command=["sh", "-lc", command])
    started = time.monotonic()
    with log.open("w") as stream:
        p = subprocess.Popen(argv, cwd=work, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        status = "passed"
        while p.poll() is None:
            if cancel and cancel.is_set():
                _cancel_process(p); status = "cancelled"; break
            if time.monotonic() - started > timeout:
                _cancel_process(p); status = "timeout"; break
            if cancel:
                cancel.wait(0.05)
            else:
                threading.Event().wait(0.05)
        returncode = p.wait()
    excerpt = _tail(log)
    if status == "passed" and returncode:
        status = "failed"
    return {"status": status, "returncode": returncode, "wall_seconds": time.monotonic() - started,
            "stdout": excerpt, "stderr": "", "log": str(log)}


def _apply_patch(work: Path, patch: str, out: Path, cancel: threading.Event | None) -> dict[str, Any]:
    if not patch:
        return {"status": "passed"}
    file = work / ".shelley-bench-hidden.patch"
    _write(file, patch)
    name = shlex.quote(file.name)
    return _shell(work, f"(git apply --whitespace=nowarn {name} || git apply --recount --whitespace=nowarn {name}) && rm -f {name}", 120, cancel, out / "apply.log")


def _setup(work: Path, command: str, out: Path, cancel: threading.Event | None) -> dict[str, Any]:
    return _shell(work, command, 1800, cancel, out / "setup.log") if command else {"status": "passed"}


def _infra_output(value: dict[str, Any]) -> bool:
    text = (value.get("stdout", "") + "\n" + value.get("stderr", "")).lower()
    markers = ("command not found", "not found", "no module named", "modulenotfounderror", "importerror",
               "cannot find module", "err_module_not_found", "compilation failed", "fatal error:", "error ts")
    return any(marker in text for marker in markers)


def _network_output(value: dict[str, Any]) -> bool:
    text = (value.get("stdout", "") + "\n" + value.get("stderr", "")).lower()
    return any(marker in text for marker in ("network", "econn", "enotfound", "could not resolve", "connection timed out", "tls"))


def _control(work: Path, task: dict[str, Any], out: Path, cancel: threading.Event | None,
             emit: Emit | None = None, name: str = "") -> dict[str, Any]:
    label = {"base": "Original code", "reference": "Reference change", "equivalent": "Equivalent implementation"}.get(name, "Candidate implementation")
    _emit(emit, phase=f"{name}_fixture", progress=f"{label}: installing the scoring tests")
    patch = _apply_patch(work, task.get("test_patch", ""), out, cancel)
    if patch["status"] != "passed":
        return {"kind": "patch", **patch}
    if task.get("setup_command"):
        _emit(emit, phase=f"{name}_setup", progress=f"{label}: preparing dependencies and assets", detail=task["setup_command"])
    setup = _setup(work, task.get("setup_command", ""), out, cancel)
    if setup["status"] != "passed":
        return {"kind": "setup", **setup}
    _emit(emit, phase=f"{name}_check", progress=f"{label}: running the scoring command", detail=task["acceptance_command"])
    test = _shell(work, task["acceptance_command"], 1800, cancel, out / "acceptance.log")
    if test["status"] != "passed" and (test["status"] in {"cancelled", "timeout"} or _infra_output(test)):
        return {"kind": "infrastructure", **test}
    if test["status"] != "passed" and task.get("grading_contract"):
        markers = [grading.MARKER + item["id"] for item in task["grading_contract"]["criteria"]]
        output = test.get("stdout", "") + "\n" + test.get("stderr", "")
        if not any(line in markers for line in output.splitlines()):
            return {"kind": "unclassified_failure", **test}
    return {"kind": "acceptance", **test, "passed": test["status"] == "passed"}


def prepare_task(task: dict[str, Any], repo: dict[str, Any], out: Path, cancel: threading.Event | None, emit: Emit | None) -> dict[str, Any]:
    if not task.get("acceptance_command", "").strip():
        return {"status": "failed", "error": "acceptance_command is required"}
    if cancel and cancel.is_set():
        return {"status": "failed", "error": "Preparation cancelled"}
    try:
        grading.require_review(task)
        controls: dict[str, Any] = {}
        for name, ref in (("base", task["base_commit"]), ("reference", task["reference_commit"]),
                          ("equivalent", task["reference_commit"])):
            _emit(emit, phase=f"{name}_snapshot", progress=f"Creating an isolated {name} control checkout")
            work = out / name / "work"
            gitops.snapshot(repo["path"], work, ref)
            if name == "equivalent":
                applied = _apply_patch(work, task["grading_probe"]["patch"], out / name / "probe", cancel)
                if applied["status"] != "passed":
                    return {"status": "failed", "controls": controls, "error": "Equivalent-implementation probe could not be applied: " + applied["status"]}
            controls[name] = _control(work, task, out / name, cancel, emit, name)
            _emit(emit, phase=f"{name}_result", progress=f"{'Original' if name == 'base' else 'Reference'} check: {controls[name]['status']}",
                  detail=f"Expected {'an assertion failure' if name == 'base' else 'a pass'}; infrastructure failures do not qualify.")
            if controls[name]["kind"] != "acceptance":
                return {"status": "failed", "controls": controls,
                        "error": f"{name} infrastructure failure: {controls[name]['kind']} {controls[name]['status']}"}
        if controls["base"].get("passed") or not controls["reference"].get("passed"):
            return {"status": "failed", "controls": controls,
                    "error": "controls require base failure and reference success"}
        if not controls["equivalent"].get("passed"):
            return {"status": "failed", "controls": controls,
                    "error": "Implementation-independence control failed. The same grader rejected an equivalent implementation; inspect the grader and probe before using this task."}
        return {"status": "ready", "controls": controls,
                "grading_qualification": {"version": grading.VERSION, "spec_sha256": grading.fingerprint(task)}}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


def _build(source: Path, config: dict[str, Any], out: Path, cancel: threading.Event | None) -> dict[str, Any]:
    attempt = out / f"build-{time.time_ns()}"
    work = attempt / "work"
    gitops.snapshot(source, work, "WORKTREE")
    built = _shell(work, config.get("build_command", "make build"), int(config.get("timeout_seconds", 1800)), cancel, attempt / "build.log")
    if built["status"] != "passed":
        raise RuntimeError(f"build {built['status']}: {built.get('stdout', '')[-1000:]}")
    binary = (work / config.get("binary_path", "bin/shelley")).resolve()
    if work not in binary.parents or not binary.is_file():
        raise RuntimeError("configured binary_path was not built inside snapshot")
    return {"binary": str(binary), "path": str(binary), "source": str(source), "build_workspace": str(work),
            "provenance": {"binary_sha256": _sha(binary), "build": built}}


def build_variants(config: dict[str, Any], out: Path, cancel: threading.Event | None, emit: Emit | None) -> dict[str, Any]:
    try:
        manifest = out / "snapshots.json"
        if manifest.exists():
            snapshots = json.loads(manifest.read_text())
        else:
            sources = out / f"sources-{time.time_ns()}"
            _emit(emit, kind="build", label="snapshot variants")
            snapshots = {
                "baseline": gitops.snapshot(config["shelley_repo"], sources / "baseline", config.get("baseline_ref") or "upstream/main"),
                "candidate": gitops.snapshot(config["shelley_repo"], sources / "candidate", config.get("candidate_ref") or "WORKTREE"),
            }
            _write(manifest, json.dumps(snapshots, indent=2))
        variants = {}
        for name in ("baseline", "candidate"):
            _emit(emit, kind="build", label=f"build {name}")
            variants[name] = _build(Path(snapshots[name]["path"]), config, out / name, cancel)
            variants[name]["provenance"]["source"] = snapshots[name]
        return variants
    except Exception as e:
        raise RuntimeError(f"variant build failed: {e}") from e


def _agent_metrics(agent: dict[str, Any]) -> dict[str, Any]:
    metrics = agent.get("metrics") or {}
    return {key: metrics.get(key) for key in ("input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "llm_calls", "tool_calls", "tool_errors", "subagents", "cost_usd")}


def _actor_binary(variant: dict[str, Any], work: Path) -> str:
    binary, target = Path(variant["path"]), work / ".shelley-bench-bin"
    shutil.copy2(binary, target)
    target.chmod(target.stat().st_mode | 0o111)
    with (work / ".git" / "info" / "exclude").open("a") as f:
        f.write("\n.shelley-bench-bin\n")
    return "/work/.shelley-bench-bin"


def _git_diff(work: Path, base_ref: str = "HEAD") -> str:
    return gitops.worktree_patch(work, base_ref)


def _artifact_name(prefix: str, name: str) -> str | None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return None
    return f"{prefix}/{path.as_posix()}"


def _artifacts(out: Path, agent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    found: dict[str, int] = {}
    for artifact in (agent or {}).get("artifacts", []):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("name"), str):
            continue
        name = _artifact_name("agent", artifact["name"])
        file = out / name if name else None
        if name and file and file.is_file():
            found[name] = file.stat().st_size
    for directory in ("actor", "grader"):
        root = out / directory
        if root.exists():
            for file in root.rglob("*.log"):
                found[str(file.relative_to(out))] = file.stat().st_size
    patch = out / "candidate.patch"
    if patch.exists():
        found["candidate.patch"] = patch.stat().st_size
    return [{"name": name, "size": size} for name, size in sorted(found.items())]


def _run_status(value: dict[str, Any]) -> str:
    status = value.get("status")
    if status in {"cancelled", "timeout"}:
        return status
    return "infrastructure_failure"


def run_trial(task: dict[str, Any], repo: dict[str, Any], variant: dict[str, Any], model: str, reasoning: str,
              out: Path, timeout: int, cancel: threading.Event | None, emit: Emit | None) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"status": "infrastructure_failure", "passed": None, "model": model,
                              "task_id": task.get("id"), "task_title": task.get("title")}
    agent: dict[str, Any] | None = None
    try:
        work = out / "work"
        gitops.snapshot(repo["path"], work, task["base_commit"])
        starting_head = subprocess.check_output(["git", "-C", str(work), "rev-parse", "HEAD"], text=True).strip()
        setup = _setup(work, task.get("setup_command", ""), out / "actor", cancel)
        if setup["status"] != "passed":
            result.update(status=_run_status(setup), error=f"setup {setup['status']}")
            return result
        _emit(emit, kind="model", label="run actor", start=time.time())
        agent = native.run_agent(_actor_binary(variant, work), work, _mkdir(out / "agent"), task["prompt"], model,
                                 reasoning=reasoning, timeout=timeout, cancel=cancel, emit=emit, runtime_profile=variant.get("runtime_profile"))
        result.update(_agent_metrics(agent), auxiliary=agent.get("auxiliary"), actors=agent.get("actors", []),
                      events=agent.get("events", []), conversation_id=agent.get("conversation_id"),
                      wall_seconds=agent.get("wall_seconds", time.monotonic() - started))
        actor_failed = agent.get("status") not in _SUCCESS
        if actor_failed:
            result.update(status=_run_status(agent), error=agent.get("error") or agent.get("text") or f"actor {agent.get('status', 'failed')}")
        try:
            _write(out / "candidate.patch", _git_diff(work, starting_head))
        except Exception as exc:
            if not actor_failed:
                raise
            result["patch_error"] = str(exc)
        if actor_failed:
            return result
        grade = out / "grading"
        gitops.snapshot(work, grade, "WORKTREE")
        checked = _control(grade, task, out / "grader", cancel)
        if checked["kind"] == "acceptance":
            result.update(status="complete", passed=checked["passed"], grader=checked)
        elif checked["status"] in {"cancelled", "timeout"} or _network_output(checked):
            result.update(status=_run_status(checked), error=f"grader {checked['kind']} {checked['status']}")
        else:
            # The actor has already changed this checkout: compiler/import breaks
            # here are behavioral failures, unlike control qualification failures.
            result.update(status="complete", passed=False, grader=checked)
        return result
    except Exception as e:
        result.update(status="cancelled" if cancel and cancel.is_set() else "infrastructure_failure", error=str(e))
        return result
    finally:
        result.setdefault("wall_seconds", None)
        result["artifacts"] = _artifacts(out, agent)
