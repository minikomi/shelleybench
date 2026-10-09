# systemd examples

This directory provides optional systemd unit templates for a dedicated Linux
host. They are examples, not an access-control solution: Shelley Bench has no
built-in UI authentication. Keep the service on a trusted local network, or
place it behind a reverse proxy or network layer that enforces authentication
and authorization.

## Configure

Before installation, edit both service files to set values appropriate for the
host:

- `User` and `Group` to a least-privileged service account.
- `WorkingDirectory` to this checkout.
- `ExecStart` to the Python interpreter, port, and data directory to use.

Use a data directory writable only by the service account. It may contain task
source, prompts, artifacts, and model-provider metadata. Keep model credentials
and runtime configuration outside the repository, such as in a protected
systemd environment file or another secret-management mechanism.

The application exposes command-line `--host`, `--port`, and `--data` settings. Runtime
configuration is supplied through environment variables. `SHELLEY_BENCH_SHELLEY_REPO`
identifies the Shelley checkout; `SHELLEY_BENCH_SHELLEY_BINARY`,
`SHELLEY_BENCH_BASELINE_REF`, `SHELLEY_BENCH_ASSISTANT_MODEL`,
`SHELLEY_BENCH_NODE_ROOT`, `SHELLEY_BENCH_GO_ROOT`,
`SHELLEY_BENCH_PLAYWRIGHT_BROWSERS`, and
`SHELLEY_BENCH_RUNTIME_PROFILE_DIR` configure optional runtime behavior.
Consult the runtime documentation for required values and profile-file
restrictions rather than hard-coding toolchain locations here.

## Install

After editing, install the main service:

```sh
sudo cp deploy/shelley-bench.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shelley-bench.service
```

To run periodic cleanup, also install and enable the cleanup timer:

```sh
sudo cp deploy/shelley-bench-cleanup.service deploy/shelley-bench-cleanup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shelley-bench-cleanup.timer
```

`restart.sh` refuses a restart while it observes active benchmark, preparation,
or discussion work. Run it as a user allowed to restart the chosen service, or
use `systemctl` directly after checking the service state.
