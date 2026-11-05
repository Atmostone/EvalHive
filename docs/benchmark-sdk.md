# ADR: independent benchmark packages and environments

Status: accepted architecture; implementation tracked by
[Epic G, SPA-137](https://linear.app/spawnhive/issue/SPA-137).

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

Existing YAML cases are served by `evalhive-cases`. Toolathlon-GYM becomes a
separate adapter retaining reset, launch time, portal readiness and native checks.
The official Toolathlon and its GYM derivative keep distinct identities. Historical
records remain readable; old floating images do not imply exact replay.

## Delivery and operational constraints

The current VPS is the only execution host in this epic. Native trials are
serial and retain the existing rootless runtime limits. Admission checks total
resources and disk; task requirements are never silently reduced. Full adapter
code and actual live verification are separate acceptance dimensions. Variants
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
commits have an SPA prefix and no coauthor. Push remains deferred by the owner.
