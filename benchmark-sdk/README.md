# Benchmark SDK 1.0

This package defines the boundary between EvalHive and administrator-installed
benchmark or environment-provider processes. A benchmark owns native cases,
preparation, interaction, evaluation and aggregation. EvalHive owns authorization,
resource admission, process supervision, durable receipts, evidence and storage.
The SDK imports neither the backend, Docker nor an LLM client.

The canonical contract is
`python/src/evalhive_benchmark/schemas/contract.json` (JSON Schema 2020-12).
Both language packages validate the same definitions and conformance fixtures.
The TypeScript build copies the canonical file into its published package.
The architecture and implementation sequence are in
[the architecture decision](../docs/benchmark-sdk.md).

## Environment-provider packages

Providers use the same framing, receipts and owner scope as benchmark workers,
with a manifest of kind `environment_provider`. Python packages implement
`EnvironmentProvider.handle(request, context)` and `serve_provider_stdio`;
TypeScript packages implement `EnvironmentProvider` and `serveProviderStdio`.
The validated resource request includes provider identity, operation, payload,
optional handle, stable request ID and the complete trial generation scope.
Native idempotency keys must include the scope as well as the request identity.

`ConnectorClient.resource` sends `allocate`, `exec`, `inspect`, `stop`, `export`
or `release`. The provider advertises each supported operation explicitly.
`describe`, scoped `cancel` and `shutdown` remain control commands. Cancellation
rejects new allocation/execution while allowing inspection, capture and cleanup.
A provider process binds to one trial generation and rejects another provider
identity or scope. The host still enforces durable leases and authority; this
process-local check does not grant access to the host or recover native effects.

The transport accepts both command and resource request directions only when the
provider wrapper explicitly enables them. Ordinary benchmark workers retain their
original incoming direction. Receipts share bounded pending/active/unacknowledged
limits; acknowledgement retires a payload only after durable host persistence.
Native effects after a worker crash must be reconciled from native idempotency
keys or resource labels before retrying an uncertain mutation. Providers must
validate their own resource payloads and handles; the broker must never forward
arbitrary host paths or privileged runtime access from a benchmark worker.

The fake package includes provider examples for both languages. Their in-memory
resources test the protocol and do not claim a real Docker/VM implementation.

## Public and private information

A `CaseDescriptor` contains a dataset revision, native case reference, source
digest, public metadata and requirements. It is an envelope around native data,
not a replacement for native task formats. It must not contain hidden answers,
private user context or evaluation implementation. A `TrialSpec` identifies the
workspace, trial and owner generation, public configuration, model bindings,
private credential references and resource limits.

Credentials are resolved outside this protocol. A configuration schema describes
public settings; `secret_schema` describes separately supplied credentials. A model
binding names a private reference. URLs cannot carry credentials, query strings or
fragments. A connector must redact its native errors, events and diagnostics.
Generic dispatch errors omit exception text; schema errors omit rejected values.

## Lifecycle

| Command | Parameters | Validated result |
| --- | --- | --- |
| `describe` | `{}` | Installed manifest |
| `list_cases` | Dataset ID, revision, optional cursor/limit | Case page with next cursor |
| `resolve_case` | Native case reference | Immutable case descriptor |
| `validate` | `spec` | Accepted public effective configuration |
| `plan` | `spec` | Environment plan and required capabilities |
| `prepare` | `spec`, `plan` | Explicit readiness, session handle and purpose-specific access |
| `drive` | `scope`, connector-owned `payload` | Interaction-controller receipt |
| `capture` | `scope`, connector-owned `payload` | Evidence/checkpoint references |
| `evaluate` | `scope`, connector-owned `payload` | Structured evaluation result |
| `aggregate` | Connector-owned `payload` | Named metrics and coverage |
| `cleanup` | `scope`, connector-owned `payload` | Cleanup receipt |
| `cancel` | `scope`, connector-owned `payload` | Cancellation acknowledgement |
| `shutdown` | Optional reason | Process shutdown acknowledgement |

