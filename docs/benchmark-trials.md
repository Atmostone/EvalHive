# Durable benchmark trials

Implementation: SPA-141 / B04. The storage and resource authority described here
are implemented; the automatic resource reconciler, deadline service and worker
phase driver remain required before production launch is enabled.

## Ownership and lifecycle

`BenchmarkTrial` is one attempt, with a fixed, server-redacted input snapshot and
a fixed deadline. A retry creates another trial. Its lifecycle is `created` →
`planning` → `preparing` → `ready` → `driving` → `capturing` → `evaluating` →
`cleaning` → `finished`. Any unfinished phase can enter `cleaning`.

Execution outcome, evaluator status, recording completeness and cleanup status
are independent. Finishing execution does not release an environment or produce
a correctness verdict. All resource leases must be released before cleanup can
finish; unresolved native effects keep the host reserved.

A controller has a UUID, generation and heartbeat expiry. Claiming an expired
controller increments the generation and enters recovery/cleanup; it does not
restart the old native conversation or assume that a lost command did nothing.
An old controller's callbacks, events and heartbeats cannot mutate the new owner.

`EnvironmentSession` records the immutable origin generation of its resources.
Docker labels do not change at takeover. A recovery authority is bound to the
new controller but can inspect, privately capture, stop and release resources
from an old session. It cannot allocate resources or execute another command.

## Locks and admission

Ownership, phase changes and resource operations use a PostgreSQL session
advisory lock on a dedicated checked-out connection. The lock survives commits
of intents and receipts. It is explicitly released before returning the physical
connection to the pool; a failed unlock invalidates that connection.

Host admission additionally takes a global host lock, always before the trial
lock. It measures capacity while holding the reservation lock and stores the
whole group's exact quotas and unchanged native minima before preparation.
Installing a container lease must match an admitted resource and quota. A second
session cannot simultaneously consume the same admitted resource. Network
leases have no independent CPU/memory grant.

The host reservation has no automatic expiry. Expiring a controller does not
prove that its native processes stopped. Only completed resource cleanup releases
the reservation for the next trial.

Heartbeat and monotonic cancellation requests use conditional SQL without the
session lock. They remain available while a native command is in flight. Event
delivery uses a short transaction with a trial row lock and validates the current
controller. Atomic sequence allocation lets resource receipts and multiple event
streams share one contiguous journal without waiting for a long native command.
Phase barriers and finalization use the same row lock to serialize with events.

## Journal and delivery barriers

Each registered worker UUID and generation has one event stream. Its sequence is
process-wide, independent of the event's `source`. Batches contain 1–128 bounded
SDK records. Duplicate identity/content is accepted, changed content is rejected,
and a sequence gap rolls back the entire batch.

A phase checkpoint requires all events through the specified sequence. The host
must first receive the ordered JSONL command response and durably drain all prior
frames. Execution completion requires acknowledged barriers for its registered
streams. The stream remains available for capture and evaluation. Permanent
sealing is reserved for final worker shutdown and requires its complete sequence.

Public journal entries contain whitelisted lifecycle/resource metadata. Raw
benchmark events are private by default, including native observations and usage
payloads. Consumers must explicitly build a redacted public projection. Direct
SQL updates of journal entries, trial inputs and installed session plans are
rejected by database triggers.

## Resource authority and private state

The host installs an immutable plan with resolved image IDs, paths, resource
identities, dependencies, actor permissions, private input references and output
grants. The worker can name a plan resource or opaque handle. It cannot provide
host paths, images, mounts, export paths or grant itself private inputs.

`SqlResourceAuthority` is mandatory for the rootless provider. Every request is
bound to its controller, actor, environment origin, provider, phase and host
reservation. It commits a request before native effects, a resource intent before
creation, and an exec control identity/key before startup. Completed receipts
replay after reconstructing the authority in another process; reused IDs with
different content fail. Failed receipts also remain stable. An interrupted or
uncertain operation requires reconciliation instead of being issued again.

Private plans, resource intents, exec control keys, inputs, requests, results and
native event payloads use versioned AES-256-GCM storage. The key is derived with
HKDF-SHA256 and a purpose-specific context from the server secret supplied by the
host. Each ciphertext authenticates its row ID and purpose; replacing a row,
changing ciphertext or using another key fails closed. Records are bounded at
32 MiB; large evidence belongs in artifact storage. The explicit JSON lease codec
cannot deserialize executable objects. `cryptography` is pinned to 50.0.2.

Release/restore must preserve the server secret alongside database backups.
Changing that secret requires an explicit private-record migration. No secret
value is included in public snapshots or the journal. Engineering tests use an
isolated fixture key and never read the owner's provider credentials.

## Validation

`backend/tests/integration/test_benchmark_trials.py` exercises real PostgreSQL:
receipt recovery, content conflicts, uncertain/cancelled operations, committed
intent fencing, independent heartbeat/cancellation, continuous concurrent event
delivery, phase barriers, recovery with old Docker ownership, admission grants,
immutable data and private storage authentication. Alembic `bg02` is tested with
upgrade, downgrade to `bg01` and upgrade again. Native Docker recovery and the
automatic worker driver are separate remaining B04 acceptance checks.

The fence follows PostgreSQL's
[session advisory lock semantics](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS).
Private storage follows the cryptography project's
[AEAD API](https://cryptography.io/en/latest/hazmat/primitives/aead/).
