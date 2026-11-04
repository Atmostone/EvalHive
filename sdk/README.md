# Harness SDK 1.0

EvalHive owns the task, execution attempt, environment and results. A connector owns
the harness process, native configuration and event translation. These packages do
not import the backend, Docker or an LLM client.

The canonical contract is
[`contract.json`](python/src/evalhive_harness/schemas/contract.json), JSON Schema
2020-12. The Python wheel includes it; the TypeScript build copies the same file
into the npm package. Runtime validation in both languages uses these definitions.
[`fixtures/conformance.json`](fixtures/conformance.json) contains shared positive
and negative records, including interleaved calls and unknown usage.

## Process and transport

The host starts the manifest's argv entrypoint **without a shell**, with a working
directory and environment chosen by the runtime. Standard input/output carry UTF-8
JSONL only; diagnostics use standard error. One process executes one attempt.

Records end in LF (CRLF is accepted), contain at most 1 MiB before the LF, and use
protocol version `1.0`. Unknown versions fail with `UNSUPPORTED_PROTOCOL`, never
silently fall back. JSON nesting is limited to 64; non-finite numbers, values above
the JavaScript safe integer range, malformed Unicode and language-specific values
are rejected. Unicode U+2028/U+2029 are text, not record delimiters. Configuration
schemas use the same dialect, without network resolution or format assertions;
do not depend on vendor-specific JSON Schema extensions or regex features.

Every command has an `id`; its response repeats that ID and contains either
`ok: true, result` or `ok: false, error: {code, message}`. Invalid frames for which
an ID cannot safely be read receive an error with `id: null`. Responses and events
can interleave. Hosts correlate responses by ID, independently of arrival order.

| Command | Parameters | Successful result |
| --- | --- | --- |
| `describe` | `{}` | Connector manifest |
| `validate` | `{run: RunSpec}` | Connector preflight result; default `{accepted: true, effective_config}` |
| `start` | `{run: RunSpec}` | `{accepted: true}` after schema/preflight checks; execution continues asynchronously |
| `control` | `{attempt_id, command, payload?}` | Connector acknowledgement; it does not assert that the run has finished |
| `shutdown` | `{reason?}` | `{stopped: true}` after the active attempt has stopped |

Connectors reject failed preflight by raising `ProtocolError`; returning a false
boolean is not rejection. The runner additionally enforces model availability and
runtime policy. Unsupported native parameters fail connector schema validation.
Controls require the active attempt, a declared native/emulated capability, and
validation against the command's `input_schema` when declared. No setting is
silently ignored. Shutdown and stdin EOF stop the active attempt. Python tasks
receive cancellation; TypeScript connectors must observe `context.signal` and
stop their native harness. The runner must impose a deadline and kill an
unresponsive process tree; the SDK does not claim to supervise container children.

The SDK emits `run.started` and exactly one `run.finished` on the normal transport
path. Connectors emit only harness events and return `{status, summary, error?}`;
`failed` requires an error. An exception produces a sanitized failure. Cancellation
is distinct from failure. Optional `error.details.normalized_failure` supplies
connector-neutral facts: `kind` (`model_error`, `limit`, `timeout`, `harness_error`,
`recording_error`, `agent_error`), nullable HTTP `status_code` and exception class.
The canonical schema validates these facts. Core consumers classify the facts;
native error-message parsing stays in the connector. Missing facts remain unknown.

Cancellation cannot turn into success merely because a connector returned successfully during
shutdown. A broken pipe can prevent a terminal event: the runner must record an
explicit transport failure. Late emits after a terminal record are rejected.

## Manifest, RunSpec and evidence

A manifest names the connector/version, protocol, entrypoint, image requirements,
configuration schema and capabilities (`native`, `emulated`, `unsupported`). Image
registration and pinning are host responsibilities; a manifest cannot grant itself
new Docker privileges. Native settings belong to `RunSpec.config` and the manifest
schema. Platform fields have strict shapes; connector configuration and resource
configuration remain extensible objects.

`RunSpec` identifies workspace/task/attempt and includes the task, cwd,
instructions, model connection, credential references, environment resources and
limits. References are names, never credential values. The host resolves secrets
privately for execution. A connector must redact its own native payloads and
diagnostics before emitting them. Schema validation error messages do not echo
the rejected values. Neither SDK writes configuration or events to disk itself.

Events have `event_id`, `attempt_id`, monotonically increasing `seq` starting at
zero, `source`, optional session/parent session IDs, a name and typed data:

- Messages use `message_id`; deltas and final snapshots are distinct. Channels
  distinguish text, reasoning and redaction. Missing reasoning stays missing.
- Tools use `call_id` within a session and optional `parent_call_id`; concurrent
  and nested calls do not rely on adjacent records. A missing end event remains
  missing rather than being invented as a successful result.
