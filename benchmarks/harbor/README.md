# EvalHive Harbor connector

This package targets Harbor 0.25.0 and Terminal-Bench 2.0, pinned by
[`sources.lock.json`](../../benchmark-sdk/sources.lock.json). Native task parsing
uses Harbor's `Task`, including its instruction, canary and verifier-mode rules.
Additional validation rejects unknown settings instead of allowing Pydantic to
silently drop them. Native requirements and raw configuration remain available.
Tasks that prohibit network access require `host.model_gateway_v1`: model
transport must not widen the task's native network policy. Allowlisted networking
and other unavailable capabilities also remain explicit compatibility requirements.

The package includes a Benchmark SDK worker, native shared/separate verification,
artifact capture and an offline administrator installation builder. Server
enablement and complete runner acceptance remain in SPA-146. Unavailable native
features remain explicit compatibility requirements, including Compose/sidecar
bindings, artifact tar exclusions, native MCP/skills and dynamic network policy.

`dataset.build_receipt` produces canonical SDK installation metadata from native
task directories. `InstalledHarbor.resolve` checks the complete case descriptor,
source bytes and unchanged native requirements before returning Harbor's parsed
Task. Private tests, solutions and instructions are not copied into the public
catalog. Nested native paths retain their native case identities; references
are validated independently before filesystem access. The caller must register exact worker and task images separately;
a dataset receipt does not build images or enable execution.

`verification.run_verifier` delegates test staging, stale reward removal, command
construction and reward parsing to Harbor's official `Verifier`. It applies the
native verifier user and the earlier native/parent deadline. Native image
precedence determines whether tests are bundled. Checker transfer failures,
missing/empty/invalid rewards and timeout remain separate error categories;
numeric rewards keep their native names and values without an inferred binary
success threshold. The function requires an already prepared environment adapter
and an isolated worker with explicitly bound native configuration. Current unit
coverage uses the real pinned Verifier and a deterministic environment fixture;
full native task acceptance remains pending.

`environment.BrokerEnvironment` supplies the native verifier's command and
directory-transfer interface using scoped Benchmark SDK broker requests. Native
commands retain their working directory, user, environment and the earlier
native/parent deadline. Unconfirmed execution receipts fail explicitly;
truncated diagnostics mark the recording incomplete. Transfer implementations
must keep private checker inputs outside the agent environment until grading.

Harbor regrade requires a separate verifier and its declared recorded artifacts.
A shared verifier runs against the live task environment; it cannot be rerun
after cleanup merely by copying files. A replacement separate verifier may grade
an originally shared trial only when its declared inputs were recorded, as
specified by Harbor's native regrade contract.

`transfers.BrokerTransfers` stages checker uploads in a private service volume
mounted read-only in the task. Task filesystem operations use an explicitly
loaded, immutable BusyBox toolchain rather than task-provided utilities. Transfers
use 24 KiB chunks with archive size and SHA-256 verification. The 64 MiB archive
limit and temporary copies must be included in the installation's resource plan.
Downloads validate archive paths, links, entry counts and logical bytes before
extraction. Log selection uses Harbor's filter rules and preserves protected
reward files. Unsupported filesystem entries and oversized transfers fail
explicitly. The engineering fixture exercises the actual pinned native verifier,
shell commands and toolchain without an external model; separate task/service
isolation and full runner acceptance are still pending.

`worker.HarborConnector` uses registered assets for its private staging service,
task and optional separate verifier. Source instructions use Harbor's parser.
The native agent user and optional whole-second timeout remain distinct from the
image and verifier defaults. Shared checks retain successful agent services until
verification completes. Separate checks collect native inputs before stopping
the task; complete inputs produce a private replay checkpoint. A fresh evaluator
restores those inputs into an independent verifier without an agent. Reward
values and Harbor's `Mean` aggregation are preserved, including its treatment of
unscored observed trials; population coverage is reported separately.

`installation.install` snapshots selected native directories, exact image
bindings, explicit resource budgets and the pinned transfer toolchain. Lowering
native resource requirements is rejected. Native image entrypoints, working
directories, users and declared environment values are retained. Unresolved host
environment templates require a private binding adapter. `images.inspect_image`
checks reserved control paths using an administrator-supplied rootless Docker
client without starting image code. The resulting package contains `manifest.json`,
`resources.json`, `files.json`, dataset receipts and worker-private native data;
backend modules are never imported by the connector.

Build the worker with the hashed native parser/verifier dependency subset:

```sh
python -m pip download --only-binary=:all: --no-deps --require-hashes \
  -r benchmarks/harbor/requirements.lock -d /tmp/harbor-runtime-wheels
docker build --network none \
  --build-context runtime_wheels=/tmp/harbor-runtime-wheels \
  -f benchmarks/harbor/Dockerfile -t evalhive-benchmark-harbor:0.1.0 .
```

The image installs the official Harbor wheel and the dependencies used by its
parser, native verifier, file selection, artifact handling and aggregation.
Harbor's own agent loops, cloud providers and model clients are not imported.
The complete upstream wheel remains pinned; unused optional execution paths
must not be enabled without installing and validating their dependencies.

Separate-verifier case metadata records independent `evaluation_requirements`
from Harbor's resolved verifier environment. Task requirements retain their
original values. Artifact restoration uses the preparation budget; the native
verifier timeout begins immediately before `Verifier.verify`, matching Harbor's
separate-verifier lifecycle.