The last commands use bounded connector-owned payloads because native state,
artifact locations and aggregation inputs differ between benchmarks. Typed
platform objects remain canonical. A native state object must use private opaque
references instead of secret values. A host must not execute a path or command
merely because it appeared in a native result.

Discovery and preflight can run without a trial. Once a mutating lifecycle command
binds a process to a scope, that process serves exactly one trial generation.
Other scopes fail with `SCOPE_CONFLICT`. This is defense in depth, not workspace
authorization or durable fencing; the coordinator and resource broker enforce
those independently. A replacement generation starts a new process.

An environment plan declares provider resources, dependencies, agent access and
output visibility. IDs are unique, dependencies are acyclic, and output resources
must exist. `kind` is an extensible provider-specific identity, so adding a VM or
another resource family does not require a benchmark schema change. Provider
configuration is validated by the selected installed provider. A declaration
does not grant privileges or access to arbitrary host paths.

Each output can declare `stage: capture` (the default) or `stage: evaluation`.
Required capture outputs must be retained before grading. Required evaluation
outputs are produced by the checker and must be retained before its completed
verdict is accepted; the final check includes both stages. The host chooses the
validation stage. A worker cannot defer an installed capture requirement by
changing its plan: stage must match the provider's registered output policy.
The evaluator returns its retained output references in `evaluation_result.artifacts`.

`required` describes evidence needed to confirm grading, not a task's expected
successful deliverable. If an absent answer file is a legitimate incorrect
solution, the native evaluator must judge that condition; it must not be turned
into a host storage failure by declaring the answer file required. A mandatory
state archive can instead preserve the filesystem, including absent files. Use
different output IDs when a native path must be captured both before and after
grading: an already published identity cannot replace its original bytes.

An evaluation result separates `completed`, `error`, `unavailable` and `cancelled`
from nullable correctness. Error/cancel/unavailable cannot claim a successful or
failed solution. Metrics retain names, units, direction and unknown values. The
native result, artifact hashes and completeness accompany the metrics. Regrade
must create a new evaluation and can run only when sufficient state was retained.
No SDK success receipt implies the agent solved a task.

Native replay requires `capabilities.regrade: native` and a typed
`regrade_checkpoint` in `capture.checkpoint`. The checkpoint identifies its
connector/version, exact case/revision/source digest, native state schema,
completeness, retained artifacts and evaluator model roles. `state` remains
connector-owned private data. Artifact references must belong to the same
capture. Agent and orchestrator roles are forbidden in the evaluator role list.
The connector attests that this state is semantically sufficient; the host
checks provenance and retained bytes. A string such as `regrade: available` is
not a checkpoint. This prerequisite does not itself start replay or authorize
reopening an execution; a new evaluator controller still needs resource admission
and restoration of the retained state.

An explicit `TrialSpec.purpose: evaluation` carries its typed `checkpoint` and
requires native regrade support. The case, optional split, source digest and
connector version must match. Its model roles exactly match `evaluator_roles`;
credential references are limited to those model bindings. Agent/orchestrator
models and unrelated private references are rejected. The SDK checks the contract;
the host must still verify retained bytes, authorization and native sufficiency.
The spec contains private checkpoint state and must never become an agent input
or a public configuration response.

Evaluation plans and prepared results declare `purpose: evaluation` and omit
`agent` entirely. `validate_environment` / `validateEnvironment` checks their
purpose against the spec. The connector wrapper rejects a different result
purpose, prevents changing purpose within a worker and rejects `drive` for an
evaluation worker. `prepare` restores connector-owned checkpoint state in new
resources; an old released handle grants no authority. Absent purpose remains
`execution` for compatibility and requires agent access in plans/readiness.
Execution specs cannot carry a replay checkpoint. This contract does not by
itself implement a durable restore/evaluate coordinator or materialize artifacts.

## Transport and resource broker

