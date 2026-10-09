from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


DEFAULTS = {
    "shelley_binary": os.environ.get("SHELLEY_BENCH_SHELLEY_BINARY") or shutil.which("shelley") or "shelley",
    "pi_binary": os.environ.get("SHELLEY_BENCH_PI_BINARY") or shutil.which("pi") or "pi",
    "shelley_repo": os.environ.get("SHELLEY_BENCH_SHELLEY_REPO", ""),
    "baseline_ref": os.environ.get("SHELLEY_BENCH_BASELINE_REF", "main"),
    "build_command": "make build",
    "binary_path": "bin/shelley",
    "assistant_model": os.environ.get("SHELLEY_BENCH_ASSISTANT_MODEL", ""),
    "reasoning": "medium",
    "timeout_seconds": 1800,
}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
        if not self.get("settings", "global"):
            self.put("settings", {"id": "global", **DEFAULTS})

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=20)
            try:
                with db:
                    yield db
            finally:
                db.close()

    def get(self, kind, key):
        with self.connection() as db:
            row = db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def require(self, kind, key):
        value = self.get(kind, key)
        if value is None:
            raise KeyError(f"Unknown {kind}: {key}")
        return value

    def list(self, kind, **filters):
        with self.connection() as db:
            rows = db.execute("SELECT data FROM records WHERE kind=? ORDER BY rowid", (kind,)).fetchall()
        values = [json.loads(row[0]) for row in rows]
        return [v for v in values if all(v.get(k) == val for k, val in filters.items())]

    def put(self, kind, value):
        value = dict(value)
        value.setdefault("id", identifier(kind))
        value.setdefault("created_at", now())
        value["updated_at"] = now()
        with self.connection() as db:
            db.execute("INSERT INTO records(kind,id,data) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind, value["id"], json.dumps(value)))
        return value

    def update(self, kind, key, **changes):
        with self.lock:
            item = self.require(kind, key)
            item.update(changes)
            return self.put(kind, item)

    def settings(self):
        return {**DEFAULTS, **self.require("settings", "global")}

    def delete(self, kind, key):
        with self.connection() as db:
            db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    def recover(self):
        for kind, states in (("jobs", {"queued", "running"}), ("benchmarks", {"queued", "building", "running"}), ("runs", {"queued", "running"}), ("discussions", {"queued", "running"})):
            for item in self.list(kind):
                if item.get("status") in states:
                    self.update(kind, item["id"], status="interrupted", error="App restarted before this finished.", progress="")
        for bench in self.list("benchmarks"):
            if bench.get("progress") and bench.get("status") not in {"queued", "building", "running"}:
                self.update("benchmarks", bench["id"], progress="")
        for task in self.list("tasks"):
            if task.get("status") == "preparing":
                self.update("tasks", task["id"], status="failed", error="Preparation interrupted by restart.")
