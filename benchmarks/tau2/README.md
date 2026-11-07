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
from this native source build. Presence of the catalog package does not yet
declare worker execution or live benchmark acceptance complete.