Standard input/output carry UTF-8 JSONL only. Diagnostics use standard error.
Records use protocol `1.0`, contain at most 1 MiB of JSON and end in LF; CRLF is
accepted. Nesting is limited to 64, non-finite/unsafe numbers and malformed Unicode
are rejected. Incompatible versions fail explicitly. Configuration schemas use
local references only; validation never downloads a schema.

Commands have stable IDs; responses repeat the ID with `ok/result` or `error`.
Events and responses can interleave. A connector may send a scoped
`broker_request` while its host command is pending; the host returns a
`broker_response`. The broker operations are allocate, exec, inspect, stop, export
and release. The payload and provider schema define the particular resource.
The host grants only resources and handles belonging to that scope.

This reverse channel lets preparation await readiness and lets a native interaction
controller await an agent step without deadlocking the command reader. Provider
implementations use the same worker contract, with `kind=environment_provider`;
they do not receive direct access to the platform's Docker socket.

Per-process defaults bound pending messages to 64, active operations to 16,
unacknowledged receipts to 256 and receipt tombstones to 16,384. Events have a
separate bounded host queue (128). Consumers must drain events concurrently.
Exhaustion fails explicitly instead of silently dropping evidence or rerunning an
old command. Limits can be configured with positive integers.

Identical request IDs/content reuse an in-flight or completed receipt. Different
content under the same ID fails with `COMMAND_CONFLICT`. After durably storing a
receipt, the caller may send `receipt_ack` for its request type and ID. The peer
releases the response payload but keeps a tombstone. A later duplicate gets
`RECEIPT_RETIRED`: the caller must use its durable receipt, never rerun the effect.
Acknowledgements are idempotent. Unknown or premature acknowledgements fail.

A timeout is `COMMAND_UNCERTAIN`, not proof that execution failed. The SDK's
receipt ledger is process-local. The coordinator must store operations before
dispatch, persist receipts before acknowledging them, and reconcile an uncertain
operation after a process loss. This library does not promise exactly-once native
side effects, durable event delivery, automatic recovery or native session resume.

Durable hosts use `request_receipt` (Python) / `requestReceipt` (TypeScript) to
obtain the full success/error wire response. Transport failures still raise;
remote error replies remain data. Store the envelope before validating its method
result, completing an event barrier or sending ACK. The monotonic
`events_received` / `eventsReceived` counter snapshots how many earlier frames
entered the client queue; it does not claim those frames were persisted.

Worker broker calls may opt into `durable_host=True` / `durableHost: true` only
when the host commits successful receipts before replying and supports durable
readback. Successful calls then ACK their broker receipt, keeping response payloads
bounded through long loops. Failed calls retain their normal receipt. Configure
the host client's `broker_replay` / `brokerReplay` with a pure receipt reader;
the underlying peer's `replay_handler` / `replayHandler` uses it for acknowledged
tombstones after checking request identity/content. It must never alias a dispatch
handler or rerun an effect. A missing durable receipt remains an explicit uncertain
outcome. The default peer still returns `RECEIPT_RETIRED` without such a reader.

Events carry trial scope, source, a monotonically increasing sequence and an ID.
Each source process sequences events independently; the coordinator maintains the
durable combined journal. `observed_at` is optional until the runner observes the
event. Native payloads and control messages are bounded. Larger evidence belongs
in hashed storage artifacts, not protocol lines. Usage records distinguish model,
role, purpose and measurement identity; omitted measurements remain unknown.

## Connector example

`examples/fake` is a self-contained external package, using only the SDK. Its
Python and JavaScript entrypoints implement the same case and lifecycle. During
preparation it asks an example provider for an environment and emits readiness;
it does not create real containers or call a model. Replace those fixture-native
operations in a real package while retaining the lifecycle boundary.

Python implementations subclass `Connector`, provide `manifest` and async
`handle(method, params, context)`, optionally `close(context)`, and invoke
`serve_stdio`. TypeScript implementations provide the equivalent object to
`serveStdio`. Context supplies event emission, cancellation and the reverse
resource-broker channel. Optional operations require declared capabilities.

