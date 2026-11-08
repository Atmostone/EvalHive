# ADR: independent benchmark packages and environments

Status: accepted architecture; implementation tracked by
[Epic G, SPA-137](https://linear.app/spawnhive/issue/SPA-137).
Engineering evidence and unavailable variants are listed in the
[acceptance inventory](benchmark-sdk-validation.md).

## Decision

Benchmark packages own case discovery, native preparation, interaction, checking
and aggregation. Harness packages own their agent loop. EvalHive coordinates
attempts and owns infrastructure, evidence, authorization and storage. Neither
benchmark nor harness implementations are imported into the backend.

The Benchmark SDK uses a canonical schema, Python/TypeScript libraries and an
isolated JSONL worker. It preserves native formats instead of reducing cases to
input/answer rows. The installed manifest advertises configuration, credentials,
requirements and capabilities. The worker obtains scoped resources through a
broker; it cannot select arbitrary host paths or grant itself privileges.

## Environment composition

For terminal and coding tasks the native agent tools must run in the task's
environment. Sharing a workspace with a harness sidecar would leave shell
commands in the wrong root filesystem. The `in_environment` mode therefore uses
an owned exec in the prepared task container. A pinned read-only harness runtime
bundle supplies Node, packages and the native CLI, avoiding a separate image for
each harness/benchmark pair. The bundle must not overwrite task dependencies or
hide initial workspace contents. Initial compatibility is Linux amd64/glibc,
validated before a model request.

`connector_container` remains available for environments reached through MCP or
HTTP. It is an explicit interaction choice and cannot silently replace a native
terminal environment. Future VM and remote providers implement the same resource
boundary; their absence is an incompatibility, not an invitation to emulate them.

## Durable ownership and evidence

The current PostgreSQL storage and resource authority are detailed in
[Durable benchmark trials](benchmark-trials.md), including implemented behavior,
delivery barriers, automatic controller, cancellation and cold recovery.

`BenchmarkTrial` is one actual case/configuration/repeat attempt. It owns an
`EnvironmentSession`, harness `ExecutionAttempt` and one or more
`EvaluationAttempt` records. The matrix cell remains a selection/current-pointer
over immutable trial history. Native completion is separate from benchmark
correctness, checker status, recording completeness and cleanup outcome.

Lifecycle is resolve, prepare, ready, execute, capture, evaluate, persist, cleanup.
Resources remain alive for native grading. Claims and receipts use owner
generation and idempotency identities; a stale worker cannot change a new trial
or release its resources. A timed-out mutation has an uncertain outcome until
reconciled. A retry starts a new trial; regrade uses existing evidence only when
the adapter can prove the retained state is sufficient.

Public task inputs, user-visible artifacts, private native state and evaluator
assets are separate. Hidden tests and user-simulator context never become agent
files or instructions. Models for agent, user, environment and judge have separate
bindings, usage and budgets. Unknown measurements remain unknown. Native scores
and aggregators remain authoritative for their own dataset revision.

## Conversations

The Harness SDK gains a versioned persistent conversation and external tool-step
capability. Finishing one assistant response keeps the session alive. Typed input
IDs and tool-call batches correlate replies without duplicating generation.
Session close ends the execution attempt; cancellation works while generating or
waiting. Existing single-turn Work behavior remains compatible.

For tau2 the native orchestrator owns agent/user/environment scheduling and native
tool effects. Pi/OpenCode retain generation and their native loop, pausing external
tools until the driver returns results. Builtins bypassing that boundary are
disabled. Native/exposed/canonical tool names and schema hashes are recorded.
This is an explicit adapted interface, not a claim of byte-identical baseline
prompts. A dead native worker is not transparently resumed without a proven
capability. Multi-tool grouping and deadlock freedom are early acceptance gates.

## Initial native integrations

- Harbor 0.25.0 and Terminal-Bench 2.0: native package parsing, task environment,
  service groups, shared/separate verifier and named rewards.
- Tau2 v1.0.1: English airline/retail/telecom with the native orchestrator, user
  simulator and evaluator. Later tau versions are distinct conditions.
- MCPMark Verified: all declared filesystem, PostgreSQL, GitHub, Notion,
  Playwright and Playwright-WebArena variants, standard/easy.
- AppWorld's pinned native MCP implementation and compatible data 0.2.0: isolated
  world, explicit save before grading and native TGC/SGC. Engineering debugging
  uses train/dev.
- SWE-bench 5.0.2 classic: Verified by default, Lite and Full; agent input is the
  base repository/problem, evaluation owns gold/test patches. Native evaluation
  run IDs are unique to prevent cached-verdict reuse.

Existing YAML cases are served by `evalhive-cases`. Toolathlon-GYM uses a
separate adapter retaining reset, launch time, portal readiness and native checks.
The official Toolathlon and its GYM derivative keep distinct identities. Historical
records remain readable; old floating images do not imply exact replay.

The `evalhive-cases` package includes an independent legacy format reader,
byte-pinned dataset installer and JSONL lifecycle worker. Its Docker runtime has
no backend or Docker client dependency. The owned task environment and Harness
session are supplied through the common host APIs. Final text can be checkpointed
for grading without replaying the agent; native exact/fuzzy reference matching
is an explicit profile choice and other quality evaluation remains deferred.
See [package installation and evaluation](../benchmarks/evalhive-cases/README.md).
Package presence does not switch existing experiment scheduling or install
production datasets. `RootlessCoordinator` composes a registered benchmark,
provider, evidence store and selected Harness SDK runtime without native adapter
imports. It also creates fresh evaluation-only controllers from retained source
installations. The recorded Harness limits and image come from the admitted Task,
and private credentials use a separate host-only grant. Product Work and native matrices share this coordinator. New file-store and
Toolathlon launches require registered SDK selections; legacy core execution is
retired while historical schemas, checker logs and result readers remain
available. See [Case Store compatibility](benchmarks.md) for migration errors
and explicit CLI profile selections.

## Delivery and operational constraints

The current VPS is the only execution host in this epic. Native trials are
serial and retain the existing rootless runtime limits. Admission checks total
resources and disk; task requirements are never silently reduced.

Shutdown gives a connector up to `BENCHMARK_STOP_GRACE_SECONDS` (default 10,
maximum 60) to acknowledge a graceful stop before forced termination. This
host setting does not extend native task, model or grading deadlines.

Migration `bg16` validates compatible built-in profiles against frozen release
schemas and advances their pins to Pi connector 0.2.1 / OpenCode connector 0.2.2.
It records the old and new versions without changing settings, model/secret
references, template snapshots or execution history. Unknown versions and invalid
settings abort the transaction with a redacted conversion error. Downgrade
restores unchanged profiles; edited profiles require the paired database backup
instead of being overwritten. These are connector versions; upstream Pi 1.5.0
and OpenCode 1.19.5 remain unchanged.

Full adapter code and actual live verification are separate acceptance dimensions. Variants
needing unavailable accounts or larger environments remain clearly unavailable
or unverified. No owner service configuration is reused or modified implicitly.

Settings receives schema-based benchmark profiles. Experiments selects pinned
datasets/cases and harness profiles with preflight. Standalone trials use the
same coordinator and current Work. UI exposes preparation, execution, grading,
cleanup, native results and availability reasons; it does not install code.

Future BrowserGym, OSWorld, TheAgentCompany and MCP-Bench adapters use the same
capability/provider boundaries. GUI/VM, voice, SWE multimodal/multilingual/pro,
full scientific runs and a new chat product are outside this delivery.

## Implementation sequence

| Work | Linear | Deliverable |
| --- | --- | --- |
| B01 | SPA-138 | Contract, language SDKs, fixtures and source lock |
| B02 | SPA-139 | Installed catalog, profiles, revisions and preflight |
| B03 | SPA-140 | Providers, owned exec, readiness and runtime bundles |
| B04 | SPA-141 | Trial coordinator, scoped journals and recovery |
| B05 | SPA-142 | Evaluation, evidence and role usage |
| B06 | SPA-143 | Conversation and external-tool Harness SDK contract |
| B07 | SPA-144 | Native Pi/OpenCode dialogue integration |
| B08 | SPA-145 | Case Store and Toolathlon migration |
| B09 | SPA-146 | Harbor/Terminal-Bench adapter |
| B10 | SPA-147 | Tau2 adapter |
| B11 | SPA-148 | MCPMark adapter |
| B12 | SPA-149 | AppWorld adapter |
| B13 | SPA-150 | SWE-bench adapter |
| B14 | SPA-151 | API and UI |
| B15 | SPA-152 | Reproduction, history and aggregation |
| B16 | SPA-153 | Conformance and native acceptance |
| B17 | SPA-154 | Cutover, backup/restore, release and rollback |

Each task records implemented behavior and actual tests in Linear. Repository
documentation/code/commits use English; project discussion uses Russian. Local
commits have an SPA prefix and no coauthor. Verified commits are published to
`codex/benchmark-sdk`; the completed Harness SDK is already merged into `master`.

## Catalog and configuration API

`GET /api/benchmarks` lists installed manifest envelopes with independent
installed/configured/compatible/live_verified fields. Unknown profile-specific
values remain null. Existing `/api/benchmarks/suites` endpoints remain compatible.
`/api/benchmark-profiles` provides workspace-scoped CRUD and schema validation;
`/api/benchmark-secrets` is write-only for credential values. Profile model
bindings refer to workspace model IDs by role. Configuration presence does not
prove remote authentication or live acceptance.

`POST /api/benchmark-datasets` registers an already installed server receipt for
a workspace; it cannot install executable code or accept an arbitrary path.
`GET /api/benchmark-datasets`, individual revision reads and paginated `/cases`
return public projections. Native descriptors and provenance remain internal.
Re-registration preserves identity and rejects changed content. Database triggers
protect immutable revision/case rows. Migration `bg01` introduces this storage.

`POST /api/benchmarks/preflight` binds a benchmark profile, registered revision,
case and harness profile from the same workspace. It preserves native capability,
platform and resource requirements, checks configured references and reports
incompatibility reasons. The initial `static` stage always reports
`runtime_admission: pending` and `can_run: false`. B03 adds authoritative provider
and resource checks; B04 reserves and revalidates before launching a trial.
Static policy capacity alone must never authorize execution.

Settings now supports benchmark profiles, connector-provided configuration
schemas, write-only credentials, role-specific model references, registration of
already installed dataset revisions and paginated case browsing. A compatibility
selector binds profile, revision, case and harness without installing packages or
running a model. Installed, configured, compatible and live-verified states remain
separate. The isolated browser check is
`frontend/browser-tests/benchmark-settings.py`; it serves a production build and
intercepts API requests with synthetic workspace data.

The workspace-scoped `/api/benchmark-trials` read API lists trials, exposes their
independent execution/evaluation/recording/cleanup states and returns public
snapshots. `/{id}/events` reads public journal entries strictly after `after_seq`
with a bounded page size; its `next_seq` is the last returned cursor.
`/{id}/evaluations` preserves each evaluator's status, native metrics and verdict.
`/{id}/artifacts` lists only user/agent-visible receipts; it never returns private
storage locators or evaluator-only files. `/{id}/usage` reads the existing role
ledger. Operator-only `POST /{id}/cancel` records a monotonic cancellation request
and is safe to repeat. These reads do not themselves authorize a new execution;
product queue and launch composition remain part of B14.

Migration `bg13` adds durable `BenchmarkJob` launch requests. Workspace/request
identity is unique, request parameters and encrypted launch inputs are immutable,
cancellation is monotonic and terminal queue receipts cannot be rewritten.
`app.benchmarks.jobs.enqueue` joins the caller's transaction so task, trial and
request creation can commit together. An identical request identity preserves
the original frozen inputs even if a provider/profile is later edited.
`JobStore` requires the current trial/evaluation controller before returning
private inputs or changing execution state. Its terminal receipt requires a
finished owned lifecycle; queue state never authorizes resource-broker effects.
These storage primitives are separate from the product launch/dispatch wiring.

An installed connector can register distinct runtime packages for its dataset
revisions. The connector root retains its single `manifest.json` and the complete
`datasets/` receipt catalog. An optional `installations.json` contains
`format_version: "1.0"` and an `installations` object mapping SDK
`dataset_key(dataset_id, revision)` values to contained relative package
directories. Each selected package contains its own matching manifest and dataset
receipt, plus its `resources.json` and `files.json`. This supports different
environment types through one connector without benchmark-name branches.
Standalone packages without an index retain their direct layout.

`app.benchmarks.installations.resolve_package` checks the registered receipt,
worker image, package containment and matching metadata without importing a
worker. Missing runtime registration does not remove cases from the metadata
catalog. A metadata digest binds the selected resource/file declarations for
launch composition; resource compilation and native file verification still run
before model access. Only administrator-installed files can populate this index.

Product benchmark dispatch is explicitly controlled by
`BENCHMARK_RUNTIME_ENABLED` (disabled by default). Static host capabilities reflect
that choice, resource-finalizer support and the configured container capability
allowlist. The built-in `evalhive.rootless` provider descriptor represents trusted
server code; its `builtin:` entrypoint/image markers are not worker images.
`BENCHMARK_GROUP_MEMORY_MB` and `BENCHMARK_GROUP_CPU_CORES` bound the whole native
group independently of existing per-container runtime limits. Static disk
capacity retains the existing 2 GiB host reserve. Actual admission still measures
and reserves the complete group before model access, with the existing 512 MiB
memory reserve and serial execution policy. A static compatibility result is
never a resource reservation.

The neutral rootless runtime now preserves image startup/workspace semantics and
checks workspace/trial/session/generation ownership for containers, networks and
native execs. Its static supervisor and SDK process transport retain task state
after native completion, stop owned descendants independently of task services,
and detect loss of safe grading state. See [the runtime contract](../environments/runtime/README.md).
Harness manifests now register a separate runtime bundle image. The canonical
receipt, bounded installer and no-model compatibility checks preserve native task
dependencies. Pi/OpenCode bundles execute shell tools in distinct task images;
unsupported libc fails before model access. These primitives are not yet a public
launch API: B14 must expose the installed provider, durable broker and controller
through product launch authorization and queue composition.

The built-in rootless provider now consumes scoped resource requests over the same
SDK reverse channel. Workers supply admitted plan IDs/opaque handles; registered
assets and private input refs become host grants. Provider output streams are
bounded, native task state survives completion, worker transports are isolated,
and transport-triggered stops recheck generation authority. A real standalone SDK
worker image exercises this boundary without backend modules or Docker access.
Private service DNS and unsafe-supervisor quiescence are also checked natively.
The provider requires a coordinator authority; its explicit in-memory test fixture
is not a production reservation or recovery implementation. B04 supplies the
PostgreSQL journal, leases, fencing, automatic lifecycle and recovery; B14 product
launch wiring remains required.

Native connector profiles for conversation support use version 0.2.0, with Pi
1.5.0 and OpenCode 1.19.5 unchanged. B17 must migrate installed 0.1 profile
references during cutover while preserving historical snapshots and image digests.

## Retained checkpoint inputs

An evaluation-purpose rootless plan can select checkpoint artifacts through
`resource.config.retained_inputs`, an array of artifact IDs. The host grants only
IDs present in the frozen checkpoint. Each selected artifact is mounted read-only
at `/evalhive/retained/<artifact-id>/payload`; configuration cannot choose a host
path or replace another mount. The connector restores its native world inside
its new task container. EvalHive does not extract benchmark archives on the host.

Complete group admission includes retained file storage, one sequential download
spool, and per-file filesystem headroom. This auxiliary storage is also charged
against trial disk limits, and cannot satisfy native environment disk minima.
Downloads use the admitted data volume and verify the full immutable reference,
size and SHA-256 before publication. Cancellation waits for bounded blocking I/O
to return before releasing the ownership fence. Evidence lookup reuses the same
SQL connection when that fence already owns a pool slot.
Creating an evaluation and resolving its source installation authorize artifact
references without downloading bodies. Full materialization verifies every
artifact declared by the checkpoint, including artifacts that the worker does
not select for a mount, before installing the new environment session.

Migration `bg11` records each private materialization intent before local writes.
Its immutable identity binds workspace, source trial, evaluation controller,
origin generation, session and artifact. Files progress from `reserved` to `ready`
to `released`; a replacement owner cleans the predecessor's files rather than
restarting native work. Cleanup removes these files only after every resource
lease in the evaluation context has been released. Pending files retain the host
reservation and prevent terminal cleanup. Storage mapping changes fail closed.
Downgrading below `bg11` is rejected while unreleased file intents remain, so a
schema rollback cannot erase the information required to reclaim their storage.

The engineering fixture transfers a world archive larger than 32 MiB through
MinIO, restores it in a fresh rootless container, checks the read-only mount, and
grades it through a new worker and evaluation attempt without calling `drive`.
The source execution and verdict remain unchanged. This verifies the generic
restoration path, not native compatibility of the planned benchmark adapters.

The worker session retains its complete manifest, registered asset catalog,
actual worker image ID and private static-file proofs. The source installation
loader verifies saved session identities, plan digests, catalog consistency and
the actual source process declaration. It reconstructs assets from these records
without consulting the current connector catalog. Fresh evaluation workers use
the retained image ID even when the source manifest's mutable tag has moved.

Static replay grants require administrator-installed read-only roots. Their
content proof covers names, types, modes and file bytes, with an aggregate 2 GiB /
65,536-entry bound and shared-path deduplication. The loader checks their content
again before launching a worker. Changed, missing or unretained static inputs
make exact replay unavailable; old writable session paths never become new
authority. Native mutable world state still comes from the checkpoint archive.
Historical records lacking a full installation snapshot remain readable and
report exact replay as unavailable.
