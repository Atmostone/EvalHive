# OpenCode connector

This package implements the Harness SDK with OpenCode and its official
TypeScript SDK, both pinned to **1.19.5**. A private `opencode serve` process owns
the agent loop. The backend only discovers this package's JSON manifest and
registered image.

The worker uses the supported `session` HTTP endpoints and SSE. Some newer
`v2.session` operations are declared in this SDK release but are unfinished in
the server. Completion therefore requires an idle native session, a completed
assistant message belonging to the latest user input, and a stable control
generation. Saved native messages and parts restore normalized state after an
SSE reconnect. Native call/session IDs preserve interleaved tools and child
sessions; repeated snapshots do not duplicate normalized results. Missing stream
deltas cannot be reconstructed and are reported separately from final snapshots.

The common turn budget is checked by a connector-owned native request hook before
another provider call. It counts native assistant-message IDs across sessions;
provider retries for the same assistant turn do not consume another turn. Title,
summary and compaction calls remain separate purposes. A reached budget records
a limit failure. Native soft step hints are not used as a substitute for this bound.

Completed tool results remain immutable in normalized evidence. Native context
compaction may prune old outputs from later REST views; those views do not replace
an already captured result. A result first seen only after pruning stays missing.

An isolated HOME/XDG/config tree excludes user credentials, project configuration,
implicit instructions and external skills. The connector's local hooks append
transport observability and an exact native tool mask. The mask is applied to
native user messages independently of session permissions: OpenCode's deprecated
prompt `tools` parameter also rewrites permission rules and could otherwise
bypass a requested approval. Executable plugins are fixed by this package;
profiles cannot install them.

OpenCode prepares plugin dependencies for configuration directories even when
default plugins are disabled. The image installs the pinned plugin SDK and
provides its lockfile/dependency tree to each isolated configuration directory.
No dependency installation is needed during execution. Worker-only transitive
dependency overrides are recorded in the lockfile; the native CLI remains 1.19.5.

Model connections are explicit OpenAI-compatible endpoints with private credential
references. A local transport observer forwards the native requests and streams
without replacing the agent loop. It records provider field availability for
each request, model and purpose. SDK token defaults and catalog cost estimates
do not become measured usage or billed cost. Unknown values stay unknown.

Common MCP resources become native local/remote servers, with OAuth discovery
disabled for remote servers. Skills and instructions are explicit resources;
memory off omits memory content. Feedback is acknowledged after its user message
is visible in the native transcript. Abort reaches the native session; shutdown
then bounds termination of the server process group. Permission requests use
the declared `approval` control. Question input is unsupported and fails explicitly.

Build from the repository root, using the EvalHive rootless Docker endpoint:

```bash
docker build --target test -f harnesses/opencode/Dockerfile -t evalhive-opencode-tests:harness-dev .
docker run --rm --network none --memory 2g --cpus 1 --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges:true --init evalhive-opencode-tests:harness-dev
docker build --target runtime -f harnesses/opencode/Dockerfile -t evalhive-opencode:1.19.5 .
```

The initial image targets Linux/amd64 and pins the baseline executable package.
The same recipe supports a registered environment variant with
`--build-arg BASE_IMAGE=toolathlon-pack:latest`; the environment supplies benchmark
dependencies while the connector pins Node. This does not run a benchmark.
Native tests use only local model/MCP fixtures and run serially without external
network access or model credentials.

## Persistent benchmark sessions

Connector 0.2.0 supports interaction protocol 1.0 `conversation` and
`external_steps`. One native server and session persist over explicit user turns;
idle/completed assistant output ends a turn without ending the execution attempt.
An idle `session.close` ends the attempt. Inputs use `turn.submit`; legacy feedback
is rejected in a persistent session. Multiple text fragments in one input become
one native user prompt separated by blank lines. The model-step budget spans the
whole session. Abort works while generating or waiting for input or tools. Native
server death also ends an idle conversation with failure, without a restart.

Agent-visible initial history is checked for causal tool/result pairing and
loaded using the pinned CLI's official `opencode import` command into the same
isolated state tree. Completed tool parts belong to their native assistant
message. Imported context does not become new usage or normalized execution.
The connector does not claim transparent recovery of a dead native session.

For external steps the connector's MCP SDK is pinned to 1.32.2. A private loopback
gateway discovers upstream schemas and returns only driver-supplied results;
it never invokes upstream `tools/call`. The native request observer correlates
the complete streamed tool batch with its native assistant message. A fixed
native hook adds call identity after native validation, the gateway checks the
original name/arguments, and the transport identity is removed from subsequent
model context and normalized arguments. Further model requests wait for the
batch. Builtins are disabled and explicit builtin selection fails preflight.

`external_tools.mapping` records native/exposed/canonical names and both source
and exposed schema hashes. OpenCode's native sanitization and its object-schema
rules remain active; collisions and names exceeding the native provider limit
fail before a model request. Native prompts, validation and error formatting are
preserved. This is a declared adapted interface, not a baseline prompt replica.
