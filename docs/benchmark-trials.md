# Durable benchmark trials

Implementation: SPA-141 / B04. The storage and resource authority described here
are implemented, together with durable worker commands, automatic phase driving,
resource recovery and deadline services. Product launch APIs and queue composition
remain B14 work; these components do not enable production launches by themselves.

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

An owned exec starts and records its running intent under that fence. Waiting for
stdout and process completion is a non-mutating observation: it releases both
the session lock and the physical connection. Cancellation and heartbeat must
remain available even with a single-connection pool. Before any subsequent write
or native control, the observation reacquires the fence on a checked-out
connection, verifies the current generation, expiry and unfinished phase, and
reloads mutable lease state. A concurrent stop cannot be overwritten by a late
completion. Losing ownership retires only the original operation as uncertain;
it does not append late events to the new owner's or a finished trial's journal.

Host admission additionally takes a global host lock, always before the trial
lock. It measures capacity while holding the reservation lock and stores the
whole group's exact quotas and unchanged native minima before preparation.
Installing a container lease must match an admitted resource and quota. A second
session cannot simultaneously consume the same admitted resource. Network
leases have no independent CPU/memory grant.

Planning can start with a `planning` admission for one isolated registered worker.
Only its `worker` container lease can be installed under that budget; native
execs, exports and task/environment leases are unavailable. It can read its
installed dataset and exchange validation/plan commands without preparing native
task services. The worker's image, mounts and quota are still server-owned.

After resolving the plan against installed assets, the host finalizes admission
exactly once, under the same host/trial locks. The full group must retain the
planner's exact original resource demand, including native minima. The existing
worker and its immutable session remain alive; no resize or replacement is
implied. Preparation and execution commands require the full-group budget.
Further changes to a finalized group fail with `ADMISSION_CONFLICT`. A failed or
cancelled finalization preserves the planning reservation for scoped cleanup.
Capacity is measured again, conservatively counting the live planner's complete
quota against currently available capacity; there is no unmeasured memory credit.
Hosts near capacity can reject a group rather than overcommit it.

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

### Reverse agent call receipts

`AgentCallStore` records the SDK's reverse `agent.call` commands in `bg12`.
It uses the existing Harness `ExecutionAttempt` as the session rather than
introducing a second execution state machine. A controller/worker-derived UUID
identifies the intended session before the Harness attempt exists. Each stream
can request one `run` or `open`; additional control, event and close calls must
reference that session. A startup intent binds a fresh current Harness attempt
before native agent execution may begin.

Every call requires the current execution controller, registered worker, driving
phase, remaining deadline and full host reservation. Short row-locked
transactions commit encrypted requests before effects and immutable encrypted
responses before SDK acknowledgement. A completed duplicate returns its saved
receipt; an interrupted call cannot authorize another model request. Recovery
claims cannot replay an old controller's receipts.

Successful completion replies must match a terminal Harness result and its
scope-bound evidence digest. Control replies must match the native command's
saved acknowledgement and original payload. Event pages must reproduce committed
Harness events and respect the requested cursor and limit. Execution status is
independent of benchmark correctness: failure, cancellation and transport loss
retain their original meanings.

Cleanup cannot release the host reservation while an intended Harness attempt
remains active, including an attempt created just before a crash prevented its
binding. Unfinished agent call receipts make recording incomplete. A downgrade
below `bg12` refuses to discard calls belonging to unfinished trials.

This ledger is a host integration component. Wiring the native agent executor
and replacing the execution driver's provisional drive-response handling are
remaining B08 work; the component alone does not advertise `host.agent_api_v1`
or enable product launches.

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

## Retained evidence and role usage

B05 is in progress. `EvidenceStore` implements the host artifact sink using an
explicitly supplied object client and installed bucket. It consumes complete
archive streams into bounded temporary files, computes SHA-256 and size, uploads
under scope/visibility/content-addressed keys and checks the storage receipt.
Only then does it commit an immutable `BenchmarkArtifact` row after rechecking
the current controller. Failed, oversized, interrupted or fenced transfers never
publish a successful reference. Object locations are encrypted; SDK references
contain opaque `artifact:` identities. Unreferenced objects from interrupted
publication can be collected separately after a retention grace period.