- Usage has stable `usage_id`, model, purpose and `delta`/`cumulative` mode. The
  same measurement's cumulative updates replace prior values, not add to them.
  Cache/reasoning fields can be null or absent. Unknown is never measured zero.
  Monetary amounts are decimal strings with currency and provenance.
- Artifacts announce paths; collection and path containment follow the task
  environment's policy, including failed/cancelled attempts.
- Interaction requests have IDs and kinds; supported responses use declared
  controls. Unsupported interactions must fail explicitly in the connector.
- Native events carry their name, `adapter_version` and redacted payload, so
  later readers can distinguish raw evidence from a particular normalization.

The SDK serializes event writes and assigns IDs/sequence numbers. It does **not**
implement durable delivery, server deduplication, token scoping, archive storage or
usage aggregation; those belong to the runner and attempt journal. Do not treat
an accepted command as evidence of a successful run.

Host clients bound pending requests (64) and queued events (128), configurable
with positive limits. Drain `events()` during execution. Overflow fails explicitly
with `EVENT_BUFFER_FULL`; events are never silently discarded. A local persistent
spool belongs in the runner. Request timeouts do not cancel the native operation;
late responses are ignored, and control/runner deadlines decide what to stop.

The optional event `observed_at` is a UTC ISO timestamp assigned by the external
runner when it receives the record. Backend receipt time is stored separately.
Delayed delivery preserves observation time; execution duration is also measured
with the runner's monotonic clock and is unknown without a terminal observation.

## Task resources

`RunSpec.environment.resources` is a bounded, validated list independent of the
connector configuration. Resource IDs are unique; `tools` and `memory` are
singletons. Both SDKs expose resource validation and private credential resolution.

| Kind | Configuration |
| --- | --- |
| `tools` | `names`: explicit allowlist; an absent resource selects connector defaults, while `[]` disables tools |
| `mcp` | `transport: stdio` with command/args/cwd, or `transport: http` with URL; optional timeout and native-tool-to-canonical aliases |
| `skill` | Explicit mounted skill path; no installation or implicit user-directory discovery |
| `memory` | `mode: off/flat/structured`; `off` forbids content, other modes carry the resolved context |
| `instructions` | Environment/workspace rules as explicit content |

MCP `credential_refs` map `env.NAME` or `headers.NAME` to private worker credential
IDs. The backend persists references, not environment/header values. Native
configuration is constructed inside the connector. Credentials never belong in
URLs, public settings, resource snapshots or artifacts. Connectors must reject
unavailable requested tools and resource failures before contacting a model.

Tool start events retain native `name` and optional canonical `aliases`. A reader
can preserve native identity while applying existing benchmark checks. The
environment also owns artifact paths and optional `workspace_seed: task`: the
runtime copies prepared inputs into the attempt workspace and excludes prior
attempts. Evaluation must use that same attempt workspace.

Manifests may register `requirements.environment_images`, mapping environment IDs
to compatible installed images. The runtime selects and freezes the registered
image without importing a harness implementation; environment dependencies remain
separate from native agent configuration.

## External connector example

[`examples/fake`](examples/fake) is a separate connector package with a manifest
and workers in both languages. It introduces a custom `prefix` setting without
changing the SDK or importing EvalHive. Install the SDK in the worker's image,
copy the worker and manifest, set the correct argv/image, and register the manifest
with the server catalog. H02/H04 add that catalog/runtime integration; this SDK
change alone does not switch existing production runs.

For Python, subclass `Connector`, implement `run(context)` and optionally
`validate(spec)`/`control(context, name, payload)`, then call
`asyncio.run(serve_stdio(connector))`. For TypeScript, pass the equivalent object
to `serveStdio(connector)`. Use `context.emit()` for normalized events and return
the terminal result. The fake example's artifact event is a transport fixture;
real connectors must produce the announced file in the environment's work tree.

## Build and verify

Use Python 3.12 and Node >=22.19.0. No provider credentials or model requests are
needed. From the repository root, in an activated Python environment:

```bash
python -m pip install -e sdk/python pytest ruff build
npm ci --prefix sdk/typescript
npm run build --prefix sdk/typescript
npm ci --prefix sdk/examples/fake
python -m pytest -c sdk/python/pyproject.toml sdk/python/tests
EH_TEST_PYTHON="$(command -v python)" npm test --prefix sdk/typescript
ruff check sdk/python sdk/fixtures/peer.py sdk/examples/fake/connector.py
python -m build sdk/python --outdir /tmp/evalhive-sdk-packages
npm pack ./sdk/typescript --pack-destination /tmp/evalhive-sdk-packages
```

Both host clients run both real worker processes. Tests additionally exercise
configuration/version rejection, malformed/unterminated frames, out-of-order and
late replies, bounded buffers, feedback, abort, EOF and sanitized errors. This is
SDK conformance; Pi/OpenCode integration and end-to-end platform validation are
tracked separately in SPA-127/SPA-129/SPA-133.