## Host agent sessions

An execution-purpose connector can call `context.agent(scope, operation, payload,
...)` (Python) or `context.agent(scope, operation, payload, options)` (TypeScript).
These reverse `agent.call` commands share the JSONL transport with resource
requests. Evaluation-purpose workers cannot call agents. Packages using this API
must declare `host.agent_api_v1` in manifest or case `requirements.capabilities`; host preflight
must reject that requirement unless its agent gateway is available.

| Operation | Session required | Payload | Result schema |
| --- | --- | --- | --- |
| `run` | No | Empty object | `agent_completion` |
| `open` | No | Optional `initial_history` | `agent_session` |
| `control` | Yes | `name` and `payload` | `agent_control_result` |
| `events` | Yes | `cursor`, optional `limit` and `wait_seconds` | `agent_events` |
| `close` | Yes | Optional `reason` | `agent_completion` |

Session IDs are opaque host grants. Harness/model selection and launch settings
come from the server's frozen profile, not from an agent request. A completion
contains `session_id`, terminal `status` and an `outcome` reference with
`attempt_id` and `evidence_digest`. Returning from a benchmark's `drive` command
does not prove successful agent execution: the host must resolve the reference
against its committed Harness SDK attempt and execution binding.

`ConnectorClient` accepts optional `agent` and `agent_replay` callbacks (Python),
or `agent` and `agentReplay` callbacks (TypeScript). The gateway must authorize
workspace/trial/generation, worker identity, phase, session and operation. It must
commit successful call receipts before replying; the replay callback only reads
those receipts. The SDK validates replies before acknowledging them. Missing
retired receipts produce an uncertainty error without repeating an agent effect.
Event pages must preserve their session, cursor and requested limit. Cancellation
interrupts a worker waiting for an agent response.
`run` and `close` can wait for terminal execution; use the remaining trial budget
as the request timeout (Python seconds, TypeScript milliseconds).

The SDK supplies routing, framing and validation. Process launch, credentials,
durable agent lifecycle and controls belong to the host runner integration.

The operator installs the worker image/package and manifest. The backend loads
the declaration, not the worker module. UI executable-code installation is outside
this contract. A worker must cooperate with cancellation; the supervising runtime
still imposes a deadline and bounded process-tree termination.

## Source and installation locks

`sources.lock.json` records immutable upstream commits, tree identities and hashes
of inspected contract files. Published Harbor/SWE-bench distributions additionally
carry the registry-provided SHA-256. The SWE-bench source revision documents the
inspected interface; the runtime release is the separately pinned distribution,
not an invented Git tag for that release.

Source locks do not mean data or images are installed. An installation must
record actual downloaded dataset content hashes, resolved image digests and
platform compatibility before a trial can prepare. Mutable tags are never run
as frozen conditions. Unavailable accounts/resources and unverified service
variants remain explicit in the installation and acceptance inventory.

## Conformance

Use Python 3.12 and Node 22.19 or later. In an isolated Python environment:

```bash
python -m pip install -e benchmark-sdk/python pytest build
npm ci --prefix benchmark-sdk/typescript
npm run build --prefix benchmark-sdk/typescript
npm ci --prefix benchmark-sdk/examples/fake
python -m pytest -c /dev/null benchmark-sdk/python/tests
EH_TEST_PYTHON="$(command -v python)" npm test --prefix benchmark-sdk/typescript
python -m build benchmark-sdk/python --outdir /tmp/evalhive-benchmark-packages
npm pack ./benchmark-sdk/typescript --pack-destination /tmp/evalhive-benchmark-packages
```

Both host libraries communicate with both real worker processes. Tests cover
native-free lifecycle, broker RPC, cancellation, receipts/conflicts, scope binding,
unsafe JSON, framing, resource dependencies and unknown measurements. These SDK
checks do not establish that a real benchmark adapter or environment is installed;
those require separate native and platform acceptance.
