"""Shelley Bench: a benchmark runner with a JSON API and a read-mostly UI."""
from __future__ import annotations

import argparse
import copy
import json
import queue
import shutil
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agents, assistant, cleanup, gitops, grading, native, runner, runtime, ui, versions
from .store import Store, identifier, now

REASONING = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
ACTIVE_BENCHMARK = {"queued", "building", "running"}
REPORT_FIELDS = ("hypothesis", "abstract", "setup", "conclusions", "followups")


def text(value, limit, name="field"):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > limit:
        raise ValueError(f"{name} is longer than {limit} characters")
    return value


def report_text(value):
    if not isinstance(value, dict) or set(value) - set(REPORT_FIELDS):
        raise ValueError("report must contain only hypothesis, abstract, setup, conclusions, followups")
    return {key: text(value[key], 12000, f"report.{key}") for key in value}


def revision(repo, ref):
    if ref == "WORKTREE":
        return "WORKTREE"
    if not ref or ref.startswith("-"):
        raise ValueError("Invalid Git revision")
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError(f"Cannot resolve {ref!r} in {repo}")
    return result.stdout.strip()


class Jobs:
    """Two sequential workers: task preparation and benchmark execution."""

    def __init__(self, app):
        self.app = app
        self.cancels = {}
        self.queues = {"prepare": queue.Queue(), "run": queue.Queue()}
        for name, q in self.queues.items():
            threading.Thread(target=self.work, args=(q,), daemon=True, name=f"bench-{name}").start()

    def submit(self, lane, kind, fn, **fields):
        job = self.app.store.put("jobs", {"id": identifier("job"), "kind": kind, "status": "queued", "log": [], **fields})
        cancel = threading.Event()
        self.cancels[job["id"]] = cancel
        self.queues[lane].put((job, cancel, fn))
        return job

    def cancel(self, job_id):
        cancel = self.cancels.get(job_id)
        if cancel:
            cancel.set()
        return bool(cancel)

    def work(self, q):
        while True:
            job, cancel, fn = q.get()
            store = self.app.store
            store.update("jobs", job["id"], status="running", started_at=now())
            def emit(event):
                if isinstance(event, dict):
                    item = store.require("jobs", job["id"])
                    log = (item.get("log") or [])[-199:] + [{"at": now(), **{k: v for k, v in event.items() if isinstance(v, (str, int, float))}}]
                    store.update("jobs", job["id"], log=log)
            try:
                fn(cancel, emit, self.app.data / "jobs" / job["id"])
                store.update("jobs", job["id"], status="cancelled" if cancel.is_set() else "complete", finished_at=now())
            except Exception as exc:
                store.update("jobs", job["id"], status="cancelled" if cancel.is_set() else "failed", error=str(exc), trace=traceback.format_exc()[-4000:], finished_at=now())
            finally:
                try:
                    removed = cleanup.finish_job(self.app.data, job["id"])
                    store.update("jobs", job["id"], cleanup_directories=len(removed))
                except Exception as exc:
                    store.update("jobs", job["id"], cleanup_error=str(exc))
                self.cancels.pop(job["id"], None)
                q.task_done()


