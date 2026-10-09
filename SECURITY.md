# Security and private data

This application has no built-in authentication or authorization. Keep it on
loopback or behind an authenticated reverse proxy and appropriate network
controls. Write API requests can launch paid model inference and execute code.

Benchmark actors use filesystem isolation, not a hostile-code security boundary.
They retain network access. Run only trusted tasks and repositories under a
service account with appropriate permissions and provider access.

The data directory can contain source code, prompts, model responses, actor
SQLite databases, traces, patches, logs, and discussions. Treat it as private.
Do not commit it, upload it as an issue attachment, or expose artifact endpoints
publicly without an independent content review. Importing/exporting records is
not anonymization.

This repository distributes application source and synthetic unit tests, not
benchmark results or provider credentials. Keep secrets in an operator-managed
environment or credential mechanism, never in deployment units or commits.

Before sharing diagnostic output, remove tokens, personal paths, private host
names, repository contents, and model/user messages.
