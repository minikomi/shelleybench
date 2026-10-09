# Shelley Bench

Shelley Bench runs, records, and compares coding-agent benchmark attempts. It
stores task definitions, run events, metrics, and retained artifacts locally.
The web UI is intended for trusted local or otherwise protected access; it does
**not** provide user authentication or authorization.

## Prerequisites

- Python 3.12 or later
- Git
- SQLite (available through Python's standard library)
- bubblewrap for isolated benchmark execution on Linux
- curl to use the optional `restart.sh` helper
- A Shelley checkout and any runtimes required by the versions being tested
- Credentials and network access for the model providers you choose

Benchmark preparation, runs, generated findings, and follow-up discussions can
call paid inference providers. Review the selected model, attempt count, and
provider billing before submitting a write request. Do not expose the service
on an untrusted network without placing it behind authentication and suitable
network controls.

## Local setup

Clone the repository, choose a directory for local data, and start the server:

```sh
python3 -m app.server --port 8138 --data "$HOME/.local/share/shelley-bench"
```

Before preparing tasks or running models, configure the Shelley checkout,
executable, and assistant model in Settings or the environment shown in
`.env.example`. The app does not automatically load `.env` files.

Open `http://127.0.0.1:8138/` in a browser. The server binds to loopback by
default. Use `--host` only when exposing it behind suitable authentication and
network controls.

The application requires no JavaScript build step. Its UI uses Datastar; see
`app/static/vendor/DATASTAR-LICENSE.txt` for its bundled third-party license.

## API

The JSON API is served under `/api/`. Start with these read-only endpoints:

- `GET /healthz` — service health.
- `GET /api/state` — current settings and available state.
- `GET /api/tasks`, `GET /api/benchmarks` — saved tasks and benchmarks.
- `GET /api/tasks/<id>`, `GET /api/benchmarks/<id>`, `GET /api/runs/<id>` —
  detailed records.
- `GET /api/jobs/<id>` — job progress and errors.

Write endpoints create or change work and may trigger paid inference:
`POST /api/tasks`, `POST /api/benchmarks`, and the task/benchmark action
endpoints. Inspect the request and response shapes in `app/server.py` and read
`/agents.txt` before automating them. `GET /agents.txt` is a plain-text guide;
`/robots.txt` only advertises that guide and does not set crawler policy.

## Data retention

Finished job checkouts and isolated dependency/build caches may be cleaned up.
Saved run records, metrics, events, trajectories, patches, and logs are local
benchmark data. Treat the data directory as sensitive: it can contain source
material, prompts, artifacts, and provider-related metadata.

To preview removable files:

```sh
python3 -m app.cleanup --data "$HOME/.local/share/shelley-bench" --dry-run
```

## Optional systemd service

`deploy/` contains example systemd units for a dedicated Linux host. Before
installing them, edit the service user, working directory, Python path, port,
and data directory for your host. Then copy the units to `/etc/systemd/system/`,
run `systemctl daemon-reload`, and enable the service (and optionally the cleanup
timer). Keep credentials out of unit files when possible, and give the service
account only the access it needs.

The runtime's Shelley, Node, Go, and profile configuration is deployment
specific. Configure those through the runtime's documented environment/profile
interface rather than hard-coding toolchain locations. The supported variables
are `SHELLEY_BENCH_SHELLEY_BINARY`, `SHELLEY_BENCH_SHELLEY_REPO`,
`SHELLEY_BENCH_BASELINE_REF`, `SHELLEY_BENCH_ASSISTANT_MODEL`,
`SHELLEY_BENCH_NODE_ROOT`, `SHELLEY_BENCH_GO_ROOT`,
`SHELLEY_BENCH_PLAYWRIGHT_BROWSERS`, and
`SHELLEY_BENCH_RUNTIME_PROFILE_DIR`. Most are optional; consult the runtime
documentation for required values and profile-file restrictions.

## Operator-supplied imports

Private study catalogues and historical benchmark results are not included.
Import utilities require an explicit user-supplied manifest and local source
paths. Imported rows and trace snapshots remain private operator data; see
`SECURITY.md` before sharing them.

```sh
python3 -m app.import_results --manifest /path/to/manifest.json \
  --archive /path/to/archive --history-json /path/to/history.json \
  --data /path/to/local-data --dry-run
```

Run the synthetic unit tests with `python3 -m unittest discover -s app/tests`.
These tests do not require model inference.
