#!/bin/sh
# Restart only when the local service reports no active benchmark work.
set -eu

service_name=${1:-shelley-bench}
port=${SHELLEY_BENCH_PORT:-8138}
busy=$(curl -fsS "http://127.0.0.1:${port}/api/state" | python3 -c '
import json, sys
state = json.load(sys.stdin)
benchmarks = state.get("benchmarks", [])
tasks = state.get("tasks", [])
active_benchmarks = sum(
    1 for benchmark in benchmarks
    if benchmark.get("status") in ("queued", "building", "running") or benchmark.get("progress")
)
active_tasks = sum(1 for task in tasks if task.get("status") == "preparing")
print(active_benchmarks + active_tasks + state.get("discussions_pending", 0))
')
if [ "$busy" != "0" ]; then
    echo "refusing restart: $busy active item(s)" >&2
    exit 1
fi
systemctl restart "$service_name"
