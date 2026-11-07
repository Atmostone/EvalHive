# EvalHive tau2 connector

This package targets the native tau2 1.0.1 half-duplex baseline at revision
`fc0055dc4e0a316c3f83133267fbd6faaa770992`. Integration is tracked in SPA-147.
The native orchestrator, user simulator, environment and evaluators remain in
the private benchmark worker. Harnesses supply the agent participant through
the shared external-step interface. Voice, solo and full-duplex modes are outside
this baseline.

`dataset.build_receipt` uses the official domain loader and native task models.
The `base` splits contain 50 airline, 114 retail and 114 telecom tasks. Telecom's
`full` split has 2,285 tasks and requires an explicit selection; it is not silently
substituted for the baseline. Split membership and task order follow upstream.
Receipts pin source files, policy, initial databases and user guidelines. Case
descriptors publish only identities, digests and requirements; user scenarios,
evaluation criteria and initial state remain private. Execution resolves an
exact frozen descriptor and returns an independent native task object.

Native imports require `PYTHON_DOTENV_DISABLED=1` and an explicit `TAU2_DATA_DIR`
matching the installed data root. Upstream's module-level dotenv discovery must
never read server configuration. Set `LITELLM_LOCAL_MODEL_COST_MAP=True` in the
isolated worker to prevent implicit remote pricing downloads. User and judge
model connections must be bound explicitly; upstream model defaults must not
select credentials or providers on the owner's behalf.

The upstream project declares version 1.0.1, but its checked-in uv lock still
lists the editable root as 1.0.0. Build the unmodified source as a native 1.0.1
wheel and verify its metadata and source bytes. Do not replace distribution
metadata to conceal the mismatch. Runtime dependencies are frozen separately
from this native source build. `Dockerfile` installs only verified local wheels;
the native wheel hash is checked before installation. `Task.Dockerfile` contains
the common Python/MCP environment, without tau2 or benchmark data.

Install from an isolated native environment with
`python -m evalhive_tau2.installation --source DATA_ROOT --output INSTALLATION
--domain airline --revision REVISION --worker-image IMMUTABLE_IMAGE
--task-image IMMUTABLE_IMAGE`. The installer does not start Docker or discover
credentials. It snapshots only the selected domain and user guidelines, freezes
the split receipt and publishes `manifest.json`, `resources.json`, `files.json`
and dataset metadata. Register the harness runtime binding on the server.
Worker data is private; the Task receives read-only public policy/tool schemas,
the common public SDK and a pinned harness runtime bundle. No harness-specific
Task image is required.

The connector uses native `build_user`, `Orchestrator`, `SimulationRun` and
`evaluate_simulation(EvaluationType.ALL, strict_replay=True)`. The official native
orchestrator executes agent and user tools, initializes shared state and chooses
termination. MCP names and schema hashes are checked before accepting tool
requests; builtin tools are disabled. This is explicitly recorded as
`adapted_mcp_external_steps`, rather than claiming a byte-identical native agent
interface. The default native runner limits are 200 steps, 10 errors and seed
300. The seed controls the native environment/user; agent sampling parameters
belong to the selected harness profile, recorded as `agent_seed_policy`.

Execution requires an explicit `user` model binding and additionally `judge`
when the task's native reward basis includes `NL_ASSERTION`. Unknown model
parameters and missing bindings fail validation. The official user model client
uses the scoped host model API; provider credentials remain on the host. Native
automatic request retries are disabled because an interrupted paid request can
have an unknown outcome. Auxiliary usage is recorded once by the host, separately
from harness usage; native usage inside private evidence is not counted again.

Allow at least ten seconds of bounded host shutdown grace for this worker. Even
an import-only native process takes over three seconds to exit at the registered
half-CPU quota. This teardown allowance does not extend native simulation or
evaluation deadlines. Incomplete delivery must still fail recording acceptance.

An owned private recorder stores the bounded native trace, checkpoint and report.
The checkpoint contains the native `SimulationRun` plus source and interface
provenance. Cold regrade starts only a worker/recorder and never an agent or user
simulator. It uses the original task and strict native environment replay; any
required judge is bound explicitly. Gold actions remain diagnostic unless the
native reward basis includes them. Interrupted simulations have no complete
checkpoint. Aggregation is not advertised until the native aggregate adapter is
installed.

Engineering tests cover native split membership, schema identity, official user
generation, grouped external tool calls, native reward/termination, private
evidence transfer and cold regrade. The rootless Pi/OpenCode acceptance fixture
is `backend/tests/integration/test_native_tau2_connector.py`; it requires explicit
opt-in, pinned images and local fake model endpoints. Availability and successful
live verification must be reported separately.

The 2026-11-07 rootless acceptance passed the original airline/0 task through
both pinned real harnesses with local fake model responses. Each run completed
with native reward 1, `user_stop`, complete recording and complete cleanup. The
same saved trajectory scored identically through the direct upstream evaluator.
Pi cold regrade reproduced the reward without another agent/user request and
left the source journal unchanged. Each execution recorded two agent and two
user requests in distinct ledger groups; missing pricing/cache/reasoning stayed
unknown. Migration downgrade/upgrade also passed. This is engineering acceptance
of the integration, not a benchmark score for either harness or a model.
