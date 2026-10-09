"""Import frozen comparison records without invoking the benchmark runner."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .store import Store, now





def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load_source(path, expected=None):
    raw = Path(path).read_bytes()
    sha = digest(raw)
    if expected and sha != expected:
        raise ValueError(f"Source hash mismatch: {path}")
    return json.loads(raw), raw, sha


def convert_row(bench, row, source_name, source_sha):
    key = row.get("id") or row.get("run") or "|".join(
        str(row.get(k, "")) for k in ("task", "model", "variant", "attempt", "repeat"))
    run_id = "run-import-" + digest(f'{bench["id"]}:{source_name}:{key}'.encode())[:20]
    usage = row
    if bench["usage_scope"] == "editing_agent":
        usage = row["model_usage_breakdown"]["editing_agent"]
    passed = row.get("passed")
    if not isinstance(passed, bool):
        raise ValueError(f"Missing acceptance result: {key}")
    metrics = {k: row[k] for k in (
        "content_correct", "patch_only_protocol", "one_shot_success", "first_patch_accepted",
        "patch_calls", "rejected_patch_calls", "additional_patch_calls", "edit_calls",
        "failed_edit_calls", "manual_bash_mutation_calls", "audited_rejection_categories",
        "rejection_categories", "valid") if k in row}
    return {
        "id": run_id, "benchmark_id": bench["id"], "external": True,
        "version": bench["variant_labels"][row["variant"]],
        "task_id": row["task"], "task_title": row["task"].replace("-", " ").title(),
        "model": row["model"], "attempt": row.get("repeat") or row.get("attempt") or 1,
        "status": "complete", "passed": passed, "wall_seconds": row.get("wall_seconds"),
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cached_input_tokens"),
        "cache_write_tokens": usage.get("cache_write_tokens"),
        "llm_calls": usage.get("model_calls") if bench["usage_scope"] == "editing_agent" else usage.get("llm_calls"),
        "tool_calls": row.get("patch_calls") if row.get("patch_only_protocol") is True else None,
        "tool_errors": row.get("rejected_patch_calls") if row.get("patch_only_protocol") is True else None,
        "subagents": None, "cost_usd": None, "events": [], "actors": [],
        "started_at": row.get("started_at"), "editing_metrics": metrics,
        "import_source": {"path": source_name, "sha256": source_sha, "row_key": key,
                          "usage_scope": bench["usage_scope"]},
        "source_record": row,
        "artifacts": [{"name": "Original row (JSON)", "path": f"rows/{run_id}.json"}],
    }


def prepare(archive, history_json, manifest):
    """Read and verify all sources before writing any records."""
    archive = Path(archive)
    specs = json.loads(Path(manifest).read_text())["studies"]

    prepared = []
    for spec in specs:
        spec = dict(spec)
        files = {}
        sources = []
        if spec.get("history_export"):
            source, raw, sha = load_source(history_json, spec["sha256"])
            files["history-export.json"] = raw
            sources.append({"path": str(history_json), "sha256": sha, "artifact": "history-export.json"})
            for item in source["sources"]:
                _, original, original_sha = load_source(archive / item["path"], item["sha256"])
                name = item["path"].replace("/", "__")
                files[name] = original
                sources.append({**item, "artifact": name, "sha256": original_sha})
            rows = source["rows"]
        else:
            source, raw, sha = load_source(archive / spec["source"], spec["sha256"])
            files["comparison.json"] = raw
            sources.append({"path": spec["source"], "sha256": sha, "artifact": "comparison.json"})
            rows = source["rows"]
            for name in ("REPORT.md", "AUDIT.md", "audit.json", "results.editing-only.csv"):
                path = (archive / spec["source"]).parent / name
                if path.is_file():
                    files[name] = path.read_bytes()
            if spec.get("config"):
                _, config_raw, config_sha = load_config(archive.parent / spec["config"], spec["config_sha256"])
                files["experiment.toml"] = config_raw
                sources.append({"path": spec["config"], "sha256": config_sha, "artifact": "experiment.toml"})
        if len(rows) != spec["expected_rows"]:
            raise ValueError(f'Unexpected row count: {spec["id"]}')
        bench = {
            "id": spec["id"], "title": spec["title"], "group": "Editing tools · historical imports",
            "status": "complete", "external": True, "study_type": spec["study_type"],
            "usage_scope": spec["usage_scope"], "measurement_note": spec["measurement_note"],
            "variant_labels": spec["variant_labels"], "report": spec["report"],
            "models": list(dict.fromkeys(row["model"] for row in rows)),
            "versions": [{"label": label, "sha": "", "ref": "historical import"}
                         for label in spec["variant_labels"].values()],
            "attempts": max(row.get("repeat") or row.get("attempt") or 1 for row in rows),
            "task_ids": [], "frozen_tasks": [], "notes": spec.get("notes", ""),
            "reasoning": spec.get("reasoning"), "progress": "", "error": None,
            "import_source": {"sources": sources, "definitions": source.get("definitions", {}),
                              "audit_notes": source.get("audit_notes", []),
                              "expected_rows": len(rows)},
            "related_studies": [{"id": s["id"], "title": s["title"]} for s in specs if s["id"] != spec["id"]],
        }
        runs = [convert_row(bench, row, spec.get("source") or "history-export.json", sha) for row in rows]
        if len({run["id"] for run in runs}) != len(runs):
            raise ValueError(f'Duplicate source rows: {bench["id"]}')
        if spec["usage_scope"] == "editing_agent":
            for run in runs:
                if run["source_record"].get("infra_failure") or not run["source_record"].get("patch_only_protocol"):
                    raise ValueError("Microbenchmark contains an infrastructure or protocol failure")
        files["import-manifest.json"] = json.dumps(spec, indent=2).encode()
        for row, run in zip(rows, runs):
            files[f'rows/{run["id"]}.json'] = json.dumps(run["source_record"], indent=2).encode()
            trial = archive / row["run"] if row.get("run") else (
                (archive / spec["source"]).parent / row["id"] if row.get("id") else None)
            evaluation = trial / "evaluation.json" if trial else None
            if evaluation and evaluation.is_file():
                name = f'rows/{run["id"]}-evaluation.json'
                files[name] = evaluation.read_bytes()
                run["artifacts"].append({"name": "Original evaluation (JSON)", "path": name})
        prepared.append((bench, runs, files))
    return prepared


def load_config(path, expected):
    raw = Path(path).read_bytes()
    if digest(raw) != expected:
        raise ValueError(f"Configuration hash mismatch: {path}")
    return None, raw, digest(raw)


def persist(store, data, studies):
    """Append atomically; a repeated identical import never updates existing runs."""
    stamp = now()
    records = []
    for bench, runs, files in studies:
        root = Path(data).resolve() / "imports" / bench["id"]
        for run in runs:
            run = {**run, "_dir": str(root), "created_at": stamp, "updated_at": stamp}
            records.append(("runs", run))
        bench = {**bench, "created_at": stamp, "updated_at": stamp}
        records.append(("benchmarks", bench))
    with store.connection() as db:
        for kind, record in records:
            found = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record["id"])).fetchone()
            if found:
                previous = json.loads(found[0])
                if previous.get("import_source") != record["import_source"]:
                    raise ValueError(f'Conflicting import: {record["id"]}')
                if kind == "runs" and previous.get("source_record") != record["source_record"]:
                    raise ValueError(f'Conflicting source row: {record["id"]}')
        for bench, _, files in studies:
            root = Path(data).resolve() / "imports" / bench["id"]
            for name, raw in files.items():
                target = root / name
                if target.exists() and target.read_bytes() != raw:
                    raise ValueError(f"Conflicting artifact: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_bytes(raw)
        for kind, record in records:
            db.execute("INSERT OR IGNORE INTO records(kind,id,data) VALUES(?,?,?)",
                       (kind, record["id"], json.dumps(record)))
    return [bench["id"] for bench, _, _ in studies]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--history-json", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    studies = prepare(args.archive, args.history_json, args.manifest)
    if not args.dry_run:
        persist(Store(Path(args.data) / "bench.sqlite3"), args.data, studies)
    print(json.dumps([{"id": b["id"], "title": b["title"], "runs": len(r)} for b, r, _ in studies], indent=2))


if __name__ == "__main__":
    main()
