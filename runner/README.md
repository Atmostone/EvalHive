# EvalHive runner

The runner supervises one SDK connector, commits a bounded SQLite WAL spool
before event delivery, retries unacknowledged batches, tracks heartbeat and
persists command receipts before acknowledging them. It imports neither the
backend nor a native harness library.

Recovery replays both events and completed command receipts. A damaged journal
ends an interrupted attempt with explicit recording loss; it never triggers a
second connector execution. Commands interrupted before a durable receipt remain
uncertain rather than being replayed as if they had never run.

The backend keeps the runner outside the connector container and attaches its
JSONL pipes through the rootless Docker runtime. The attempt API token stays on
the server. Only native credentials are mounted privately into the connector;
they are excluded from the workspace and artifact tree.

Install the Python SDK first, then `pip install -e runner`. Run
`python -m pytest -c runner/pyproject.toml runner/tests` with the SDK examples
prepared as described in `sdk/README.md`. Tests use fake connectors and no model
credentials. The runner and SDK suites must pass together when the protocol changes.
