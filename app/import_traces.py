"""Recover recorded microbenchmark events without changing acceptance or usage."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import native
from .store import Store


def recover(data, actor_root, benchmark_id):
    data, actor_root = Path(data), Path(actor_root)
    store = Store(data / "bench.sqlite3")
    runs = store.list("runs", benchmark_id=benchmark_id)
    jobs = [(run, run["source_record"], actor_root / run["source_record"]["id"] / "runtime/agent.db") for run in runs]
    recovered = []
    existing = 0
    for run, row, source in jobs:
        if run.get("trace_source") and run.get("events"):
            existing += 1
            continue
        if not source.is_file():
            continue
        snapshot = data / "import-trace-snapshots" / run["benchmark_id"] / f'{run["id"]}.sqlite3'
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original:
            if not original.execute("SELECT 1 FROM conversations WHERE conversation_id=?", (row["conversation_id"],)).fetchone():
                raise ValueError(f'Wrong actor database: {run["id"]}')
            with sqlite3.connect(snapshot) as copy:
                original.backup(copy)
        origin = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00")).timestamp()
        final, metrics, actors, events, _ = native._extract(snapshot, row["conversation_id"], origin)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "llm_calls", "tool_calls", "tool_errors"):
            if run.get(field) is not None and metrics[field] != run[field]:
                raise ValueError(f'Metrics mismatch: {run["id"]} {field}')
        if any(ev["kind"] == "tool" and ev["end"] is None for ev in events):
            raise ValueError(f'Missing tool result: {run["id"]}')
        provenance = {"path": str(source), "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                      "conversation_id": row["conversation_id"], "origin": row["started_at"],
                      "timing": "Recorded tool start/end timestamps; model reply timestamps may have one-second precision."}
        public_trace = {"source": provenance, "actors": actors, "events": events, "final_reply": final}
        name = f'traces/{run["id"]}.json'
        target = Path(run["_dir"]) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(public_trace, indent=2))
        artifacts = [a for a in run["artifacts"] if a["path"] != name]
        artifacts.append({"name": "Original recorded events (JSON)", "path": name})
        recovered.append((run, actors, events, provenance, artifacts))
    for run, actors, events, provenance, artifacts in recovered:
        store.update("runs", run["id"], actors=actors, events=events, trace_source=provenance,
                     artifacts=artifacts)
    return existing + len(recovered), len(jobs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True)
    parser.add_argument("--actor-root", required=True)
    parser.add_argument("--benchmark-id", required=True)
    args = parser.parse_args()
    found, total = recover(args.data, args.actor_root, args.benchmark_id)
    print(f"Recovered {found}/{total} original event traces; acceptance and usage unchanged.")


if __name__ == "__main__":
    main()