class App:
    def __init__(self, data, recover=True):
        self.data = Path(data)
        self.store = Store(self.data / "bench.sqlite3")
        if recover:
            self.store.recover()
        self.lock = threading.RLock()
        try:
            self.models = native.available_models(self.store.settings()["shelley_binary"])
        except Exception:
            self.models = []
        self.jobs = Jobs(self)

    # ----- views ---------------------------------------------------------
    def state(self):
        s = self.store.settings()
        return {"settings": s, "models": self.models, "shelley": versions.describe(s["shelley_repo"], "WORKTREE"),
                "tasks": [self.task_view(t) for t in self.store.list("tasks")],
                "discussions_pending": sum(t["status"] in {"queued", "running"} for t in self.store.list("discussions")),
                "benchmarks": [self.benchmark_view(b) for b in self.store.list("benchmarks")]}

    def task_view(self, task):
        return {k: v for k, v in task.items() if not k.startswith("_")}

    def benchmark_view(self, bench, runs=False):
        view = {k: v for k, v in bench.items() if not k.startswith("_")}
        items = self.store.list("runs", benchmark_id=bench["id"])
        view["summary"] = self.summary(bench, items)
        if runs:
            view["runs"] = [self.run_view(r, brief=True) for r in items]
        return view

    def run_view(self, run, brief=False):
        view = {k: v for k, v in run.items() if not k.startswith("_")}
        if brief:
            view.pop("events", None)
            view["event_count"] = len(run.get("events") or [])
        return view

    def summary(self, bench, runs):
        from .report import current_runs
        runs, _ = current_runs(runs)
        out = {}
        for version in bench.get("versions") or []:
            mine = [r for r in runs if r.get("version") == version["label"]]
            done = [r for r in mine if r.get("status") == "complete"]
            out[version["label"]] = {
                "runs": len(mine), "complete": len(done), "passed": sum(1 for r in done if r.get("passed")),
                "failed": sum(1 for r in done if r.get("passed") is False),
                "errors": sum(1 for r in mine if r.get("status") in {"infrastructure_failure", "failed", "interrupted", "cancelled", "timeout"}),
                "wall_seconds": sum(float(r.get("wall_seconds") or 0) for r in done),
                "tokens": sum(sum(int(r.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")) for r in done),
                "llm_calls": sum(int(r.get("llm_calls") or 0) for r in done),
                "cost_usd": sum(float(r.get("cost_usd") or 0) for r in done),
            }
        return out

    # ----- settings --------------------------------------------------------
    def settings(self, body):
        allowed = {"shelley_binary", "pi_binary", "shelley_repo", "build_command", "binary_path", "assistant_model", "reasoning", "timeout_seconds"}
        if set(body) - allowed:
            raise ValueError("Unknown settings field")
        values = {**self.store.settings(), **body}
        values["timeout_seconds"] = int(values["timeout_seconds"])
        if values["reasoning"] not in REASONING:
            raise ValueError("Unknown reasoning level")
        saved = self.store.put("settings", values)
        if "shelley_binary" in body:
            self.models = native.available_models(values["shelley_binary"])
        return saved

    # ----- tasks -----------------------------------------------------------
    def repo(self):
        s = self.store.settings()
        return {"id": "shelley", "name": "Shelley", "path": s["shelley_repo"], "setup_command": ""}

    def create_task(self, body):
        s = self.store.settings()
        task = {"id": identifier("task"), "title": text(body.get("title"), 200, "title").strip(), "repo_id": "shelley",
                "prompt": text(body.get("prompt"), 20000, "prompt"), "blurb": text(body.get("blurb"), 3000, "blurb"),
                "tags": [str(t)[:40] for t in (body.get("tags") or [])[:10]],
                "setup_command": text(body.get("setup_command"), 2000, "setup_command"),
                "acceptance_command": text(body.get("acceptance_command"), 2000, "acceptance_command"),
                "test_patch": text(body.get("test_patch"), 400000, "test_patch"),
                "status": "proposed", "controls": {}}
        if not task["title"]:
            raise ValueError("title is required")
        task["reference_commit"] = revision(s["shelley_repo"], text(body.get("reference_commit"), 200, "reference_commit"))
        base = text(body.get("base_commit"), 200, "base_commit") or task["reference_commit"] + "^"
        task["base_commit"] = revision(s["shelley_repo"], base)
        if task["acceptance_command"] and task["test_patch"]:
            # Hand-written grader: keep it as-is; qualification still checks before/after.
            task["grading_contract"] = body.get("grading_contract")
            task["grading_probe"] = body.get("grading_probe")
        self.store.put("tasks", task)
        if body.get("prepare", True):
            self.prepare_task(task["id"])
        return self.task_view(self.store.require("tasks", task["id"]))

    def update_task(self, key, body):
        task = self.store.require("tasks", key)
        allowed = {"title", "prompt", "blurb", "tags", "setup_command", "acceptance_command", "test_patch", "base_commit", "reference_commit"}
        if set(body) - allowed:
            raise ValueError("Unknown task field")
        changes = {k: v for k, v in body.items()}
        material = {"prompt", "setup_command", "acceptance_command", "test_patch", "base_commit", "reference_commit"}
        if material & set(changes):
            if task.get("task_definition") and "prompt" not in changes:
                changes["prompt"] = task["task_definition"]
            for field in ("grading_contract", "grading_probe", "grading_review", "grading_qualification", "task_definition"):
                changes[field] = None
            changes.update(status="proposed", controls={}, error=None)
        return self.task_view(self.store.update("tasks", key, **changes))

    def prepare_task(self, key):
        t = self.store.require("tasks", key)
        if t["status"] == "preparing":
            raise ValueError("Already preparing")
        repo, settings = self.repo(), self.store.settings()
        self.store.update("tasks", key, status="preparing", error=None, grading_review=None, grading_qualification=None)
        def prepare(cancel, emit, out):
            out.mkdir(parents=True, exist_ok=True)
            task = copy.deepcopy(t)
            try:
                grading.validate_spec(task)
                has_spec = True
            except (ValueError, TypeError, KeyError):
                has_spec = False
            try:
                feedback = None
                for attempt in range(3):
                    if cancel.is_set():
                        raise RuntimeError("Preparation cancelled")
                    if not has_spec or feedback:
                        emit({"phase": "write", "progress": "Writing scoring tests from the reference change" if not feedback else f"Revising the grader (round {attempt + 1}) after rejection"})
                        try:
                            generated = assistant.prepare_spec(settings, task, repo, out / f"assistant-{attempt}", cancel, emit, feedback=feedback)
                        except ValueError as exc:
                            if attempt == 2 or "cannot" in str(exc).lower() and "Assistant" in str(exc):
                                raise
                            feedback = {"stage": "validation", "issues": [str(exc)], "task_brief": task.get("task_definition") or task.get("prompt")}
                            emit({"phase": "write", "progress": "Generated grader was invalid", "detail": str(exc)[:500]})
                            continue
                        task.update(generated)
                        self.store.update("tasks", key, **generated)
                    emit({"phase": "review", "progress": "Reviewing the grader for hidden assumptions"})
                    rejected = {k: task.get(k) for k in ("prompt", "grading_contract", "acceptance_command", "test_patch", "grading_probe")}
                    try:
                        review = assistant.review_spec(settings, task, repo, out / f"review-{attempt}", cancel, emit)
                        task["grading_review"] = review
                        self.store.update("tasks", key, grading_review=review)
                        grading.require_review(task)
                    except ValueError as exc:
                        if attempt == 2:
                            raise
                        feedback = {"stage": "review", "issues": [str(exc)], "rejected_grader": rejected}
                        emit({"phase": "review", "progress": "Grader rejected; sending issues back to the writer", "detail": str(exc)[:500]})
                        continue
                    result = runner.prepare_task(task, repo, out / f"controls-{attempt}", cancel, emit)
                    self.store.update("tasks", key, **result, error=result.get("error"))
                    if result.get("status") == "ready":
                        break
                    error = result.get("error") or "Controls did not qualify this task"
                    if attempt == 2 or cancel.is_set():
                        raise RuntimeError(error)
                    evidence = {}
                    for name, control in (result.get("controls") or {}).items():
                        evidence[name] = {k: control.get(k) for k in ("kind", "status", "passed")}
                        evidence[name]["output_tail"] = ((control.get("stdout") or "") + "\n" + (control.get("stderr") or ""))[-3000:]
                    feedback = {"stage": "controls", "issues": [error], "controls": evidence, "rejected_grader": rejected,
                                "hint": "base must fail a marked assertion, the reference and the equivalent probe must pass the SAME tests. If the reference fails a criterion, that criterion is not true of the real change: drop or correct it rather than weakening unrelated checks. If the probe failed to apply, regenerate it as a valid unified diff against the reference files."}
                    emit({"phase": "controls", "progress": "Controls failed; sending evidence back to the writer", "detail": error[:500]})
            except Exception as exc:
                self.store.update("tasks", key, status="failed", error=str(exc))
                raise
        job = self.jobs.submit("prepare", "prepare_task", prepare, task_id=key)
        self.store.update("tasks", key, job_id=job["id"])
        return job

    def delete_task(self, key):
        self.store.require("tasks", key)
        if any(key in (b.get("task_ids") or []) and b.get("status") in ACTIVE_BENCHMARK for b in self.store.list("benchmarks")):
            raise ValueError("Task is used by an active benchmark")
        self.store.delete("tasks", key)

    # ----- benchmarks -------------------------------------------------------
    def resolve_tasks(self, task_ids):
        if not isinstance(task_ids, list) or not task_ids:
            raise ValueError("task_ids must be a non-empty list")
        tasks = [self.store.require("tasks", tid) for tid in task_ids]
        not_ready = [t["title"] for t in tasks if t["status"] != "ready"]
        if not_ready:
            raise ValueError("Tasks are not ready: " + ", ".join(not_ready))
        return tasks

    def resolve_versions(self, raw_versions, seen=()):
        s = self.store.settings()
        if not isinstance(raw_versions, list) or not raw_versions or len(raw_versions) > 6:
            raise ValueError("versions must list 1-6 entries of {label, ref}")
        resolved, seen = [], set(seen)
        for item in raw_versions:
            if not isinstance(item, dict):
                raise ValueError("Each version must be an object")
            if item.get("reuse_run_id"):
                source = self.store.require("runs", item["reuse_run_id"])
                source_bench = self.store.require("benchmarks", source["benchmark_id"])
                original = next(v for v in source_bench["versions"] if v["label"] == source["version"])
                label = text(item.get("label") or source["version"], 60, "label").strip()
                if label in seen:
                    raise ValueError(f"Duplicate version label {label!r}")
                seen.add(label)
                resolved.append({**copy.deepcopy(original), "label": label, "reuse_run_id": source["id"]})
                continue
            if item.get("agent") == "pi":
                version = agents.pi_version(item, s.get("pi_binary") or "pi")
                label = text(item.get("label") or version["ref"], 60, "label").strip()
                if label in seen:
                    raise ValueError(f"Duplicate version label {label!r}")
                seen.add(label)
                resolved.append({**version, "label": label})
                continue
            if item.get("agent", "shelley") != "shelley":
                raise ValueError("Unknown coding-agent adapter")
            ref = text(item.get("ref"), 200, "ref").strip() or "WORKTREE"
            sha = revision(s["shelley_repo"], ref)
            label = text(item.get("label"), 60, "label").strip() or (ref if ref == "WORKTREE" else sha[:10])
            if label in seen:
                raise ValueError(f"Duplicate version label {label!r}")
            seen.add(label)
            profile = runtime.freeze(item.get("runtime_profile"))
            resolved.append({"label": label, "ref": ref, "sha": sha, "describe": versions.describe(s["shelley_repo"], ref), "runtime_profile": profile})
        return resolved

    def resolve_models(self, models, *, external=False):
        if not isinstance(models, list) or not models:
            raise ValueError("models must be a non-empty list")
        known = {m["id"] for m in self.models}
        unknown = [m for m in models if m not in known]
        if unknown and known and not external:
            raise ValueError("Unknown models: " + ", ".join(map(str, unknown)))
        return models

    def queue_runs(self, bench, tasks, models, attempts, versions, retry=False):
        existing = {(r["task_id"], r["model"], r["attempt"], r["version"]): r for r in self.store.list("runs", benchmark_id=bench["id"])}
        ids = []
        if retry:
            for combo, run in existing.items():
                if run.get("status") in {"interrupted", "cancelled", "infrastructure_failure"}:
                    self.store.update("runs", run["id"], status="queued", error=None, passed=None)
                    ids.append(run["id"])
        for task in tasks:
            for model in models:
                for attempt in attempts:
                    for version in versions:
                        if (task["id"], model, attempt, version["label"]) in existing:
                            continue
                        if version.get("reuse_run_id"):
                            raise ValueError("A reused version cannot queue new coding runs")
                        existing[(task["id"], model, attempt, version["label"])] = None
                        run = self.store.put("runs", {"id": identifier("run"), "benchmark_id": bench["id"], "task_id": task["id"], "task_title": task["title"],
                                                      "version": version["label"], "model": model, "attempt": attempt, "status": "queued", "passed": None})
                        ids.append(run["id"])
        if not ids:
            raise ValueError("Nothing new to run")
        job = self.jobs.submit("run", "benchmark", lambda cancel, emit, out: self.execute(bench["id"], ids, cancel, emit, out), benchmark_id=bench["id"])
        self.store.update("benchmarks", bench["id"], job_id=job["id"], status="queued", error=None, progress="Waiting for the runner")
        return ids

    def resume_benchmark(self, key):
        """Re-queue runs that never finished (interrupted, cancelled, infrastructure failures)."""
        bench = self.store.require("benchmarks", key)
        if bench.get("import_source"):
            raise ValueError("Historical imports cannot be rerun; create a new benchmark")
        if bench["status"] in ACTIVE_BENCHMARK:
            raise ValueError("Benchmark is already running")
        ids = [r["id"] for r in self.store.list("runs", benchmark_id=key) if r["status"] in {"interrupted", "cancelled", "queued", "infrastructure_failure", "timeout"}]
        if not ids:
            raise ValueError("Nothing to resume")
        for rid in ids:
            self.store.update("runs", rid, status="queued", error=None, passed=None, events=[], actors=[], wall_seconds=None)
        job = self.jobs.submit("run", "benchmark", lambda cancel, emit, out: self.execute(key, ids, cancel, emit, out), benchmark_id=key)
        self.store.update("benchmarks", key, job_id=job["id"], status="queued", error=None, progress="Waiting for the runner")
        return self.benchmark_view(self.store.require("benchmarks", key), runs=True)

    def extend_benchmark(self, key, body):
        """Add runs for extra models, versions, tasks, or attempts to an existing benchmark."""
        bench = self.store.require("benchmarks", key)
        if bench.get("import_source"):
            raise ValueError("Historical imports cannot be extended; create a new benchmark")
        if bench["status"] in ACTIVE_BENCHMARK:
            raise ValueError("Wait for the current runs to finish before adding more")
        tasks = self.resolve_tasks(body["task_ids"]) if body.get("task_ids") else bench["frozen_tasks"]
        versions = self.resolve_versions(body["versions"], {v["label"] for v in bench["versions"]}) if body.get("versions") else bench["versions"]
        if body.get("version_labels"):
            selected = body["version_labels"]
            if not isinstance(selected, list) or set(selected) - {v["label"] for v in versions}:
                raise ValueError("version_labels must name existing benchmark versions")
            versions = [v for v in versions if v["label"] in selected]
        models = self.resolve_models(body["models"], external=all(v.get("agent") == "pi" or v.get("reuse_run_id") for v in versions)) if body.get("models") else bench["models"]
        attempts = int(body.get("attempts") or bench["attempts"])
        if not 1 <= attempts <= 20:
            raise ValueError("attempts must be 1-20")
        frozen = {t["id"]: t for t in bench["frozen_tasks"]}
        for t in tasks:
            frozen.setdefault(t["id"], copy.deepcopy(t))
        self.store.update("benchmarks", key, frozen_tasks=list(frozen.values()), task_ids=list(frozen),
                          versions=bench["versions"] + [v for v in versions if v["label"] not in {b["label"] for b in bench["versions"]}],
                          models=bench["models"] + [m for m in models if m not in bench["models"]], attempts=max(attempts, bench["attempts"]))
        bench = self.store.require("benchmarks", key)
        self.queue_runs(bench, [frozen[t["id"]] for t in tasks], models, range(1, attempts + 1), versions, retry=bool(body.get("retry")))
        return self.benchmark_view(bench, runs=True)

    def create_benchmark(self, body):
        s = self.store.settings()
        tasks = self.resolve_tasks(body.get("task_ids") or [])
        resolved = self.resolve_versions(body.get("versions") or [])
        models = self.resolve_models(body.get("models") or [], external=all(v.get("agent") == "pi" or v.get("reuse_run_id") for v in resolved))
        attempts = int(body.get("attempts") or 1)
        if not 1 <= attempts <= 10:
            raise ValueError("attempts must be 1-10")
        reasoning = body.get("reasoning") or s["reasoning"]
        if reasoning not in REASONING:
            raise ValueError("Unknown reasoning level")
        bench = {"id": identifier("bench"), "title": text(body.get("title"), 200, "title").strip() or tasks[0]["title"],
                 "group": text(body.get("group"), 100, "group").strip(), "notes": text(body.get("notes"), 20000, "notes"),
                 "tags": [str(t)[:40] for t in (body.get("tags") or [])[:10]],
                 "agent_note": text(body.get("agent_note"), 2000, "agent_note").strip(),
                 "report": report_text(body.get("report", {})),
                 "task_ids": [t["id"] for t in tasks], "frozen_tasks": [copy.deepcopy(t) for t in tasks],
                 "versions": resolved, "models": models, "attempts": attempts, "reasoning": reasoning,
                 "status": "queued", "error": None, "progress": "Waiting for the runner"}
        reused = self.reused_runs(bench)
        self.store.put("benchmarks", bench)
        for run in reused:
            self.store.put("runs", run)
        self.queue_runs(bench, tasks, models, range(1, attempts + 1), resolved)
        return self.benchmark_view(self.store.require("benchmarks", bench["id"]), runs=True)

    def reused_runs(self, bench):
        reused = []
        for version in bench["versions"]:
            if not version.get("reuse_run_id"):
                continue
            source = self.store.require("runs", version["reuse_run_id"])
            source_bench = self.store.require("benchmarks", source["benchmark_id"])
            if source.get("status") != "complete" or not isinstance(source.get("passed"), bool):
                raise ValueError("Only completed graded runs can be reused")
            if bench["models"] != [source["model"]] or bench["task_ids"] != [source["task_id"]] or bench["attempts"] != 1:
                raise ValueError("Reused run must match the task/model and a single comparison attempt")
            if source_bench.get("reasoning") != bench["reasoning"] or source_bench.get("agent_note", "") != bench.get("agent_note", ""):
                raise ValueError("Reused run reasoning and actor note must match")
            old_task = next(t for t in source_bench["frozen_tasks"] if t["id"] == source["task_id"])
            new_task = next(t for t in bench["frozen_tasks"] if t["id"] == source["task_id"])
            fields = ("base_commit", "prompt", "setup_command", "acceptance_command", "test_patch", "grading_contract", "grading_qualification")
            if any(old_task.get(key) != new_task.get(key) for key in fields):
                raise ValueError("Reused run must match the frozen task and grader")
            reused.append({**copy.deepcopy(source), "id": identifier("run"), "benchmark_id": bench["id"],
                           "version": version["label"], "attempt": 1, "_source_run_id": source["id"],
                           "recorded_attempt": source.get("attempt")})
        return reused

    def build(self, version, config, out, cancel):
        """Build one Shelley version; commits are cached by SHA, the worktree never is."""
        if version.get("agent") == "pi":
            return agents.prepare_pi(version, out)
        if version["sha"] != "WORKTREE":
            cache = self.data / "builds" / version["sha"]
            manifest = cache / "build.json"
            if manifest.exists():
                built = json.loads(manifest.read_text())
                if Path(built["path"]).is_file():
                    return cleanup.compact_build(cache, built)
            source = cache / "source"
            if source.exists():
                shutil.rmtree(source)
            gitops.snapshot(config["shelley_repo"], source, version["sha"])
            built = runner._build(source, config, cache, cancel)
            return cleanup.compact_build(cache, built)
        source = out / "worktree-source"
        gitops.snapshot(config["shelley_repo"], source, "WORKTREE")
        return runner._build(source, config, out, cancel)

    def execute(self, key, run_ids, cancel, emit, out):
        store = self.store
        bench = store.require("benchmarks", key)
        config = store.settings()
        repo = self.repo()
        tasks = {t["id"]: dict(t) for t in bench["frozen_tasks"]}
        if bench.get("agent_note"):
            for t in tasks.values():
                t["prompt"] = t["prompt"].rstrip() + "\n\n" + bench["agent_note"]
        try:
            store.update("benchmarks", key, status="building", started_at=now(), progress="Building Shelley versions")
            runs = [self.store.require("runs", rid) for rid in run_ids]
            needed = {r["version"] for r in runs}
            binaries = {}
            for version in [v for v in bench["versions"] if v["label"] in needed]:
                emit({"phase": "build", "progress": f"Building {version['label']}"})
                if cancel.is_set():
                    raise RuntimeError("Cancelled")
                binaries[version["label"]] = {**self.build(version, config, out / "build" / version["label"], cancel), "runtime_profile": version.get("runtime_profile")}
            store.update("benchmarks", key, status="running", binaries={**(bench.get("binaries") or {}), **{k: v.get("provenance", {}).get("binary_sha256") for k, v in binaries.items()}})
            for index, run in enumerate(runs):
                if cancel.is_set():
                    raise RuntimeError("Cancelled")
                label = f"{run['task_title']} · {run['model']} · {run['version']} · attempt {run['attempt']}"
                store.update("benchmarks", key, progress=f"{index + 1}/{len(runs)}: {label}")
                store.update("runs", run["id"], status="running", started_at=now(), _dir=str(out / "runs" / run["id"]))
                emit({"phase": "run", "progress": label})
                last = [0.0]
                def live(event, run_id=run["id"]):
                    if not isinstance(event, dict) or event.get("kind") != "progress":
                        return
                    if time.monotonic() - last[0] < 3:
                        return
                    last[0] = time.monotonic()
                    store.update("runs", run_id, wall_seconds=event.get("wall_seconds"), events=event.get("events") or [],
                                 actors=event.get("actors") or [], **{k: v for k, v in (event.get("metrics") or {}).items()})
                result = runner.run_trial(tasks[run["task_id"]], repo, binaries[run["version"]], run["model"], bench["reasoning"],
                                          out / "runs" / run["id"], int(config["timeout_seconds"]), cancel, live)
                result.pop("task_id", None); result.pop("task_title", None); result.pop("model", None)
                store.update("runs", run["id"], **result, finished_at=now(), _dir=str(out / "runs" / run["id"]))
            store.update("benchmarks", key, status="complete", progress="Writing findings", finished_at=now())
            self.write_findings(key, cancel, emit, out)
            store.update("benchmarks", key, progress="")
        except Exception as exc:
            status = "cancelled" if cancel.is_set() else "failed"
            for run in [store.require("runs", rid) for rid in run_ids]:
                if run["status"] in {"queued", "running"}:
                    store.update("runs", run["id"], status=status, error=str(exc))
            store.update("benchmarks", key, status=status, error=str(exc), progress="", finished_at=now())
            raise

    def write_findings(self, key, cancel, emit, out):
        bench = self.store.require("benchmarks", key)
        runs = self.store.list("runs", benchmark_id=key)
        bench["summary"] = self.summary(bench, runs)
        try:
            text = assistant.findings(self.store.settings(), bench, runs, out / "findings", cancel, emit)
            self.store.update("benchmarks", key, findings=text, findings_error=None, findings_at=now())
        except Exception as exc:
            self.store.update("benchmarks", key, findings_error=str(exc))

    def regenerate_findings(self, key):
        bench = self.store.require("benchmarks", key)
        if bench["status"] in ACTIVE_BENCHMARK:
            raise ValueError("Wait for the benchmark to finish")
        self.store.update("benchmarks", key, findings_error=None, progress="Writing findings")
        def work(cancel, emit, out):
            try:
                self.write_findings(key, cancel, emit, out)
            finally:
                self.store.update("benchmarks", key, progress="")
        return self.jobs.submit("prepare", "findings", work, benchmark_id=key)

    def ask(self, key, body):
        question = text(body.get("message"), 4000, "message").strip()
        request_id = text(body.get("request_id"), 100, "request_id")
        if not question:
            raise ValueError("Enter a follow-up question")
        with self.lock:
            self.store.require("benchmarks", key)
            turns = self.store.list("discussions", benchmark_id=key)
            if request_id:
                previous = next((t for t in turns if t.get("request_id") == request_id), None)
                if previous:
                    if previous["question"] != question:
                        raise ValueError("This request ID was already used for another question")
                    return previous
            if any(t["status"] in {"queued", "running"} for t in turns):
                raise ValueError("Wait for the current answer before sending another question")
            settings = self.store.settings()
            turn = self.store.put("discussions", {
                "id": identifier("chat"), "benchmark_id": key, "question": question,
                "status": "queued", "request_id": request_id, "model": settings["assistant_model"]})
            history = [{"question": t["question"], "reply": t.get("reply")} for t in turns if t["status"] == "complete"]
            def work(cancel, emit, out):
                self.store.update("discussions", turn["id"], status="running")
                try:
                    bench = self.store.require("benchmarks", key)
                    runs = self.store.list("runs", benchmark_id=key)
                    reply = assistant.discuss(settings, bench, runs, history, question, out, cancel, emit)
                    self.store.update("discussions", turn["id"], status="complete", reply=reply)
                except Exception as exc:
                    self.store.update("discussions", turn["id"], status="failed", error=str(exc))
                    raise
            job = self.jobs.submit("prepare", "discussion", work, benchmark_id=key)
            return self.store.update("discussions", turn["id"], job_id=job["id"])

    def cancel_benchmark(self, key):
        bench = self.store.require("benchmarks", key)
        if bench["status"] not in ACTIVE_BENCHMARK:
            raise ValueError("Benchmark is not running")
        self.jobs.cancel(bench.get("job_id"))
        return {"ok": True}

    def delete_benchmark(self, key):
        bench = self.store.require("benchmarks", key)
        if bench["status"] in ACTIVE_BENCHMARK:
            raise ValueError("Cancel the benchmark first")
        if any(t["status"] in {"queued", "running"} for t in self.store.list("discussions", benchmark_id=key)):
            raise ValueError("Wait for the discussion to finish before deleting the benchmark")
        for run in self.store.list("runs", benchmark_id=key):
            self.store.delete("runs", run["id"])
        for turn in self.store.list("discussions", benchmark_id=key):
            self.store.delete("discussions", turn["id"])
        self.store.delete("benchmarks", key)

    def update_benchmark(self, key, body):
        allowed = {"title", "group", "notes", "tags", "report", "featured_order"}
        if set(body) - allowed:
            raise ValueError("Unknown benchmark field")
        order = body.get("featured_order")
        if order is not None and (type(order) is not int or not 0 <= order <= 1000):
            raise ValueError("featured_order must be null or an integer from 0 to 1000")
        if "report" in body:
            bench = self.store.require("benchmarks", key)
            body = {**body, "report": {**(bench.get("report") or {}), **report_text(body["report"])}}
        return self.benchmark_view(self.store.update("benchmarks", key, **body))

    def record_run(self, body):
        """Record a run executed outside this app (e.g. driven directly by Shelley)."""
        bench = self.store.require("benchmarks", body.get("benchmark_id"))
        version = body.get("version")
        if version not in {v["label"] for v in bench["versions"]}:
            raise ValueError("version must match a benchmark version label")
        run = {"id": identifier("run"), "benchmark_id": bench["id"], "external": True, "version": version,
               "task_id": text(body.get("task_id"), 100), "task_title": text(body.get("task_title"), 200) or body.get("task_id"),
               "model": text(body.get("model"), 100), "attempt": int(body.get("attempt") or 1),
               "status": body.get("status") or "complete", "passed": body.get("passed"), "error": text(body.get("error"), 4000),
               "wall_seconds": body.get("wall_seconds"), "events": body.get("events") or [], "actors": body.get("actors") or [],
               "notes": text(body.get("notes"), 20000), "finished_at": now()}
        for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "llm_calls", "tool_calls", "tool_errors", "subagents", "cost_usd"):
            run[k] = body.get(k)
        return self.run_view(self.store.put("runs", run))


class Handler(BaseHTTPRequestHandler):
    server_version = "ShelleyBench/2"

    def log_message(self, fmt, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def json(self, payload, status=200):
        raw = json.dumps(payload, indent=1, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 50_000_000:
            raise ValueError("Request too large")
        raw = self.rfile.read(length) if length else b""
        kind = self.headers.get("Content-Type", "")
        if kind.startswith("application/x-www-form-urlencoded"):
            return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(raw.decode()).items()}
        return json.loads(raw or b"{}")

    def same_origin(self):
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        path = url.path
        try:
            if path == "/agents.txt":
                return self.static("/static/agents.txt")
            if path == "/robots.txt":
                return self.static("/static/robots.txt")
            if path.startswith("/static/"):
                return self.static(path)
            if path == "/healthz":
                return self.json({"ok": True})
            if path == "/api/state":
                return self.json(self.app.state())
            if path == "/api/tasks":
                return self.json([self.app.task_view(t) for t in self.app.store.list("tasks")])
            if path.startswith("/api/tasks/"):
                return self.json(self.app.task_view(self.app.store.require("tasks", path.split("/")[3])))
            if path == "/api/benchmarks":
                return self.json([self.app.benchmark_view(b) for b in self.app.store.list("benchmarks")])
            if path.startswith("/api/benchmarks/") and path.endswith("/chat"):
                key = path.split("/")[3]
                self.app.store.require("benchmarks", key)
                return self.json(self.app.store.list("discussions", benchmark_id=key))
            if path.startswith("/api/benchmarks/"):
                return self.json(self.app.benchmark_view(self.app.store.require("benchmarks", path.split("/")[3]), runs=True))
            if path.startswith("/api/runs/"):
                return self.json(self.app.run_view(self.app.store.require("runs", path.split("/")[3])))
            if path.startswith("/api/jobs/"):
                return self.json(self.app.store.require("jobs", path.split("/")[3]))
            if path.startswith("/artifacts/"):
                return self.artifact(path)
            return ui.respond(self, path, params)
        except KeyError as exc:
            self.json({"error": str(exc)}, 404)
        except ValueError as exc:
            self.json({"error": str(exc)}, 400)
        except Exception:
            self.json({"error": traceback.format_exc()[-3000:]}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path
        if not self.same_origin():
            return self.json({"error": "Cross-origin request rejected"}, 403)
        try:
            body = self.body()
            app = self.app
            parts = path.split("/")
            if path == "/api/settings":
                return self.json(app.settings(body))
            if path == "/api/tasks":
                return self.json(app.create_task(body), 201)
            if path.startswith("/api/tasks/") and len(parts) == 5 and parts[4] == "prepare":
                return self.json(app.prepare_task(parts[3]), 202)
            if path.startswith("/api/tasks/") and len(parts) == 5 and parts[4] == "delete":
                app.delete_task(parts[3]); return self.json({"ok": True})
            if path.startswith("/api/tasks/") and len(parts) == 4:
                return self.json(app.update_task(parts[3], body))
            if path == "/api/benchmarks":
                return self.json(app.create_benchmark(body), 201)
            if path.startswith("/api/benchmarks/") and len(parts) == 5 and parts[4] == "chat":
                return self.json(app.ask(parts[3], body), 202)
            if path.startswith("/api/benchmarks/") and len(parts) == 5 and parts[4] == "runs":
                return self.json(app.extend_benchmark(parts[3], body), 201)
            if path.startswith("/api/benchmarks/") and len(parts) == 5 and parts[4] == "resume":
                return self.json(app.resume_benchmark(parts[3]), 202)
            if path.startswith("/api/benchmarks/") and len(parts) == 5 and parts[4] == "findings":
                return self.json(app.regenerate_findings(parts[3]), 202)
            if path.startswith("/api/benchmarks/") and len(parts) == 5 and parts[4] == "cancel":
                return self.json(app.cancel_benchmark(parts[3]))
            if path.startswith("/api/benchmarks/") and len(parts) == 5 and parts[4] == "delete":
                app.delete_benchmark(parts[3]); return self.json({"ok": True})
            if path.startswith("/api/benchmarks/") and len(parts) == 4:
                return self.json(app.update_benchmark(parts[3], body))
            if path == "/api/runs":
                return self.json(app.record_run(body), 201)
            if path.startswith("/ui/"):
                return ui.action(self, path, body)
            self.json({"error": "Not found"}, 404)
        except KeyError as exc:
            self.json({"error": str(exc)}, 404)
        except (ValueError, TypeError) as exc:
            self.json({"error": str(exc)}, 400)

    def do_DELETE(self):
        parts = urlparse(self.path).path.split("/")
        try:
            if len(parts) == 4 and parts[2] == "tasks":
                self.app.delete_task(parts[3]); return self.json({"ok": True})
            if len(parts) == 4 and parts[2] == "benchmarks":
                self.app.delete_benchmark(parts[3]); return self.json({"ok": True})
            self.json({"error": "Not found"}, 404)
        except KeyError as exc:
            self.json({"error": str(exc)}, 404)
        except ValueError as exc:
            self.json({"error": str(exc)}, 400)

    def static(self, path):
        root = (Path(__file__).parent / "static").resolve()
        target = (root / path[len("/static/"):]).resolve()
        if root not in target.parents or not target.is_file():
            return self.json({"error": "Not found"}, 404)
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/css" if target.suffix == ".css" else "text/javascript" if target.suffix == ".js" else "text/plain; charset=utf-8" if target.suffix == ".txt" else "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def artifact(self, path):
        parts = path.split("/")
        run = self.app.store.require("runs", parts[2])
        root = Path(run.get("_dir") or "").resolve()
        target = (root / "/".join(parts[3:])).resolve()
        if not run.get("_dir") or root not in target.parents or not target.is_file():
            return self.json({"error": "Not found"}, 404)
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8138)
    parser.add_argument("--data", default=str(Path.home() / ".local/share/shelley-bench"))
    args = parser.parse_args()
    app = App(args.data)
    server = Server((args.host, args.port), app)
    print(f"Shelley Bench on http://localhost:{args.port}  data={args.data}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