Broker exports publish through the resource call's existing fenced connection.
Storage never waits for a second pool checkout while that export holds the trial
fence. The pre-transfer ownership check releases its row lock before object I/O,
preserving independent heartbeat/cancellation. The post-transfer publication
rechecks origin and output identity/visibility/size grants before committing.

The first receipt for a session/output identity is immutable. Another capture
cannot replace its bytes or visibility; repeated identical bytes return the same
receipt. Cold recovery can publish old-origin data only privately. Migration
`bg05` enforces reference immutability and the session/workspace/origin foreign
key. Restore must retain both object bytes and their database receipts.

Consumers explicitly grant allowed visibility. Scoped reads compare the complete
reference, spool the retained object, and verify its size and digest before
yielding bytes. Missing or corrupt objects are recording failures. Evaluator and
private visibility cannot be promoted by modifying the caller's reference.
Blocking storage calls retain their spool until completion even after caller
cancellation; deployment clients must use finite network timeouts.

`TrialDriver` requires a trusted capture validator. The production validator
checks retained bytes and every required output's exact session identity before
native grading. The frozen session registration includes the canonical output
plan. A schema-valid artifact URI alone is insufficient. Engineering fixtures
supply explicit test-only validators; there is no implicit production fallback.
Object operations use the pinned [MinIO Python 7.2.20 API](https://github.com/minio/minio-py/blob/7.2.20/docs/API.md).

Output `stage` defaults to `capture`; `evaluation` declares files produced by the
checker itself. Stage is part of the canonical SDK plan and registered asset
policy, and a worker cannot change it. Pre-grading validation requires capture
outputs. Before accepting a completed evaluation, the host validates both stages
using the combined capture/evaluator references. A failed/unavailable checker
does not need to fabricate outputs for a completed verdict; any references it
does report still undergo integrity checks. Different captures of a changing
native path require different logical output IDs to preserve immutable evidence.

`trial_usage` rebuilds a ledger from the scoped immutable journal in bounded
pages, with a fixed high-water mark for a live trial. It groups by role, model and
purpose. Within a registered actor stream, `usage_id` identifies one model
request. An immutable delta measurement announced again is counted once;
cumulative snapshots replace earlier snapshots of that request. Adapters must
use distinct IDs for distinct requests and avoid overlapping summary series.
Conflicting modes/dimensions or decreasing known token counters invalidate that
call's projection and remain visible as errors, without rewriting native events.
`evaluation_usage` uses the same reader and projection for one independently
authorized regrade journal. Its watermark belongs to that evaluator. Matching
actor/call IDs in another evaluation cannot add to its usage or charge the source
execution; unknown measurements retain the same null/subtotal semantics.

Unknown tokens and cost remain null. Each projection reports the known subtotal
and unknown call count, without presenting that subtotal as a complete total.
Costs use exact decimal arithmetic and retain provider/estimate provenance.
Out-of-policy cost strings remain unknown with an explicit projection error.
An empty ledger does not claim complete measurement. Initial evaluator attempts
are described below. Product regrade launch and native aggregation history remain
B14/B15 work; retained files alone do not authorize regrade.

## Initial evaluator attempts

`BenchmarkEvaluationAttempt` records one evaluator intent and its confirmed
terminal result independently of execution. Starting the initial attempt and
entering `evaluating` are atomic. Inputs fix the applied spec, evaluator manifest,
worker actor, native preparation/execution/capture context, and immutable
environment session plan digests. Inputs and raw native results are encrypted;
database guards freeze inputs and the first terminal outcome. The initial
attempt shares its execution controller and admitted environment, keeping native
services alive for grading.

The evaluator receives the attempt ID. Its durable command uses that same ID;
completion requires that exact worker actor/generation, a completed event barrier
and a successful wire receipt whose result matches the proposed result. Another
command's output cannot substitute for it. Returned artifact references are also
validated against retained bytes. Identical repeated begin/completion reads the
original record without adding another journal entry or invoking the checker.
The final transaction checks committed cancellation and the database deadline
before accepting a new verdict; a late confirmation cannot outrun an earlier
stop request. An already confirmed receipt remains stable on repeated reads.

Completed evaluation with success false means `incorrect`; it remains a completed
checker and a successful execution. A null success remains unknown, regardless
of metric values. Checker errors, transport/infrastructure loss, recording
failures and cancellation remain distinct outcomes. Cleanup has its own result
and cannot change an existing evaluator verdict. A lost controller terminates a
pending initial evaluation as uncertain without accepting a late native result.
Unavailable/unrequested grading does not fabricate a successful evaluator call.

Migration `bg06` enforces trial/workspace and source-attempt scope, one initial
attempt per trial, complete terminal writes and immutable input/result fields.
Public history contains normalized metrics, status and verdict, without native
state, hidden assets or raw error messages. Regrade creates another attempt with
independent ownership/resources and a sufficient verified native checkpoint,
while preserving the finished execution. No current
API treats a capture's arbitrary native metadata as permission to regrade.

`regrade.retained_source` now reads a finished source evaluation without changing
its execution, journal or resources. It requires the source connector's explicit
native regrade capability and a complete typed checkpoint matching the captured
case revision/source digest and connector version. It verifies each checkpoint
artifact against the original capture and retained storage. Its private output
contains only declared evaluator model/credential references, excluding agent and
orchestrator roles and mutable native session handles. Older captures without
this proof remain unavailable for replay. Evaluation creation and installation
resolution authorize references without downloading bodies; the owned host
materializer verifies all checkpoint bytes after full disk admission.

Independent regrade ownership now has dedicated `BenchmarkEvaluationControl` and
immutable `BenchmarkEvaluationJournal` tables (`bg07`). Creating the new regrade
intent, its controller lease and first journal entry is atomic after source
verification. Generation/heartbeat/cancellation and the journal counter belong
to that evaluator, while its source execution remains finished and unchanged.
Both execution and evaluation contexts use `SqlControlRepository` for current
owner checks and atomic journal numbering. Execution APIs reject an evaluator
owner token. Expired evaluator takeover enters cleanup, records an uncertain
grading result and fences the old owner; it cannot resume native interaction.
The failed invocation retains a typed source checkpoint for a later new regrade.

The lease repository supplies ownership to the regrade driver described below.
Source resource leases are not transferred to it. The source installation loader
and retained-state materializer supply fresh grants; product launch wiring is B14.

Resource sessions now carry an immutable optional evaluator control ID (`bg08`).
Its composite foreign key binds the control, source trial and workspace. Operation
receipt identity includes that context, with null treated as the execution
context. A database trigger prevents receipts from naming another context's
session and prevents changing an existing binding. Broker routing, direct
authority, deadline discovery, recovery and evidence publication check the same
context. A matching actor/request ID from execution cannot replay as an evaluator
receipt, and a source execution handle cannot become evaluator authority.

Evaluation planning reuses the existing whole-group admission transaction. A
takeover transfers only the reservation whose exact prior owner/generation
matches the recovering evaluation; another evaluation's reservation is retained.
This resource boundary is implemented. The complete restore/evaluate coordinator
remains required before product launch.

Worker streams also bind to the evaluator control (`bg09`). Execution and
evaluation share `StreamLedger` and `CommandStore`, including bounded event
delivery, duplicate detection, encrypted receipts, delivery barriers and ACKs.
Stream identity includes its immutable control, so the same actor, request and
event IDs can exist independently in execution and regrading. Evaluation events
increment only their own journal counter. Database guards reject cross-context
journal references and stream rebinding. Late events and command responses are
fenced after evaluator takeover; recording failures affect only that evaluator.
An SDK-only worker test exercises describe, validate, event draining and shutdown
through the evaluator channel without reopening the finished execution.
Downgrading to a schema without evaluator stream identities rejects historical
identity collisions rather than merging their records. Release rollback must use
the matching database snapshot when the older schema cannot represent new data.

The SDK now distinguishes execution and evaluation-only specs, plans and readiness.
An evaluation spec carries the typed source checkpoint and exactly its declared
evaluator model roles/private references. Plans and readiness omit agent access;
the wrapper rejects `drive` and purpose changes. `RootlessWorkerHost` validates
the spec/plan against its controller before compilation or group admission,
records the session purpose and excludes driving from evaluator actor policies.
Rootless asset compilation retains the same native grants and resource minima
without requiring a synthetic agent. The execution driver rejects evaluation-only
specs before starting a worker. Legacy specs without purpose remain executions.
These SDK boundaries do not themselves materialize retained artifacts or authorize
product launch.

Execution and evaluator stores now share `ControlLifecycle`: owner-checked
control snapshots, journal notes, monotonic controller cancellation, phase gates,
admission checks and final cleanup accounting. The evaluation graph has no
driving phase. Its execution status is `not_applicable` in the control snapshot;
its grading status comes from the immutable evaluation attempt. No artificial
execution result or second execution trial is created.

An evaluator result requires its own attempt ID and the exact completed durable
command from its worker actor, context and generation. The shared receipt verifier
also serves initial evaluation. Mismatched outputs, another actor's response,
committed cancellation and an expired deadline cannot produce a new verdict.
Repeated identical completion reads the original terminal result. Cleanup failures
are independent and do not overwrite native correctness.

Migration `bg10` adds evaluator cleanup status and terminal time. A finished
evaluator lifecycle is database immutable. Final cleanup checks only that
context's leases, streams, operations and commands, and removes a host reservation
only when the complete owner/generation matches. Another context's unsealed
stream or live resource is not its cleanup authority. Lost-owner takeover seals
an unfinished grading result as uncertain and enters cleanup. A stale controller
cannot cancel its replacement through the controller stop method; user-requested
workspace cancellation remains a separate API.

`HostAdmission` provides the same reservation transaction to both stores. Neither
store borrows another store class's methods or uses an artificial execution row.

## Supervised regrade worker

`PhaseDriver` owns heartbeat, cancellation, database deadlines, bounded phase
waits, partial workflow state and teardown. `TrialDriver` adds the execution
workflow; `RegradeDriver` adds restore/capture/evaluate without drive. Preparation,
capture, evaluation delivery barriers and cleanup share their implementation.
Partial preparation/execution results remain available to teardown when a later
phase raises. Recovery enters cleanup without launching a replacement worker.

Creating a regrade freezes a complete evaluation-purpose spec in its immutable
intent. It preserves the retained case, public configuration, evaluator-only
models/references and resource limits, with a new wall-clock budget derived from
the evaluation deadline. A creation retry reads the original receipt and budget;
it does not regenerate a deadline or change the input digest.

The driver reads that saved spec through `launch_inputs`, checks the input digest
and authorizes retained references without unadmitted body downloads. A source with
missing resource limits cannot create a new runnable invocation. The selected
worker must match the saved evaluator metadata; native validation cannot silently
change the frozen effective configuration.

The new SDK worker prepares from `spec.checkpoint`, reports evaluation readiness
without agent access, captures its restored environment and evaluates under its
own attempt ID. Its result uses the exact durable command receipt. The original
execution and its evaluator result remain unchanged. A caller cancellation waits
for bounded cleanup before propagating; stale ownership causes local detachment.

`RetainedInputs` includes every checkpoint artifact in the complete group disk
reservation, counting retained copies, one sequential download spool and file
metadata headroom. Mount selection cannot omit that verification budget. After
admission it commits `bg11` file intents, verifies bytes on the admitted data
volume and publishes read-only payloads in the new environment session. The
connector restores its format inside the task container. EvalHive does not unpack
native checkpoint archives on the host. Files are released only after all owned
resource leases, including worker leases, have been released. Pending files block
terminal cleanup and reservation release. A downgrade below `bg11` refuses live
file intents; recovered owners clean predecessor files without resuming work.

The source worker session retains its complete manifest/catalog, actual image ID
and bounded content proofs for installed read-only files. The installation loader
checks saved session digests, process identity, catalog/file consistency, static
bytes/modes and availability of the exact native image IDs. It never consults the
current connector catalog or grants access to old writable world paths. A moved
manifest image tag cannot change the fresh worker's retained image ID. Missing
metadata or changed static inputs report exact replay as unavailable.

SDK-only process tests exercise inline checkpoint restoration and failure/stop
paths. The rootless/MinIO fixture additionally restores an archive larger than
32 MiB in a fresh worker/task container, checks its read-only grant, native verdict
and cleanup, rejects changed static files or unavailable image pins, and tolerates
a reassigned source image tag. These verify the generic worker workflow, not
acceptance of the planned native benchmark adapters. Product launch wiring is B14.

## Durable worker commands

`BenchmarkCommand` stores one immutable command intent per worker stream and
request ID before transmission. The `sent` state means the request might have
been sent: a crash between commit and write never permits blind replay. The full
wire success/error receipt is encrypted and committed as `received` before method
result validation or event delivery is claimed. An interrupted command keeps its
unknown outcome; another generation cannot save a late result.

`WorkerChannel` drains events concurrently with commands. After a response, it
snapshots the SDK's received-frame count and waits for durable delivery through
that count. Future background events do not extend the barrier indefinitely.
Completing the command records the current stream checkpoint and only then sends
ACK. Capture and evaluation can continue on the same unsealed stream; shutdown
and final draining precede permanent sealing. Duplicate commands share an
in-flight task or read the saved receipt instead of sending another native call.
Invalid method results remain recorded and explicitly incomplete.

Command metadata uses short trial row locks, without holding the native-resource
session fence while awaiting a worker. Reverse broker requests, cancellation,
heartbeats and event persistence remain responsive during native execution.
Database triggers protect command intent and the first received response.
Unresolved commands prevent execution completion and preserve incomplete recording.

For reverse RPCs, the host's pure broker receipt reader can resolve acknowledged
IPC tombstones from its SQL authority. It never dispatches an operation; a missing
receipt stays uncertain. Workers explicitly opting into that durable-host contract
ACK successful broker replies, preserving bounded payload buffers through long
interaction loops. Generic peers without a receipt reader keep their strict
non-reexecution tombstone behavior.

Transport shutdown bounds every process wait and the final stream drain. A
worker that ignores stop, a fenced stop callback, or a pipe retained after its
parent exits yields `TRANSPORT_CLOSE_INCOMPLETE`; it cannot hang the runner or
claim complete recording. Detaching closes only that transport's socket. It does
not release resource leases or bypass the current owner's resource authority.
Resource cleanup remains the responsibility of the durable controller/recovery
service. A blocked journal writer likewise prevents final stream sealing.

## Server resource registration and worker provisioning

`SqlBroker` routes a registered actor's requests to installed provider factories.
It resolves logical names and opaque handles using scoped SQL rows. Workers cannot
choose a session, actor, image or host path. A prior operation is looked up before
the current resource name: releasing or replacing a resource cannot redirect a
retry into another session. The request ID is unique across the actor's entire
trial generation, matching the SDK's process-wide broker ledger. Conflicting
concurrent requests are rejected before a second effect. Database triggers freeze
the route, request, receipt and terminal outcome. Removed providers produce an
explicit error; there is no fallback provider. IPC receipt readback calls only
the provider's pure replay path.

The rootless provider's `RootlessAssets` consumes bounded JSON installation data,
separate from the benchmark's public configuration. The registration identifies
the connector/version, provider, worker asset and named container/network assets.
It is a bounded resource view for the selected case, not an obligation to load a
whole large dataset into one JSON file. Installers can provide per-case views
alongside the shared worker declaration using their verified dataset inventory.
Container assets specify immutable local images, native startup defaults, quotas,
unchanged native minima, registered file mounts and output rules. File grants come
from the host's verified installation inventory, with a content digest and a
separate writable permission. File verification belongs to that installer; a
worker cannot manufacture an installation receipt. Source digests participate in
the effective fingerprint, without including relocatable host paths.
The complete group must fit the trial's requested memory, CPU and disk budget as
well as host/group policy. Worker overhead and independent harness resources
count toward that budget; native resource requirements are never lowered to fit.
A registered runtime file reference may resolve to the standalone supervisor or
the selected harness's complete runtime bundle. The host chooses one mount for
that reference, preserving the task image and avoiding a combined image for every
harness/benchmark pair. Bundle compatibility remains a pre-model launch check.

A task plan's rootless resource config selects `asset_id`, optional logical
`network`/`namespace` dependencies and private-bridge DNS aliases. It cannot supply
images, host mounts, startup commands or quotas. Benchmark-native command and
configuration details remain in its worker, using owned execs in the granted
environment. Other providers own their own registration/config vocabulary.
Outputs must match registered paths, visibility and requiredness exactly; a
required output cannot disappear from a plan. Whole-rootfs exports stay private.
The canonical SDK plan remains independent of rootless installation details.

Declared dependencies become immutable lease metadata even when no native network
link is involved. Allocation requires those dependencies to be allocated; a
provider cannot drop them when saving a lease. Recovery uses the same graph in
reverse. Logical IDs are unique among live resources, including networks, so a
name cannot ambiguously identify two sessions.

`RootlessWorkerHost` composes these pieces without importing native benchmark
modules. It admits and starts a registered planner container, attaches its JSONL
transport, installs durable broker handling/readback and checks its SDK identity,
schema, capabilities and requirements. The worker's reported image reference is
not a self-referential Docker digest: the actual immutable image is checked and
recorded by the host. After the worker returns a plan, the host resolves it against
installed assets, finalizes group admission and freezes session grants. The
benchmark worker and host controller have distinct actor identities; the worker
has no permission to control its own planner container. Harness resources can be
admitted independently as additional group demands. This provisioner does not
choose a harness or infer a correctness verdict.

## Automatic controller

`TrialDriver`, using the shared phase supervisor, runs validation, planning,
preparation, execution, capture, native evaluation and cleanup through that host
and its durable SDK channel. Effective
configuration is validated and recorded before planning. Typed capture references
and evaluator results are checked before recording a verdict. RPC completion is
an execution outcome, not evidence that the answer is correct.

Independent supervised tasks renew the controller lease, poll cancellation and
the database deadline, and enforce owned-exec deadlines. Phase waits do not hold
the resource fence. Cancellation first requests worker and optional harness
cooperation, bounds pending-command draining, and then quiesces active execs.
All service waits, phase deadlines and teardown waits are bounded. A failed
supervision service causes detachment; it does not authorize further native work.

Warm cleanup conditionally stops active execs while retaining completed preparation
processes, their background services and the environment's PID 1 for capture and
grading. Missing native capture falls back to declared file outputs. Failure to
capture required evidence leaves cleanup pending, resources unreleased and the
host reservation held. A native cleanup failure is recorded independently and
remains visible even if subsequent resource release succeeds.

An expired controller is claimed with a new generation in `cleaning`. Cold recovery
does not start another worker or resume a native conversation. It stops old
resources in dependency order, exports declared files using immutable origin
ownership, and downgrades all recovered output to private visibility. File
recovery explicitly reports native state unavailable; it cannot manufacture a
grading checkpoint. Release follows successful capture. A stale driver detaches
only its local transport and cannot stop or release the new owner's resources.

A driver instance is single-use. Another driver observing a partially running
epoch detaches instead of taking over its cleanup. A retry is a new trial; only
an explicit expired-owner claim permits recovery. Initial evaluator history and
production evidence storage and the independent regrade worker workflow are
implemented in B05, including retained-file materialization and source asset
resolution. Product API and queue wiring are B14 work.

## Recovery and deadlines

`RecoveryService` dispatches unreleased leases to registered provider factories.
Dependency handles belong to provider-independent record metadata; the coordinator
does not decode a provider's private intent or native-state format. A record
without valid dependency metadata fails closed and requires a storage migration.
Execs precede containers, namespace users precede namespace owners, and containers
precede networks. A failed or unconfirmed child release blocks its dependencies.
Missing providers remain visible errors. A default sweep quiesces resources; the
host applies the environment's capture policy before requesting release. The
service never finishes a trial or releases its host reservation itself.

The rootless reconciler finds unconfirmed creates by exact origin/resource labels
and image identity. It never repeats startup. An absent unconfirmed create stays
uncertain: a daemon request can finish after its caller disappears. Absence of a
previously confirmed identity can complete cleanup. Interrupted stop/remove
operations retain their intents for another observation.

Read-only create validation failures have a separate `rejected` state with no
native identity. Such resources can be stopped/released without native lookup or
quiescing an exec's parent. Stopping a never-created resource records
`unallocated`, preserving that proof for cleanup. Cancellation and lost mutation
replies never receive this classification.

A missing owned-exec supervisor makes its parent unsafe. Quiescence stops only
that owned container and preserves its filesystem. A verified stopped/missing
parent proves that an old exec cannot run there, even if its individual identity
was lost. Scope/image mismatches never authorize stopping another container.

`DeadlineService` discovers due execs in PostgreSQL and re-enters the current-owner
authority. It compares the persisted deadline with the database clock. Its loop
is independent of an attached transport and reports results/errors to a durable
projection callback owned by the controller. Its reconciler comes from the same
provider registry; the service contains no native runtime implementation.

Supervisor 1.1 adds authenticated conditional `expire`. It reaps the main process
when processing the command and starts cancellation only if that process is still
active. Native completion retains background descendants for grading. Deadline
enforcement avoids a remote status check followed by an unconditional stop.
Active cancellation still bounds TERM → KILL for owned descendants; unrelated
services and the task container remain alive.

Bundle compatibility requires supervisor 1.1 and `conditional_expire`. Older
bundles need new immutable images and receipts. They cannot silently pass the new
preflight or have their historical fingerprint changed.

## Validation

`backend/tests/integration/test_benchmark_trials.py` exercises real PostgreSQL:
receipt recovery, content conflicts, uncertain/cancelled operations, committed
intent fencing, independent heartbeat/cancellation, continuous concurrent event
delivery, phase barriers, recovery with old Docker ownership, admission grants,
immutable data and private storage authentication. Alembic `bg02` is tested with
upgrade, downgrade to `bg01` and upgrade again. The recovery tests additionally
check unavailable providers and dependency release barriers.

`test_benchmark_recovery_native.py` runs against an isolated PostgreSQL and the
actual rootless daemon. It covers lost creation receipts, late native creation,
stale-controller rejection, stopped-rootfs capture, transport loss and durable
deadlines for active/completed processes. Runtime unit checks cover uncertain
cleanup, identity mismatches, dependency ordering and completion/expiry races.
The automatic controller tests additionally exercise cancellation and worker loss
in each phase, deadline races, rejected capture, unavailable evidence storage,
checker failure versus an incorrect answer, and takeover without native resume.

`test_benchmark_observation.py` checks that native read waits release their locks
and pool slots, cancellation returns a single pooled connection safely, and old
generations or finished trials cannot accept late native observations.

`test_evaluation_streams.py` checks independent execution/evaluator receipts,
encrypted event journals, duplicate/gap/conflict handling, sealed streams and late
responses after takeover. It also exercises an SDK-only worker through the
evaluator channel and database rejection of cross-context event references.
The `bg09` migration is checked by downgrading to `bg01` and upgrading again.

`test_benchmark_broker.py` checks dispatch across sessions, global command identity,
pure receipt readback, actor/workspace scoping, immutable routes/outcomes and
dependency enforcement on PostgreSQL. Alembic `bg04` adds the global identity and
outcome guards; its upgrade/downgrade roundtrip is checked with the same database.

`test_benchmark_worker_host_native.py` runs a fresh SDK-only worker image with no
backend/Docker modules or daemon socket. It uses the real SQL broker to allocate
a separate native task, transfer private input references, run bounded native
commands, preserve a background service and export the result. The worker's
commands/events pass through the durable channel. Its task remains alive through
capture and worker shutdown, then all leases and the host reservation are released.
Automatic variants run the complete phase driver, cancel an active native command,
and recover both a prepared environment and an exec whose controller is lost.
The native checker observes the completed preparation service still alive during
warm capture. Cold recovery retains files privately without starting another worker.
The base fixture uses a local execution stub and an in-memory artifact sink.
Evidence and regrade variants use isolated MinIO with the production evidence
store. None of these fixtures proves real benchmark acceptance.

The fence follows PostgreSQL's
[session advisory lock semantics](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS).
Private storage follows the cryptography project's
[AEAD API](https://cryptography.io/en/latest/hazmat/primitives/aead/).
