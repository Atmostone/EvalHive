# Pi connector

The package uses the official Pi coding-agent SDK and MCP client pinned to 1.5.0.
Pi owns model requests, tool execution, automatic continuations, retry and
compaction. The connector translates resources, controls and events to the
Harness SDK. The native version and connector version are recorded separately.

## Persistent benchmark sessions

Connector 0.2.0 supports Harness SDK interaction protocol 1.0. `conversation`
retains one native AgentSession across explicit user turns. `external_steps`
additionally delegates tool effects to the benchmark driver. Without an
interaction selection the existing single-turn behavior is preserved.

Agent-visible initial history is checked for complete tool/result pairing and
replayed with SessionManager and the public Agent state. It is context, not new
execution evidence or current-run usage. Multiple user text fragments in one
input are joined with a blank line into one native user prompt. `agent_settled`
ends that turn only after native continuations finish; `session.close` ends the
attempt. Typed conversation input uses `turn.submit`; ordinary `feedback` is
rejected in a persistent session. Abort works during generation and while waiting
for input or a driver tool result. The model-step budget spans the entire session.

External steps discover MCP schemas but never call the source server's tools.
The complete native assistant message supplies the tool batch and call IDs.
Each registered native tool waits for the matching driver's result; partial and
out-of-order delivery cannot interchange identical calls. Further model requests
wait for that batch. Builtins are disabled by default in this mode and selecting
one explicitly fails before a model request. Native validation and the native
agent prompt remain active: this is an adapted MCP interface, not the benchmark's
original baseline agent. Native/exposed/canonical names and schema hashes are
included in `external_tools.mapping` evidence.

Sessions are process-local and cannot be transparently resumed after worker
death. A coordinator must reconcile its durable command journal and start a new
trial instead of replaying an uncertain input into a fresh native session.

Build and test with the EvalHive rootless Docker endpoint:

```bash
docker build --target test -f harnesses/pi/Dockerfile -t evalhive-pi-tests:dev .
docker run --rm --network none --memory 2g --cpus 1 --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges:true --init evalhive-pi-tests:dev
```

Tests use the real pinned SDK, a local fake model endpoint and local MCP fixtures.
They never need an external model, service account or scientific benchmark run.

Dependency installation prefers the BuildKit npm cache and still verifies the
committed lockfile integrity. An administrator with a complete cache can add
`--build-arg NPM_OFFLINE=true` to require a build without package downloads. A
missing archive fails explicitly with `ENOTCACHED`; this does not change package
versions or permit unpinned dependencies. The default remains online-capable.
