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
features remain explicit compatibility requirements, including unsupported Compose
options, artifact tar exclusions, legacy SSE, private MCP header bindings and
dynamic network policy.

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
coverage uses the real pinned Verifier and deterministic native-format fixtures.
The full coordinator has passed shared/separate verification with real pinned
Pi/OpenCode and a local fixture model. Pi cold regrade has independently restored
the recorded files and reproduced the native reward without running an agent.
These are engineering acceptance checks, not Terminal-Bench performance results.

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

`transfers.BrokerTransfers` stages large checker uploads in a private service
volume mounted read-only in the task. Small uploads use one owned command with
the same size and SHA-256 proof and private temporary files. Newly created
destination directories retain normal traversal permissions for the native
verifier user. Task filesystem operations use an explicitly
loaded, immutable BusyBox toolchain rather than task-provided utilities. Transfers
use 24 KiB chunks with archive size and SHA-256 verification. The 64 MiB archive
limit and temporary copies must be included in the installation's resource plan.
Downloads validate archive paths, links, entry counts and logical bytes before
extraction. Log selection uses Harbor's filter rules and preserves protected
reward files. Unsupported filesystem entries and oversized transfers fail
explicitly. The engineering fixture exercises the actual pinned native verifier,
shell commands and toolchain without an external model. Coordinator checks keep
the worker, task and separate verifier isolated and preserve nonroot 0600 files.

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

`resources.agent_resources` maps native stdio/streamable HTTP MCP servers and
task-local skills to the common Harness SDK resources. Commands and argument
boundaries are retained; relative skills resolve against the native task workdir.
Built-in shell access remains available alongside discovered MCP namespaces.
Legacy SSE, nonportable/colliding namespaces and unresolved private header
templates fail before preparation. Evaluation-only workers do not initialize
agent resources.

Short transfer commands request `host.ephemeral_exec_v1`: their owned supervisors
are released after completion instead of accumulating until trial cleanup. Small
archives are written and sealed in one bounded staging operation with the same
SHA-256/size proof; larger transfers retain chunk acknowledgements and limits.
Archive creation, size and hash metadata share one task command. Native verifier
deadlines and agent background-service retention remain unchanged.

`compose.read_compose` is an administrator-side parser for bounded native service
descriptions. It preserves argv, literal environment values, service-start
dependencies, scoped named volumes and resource minima; it performs no host
interpolation or Docker operation. Cycles, duplicate keys and unsupported native
options fail explicitly. Installation requires exact immutable image bindings
for every native service and explicit disk budgets for named volumes. The worker
plans owned service containers, networks and scoped named volumes through the
common provider API. Default Docker copy-up preserves image files and directory
permissions. Native DNS names, start dependencies, image healthchecks and service
artifact paths are retained; no host Compose process or Docker socket is exposed
to the connector.

The entire resource group is reserved before execution. A separate verifier's
containers start only when grading begins, after task artifact collection and
shutdown. Sidecar artifacts are restored into the verifier main container using
Harbor's native replay rules. Pi and OpenCode shared-verifier engineering checks
have passed with a healthchecked HTTP sidecar, a shared volume and a native
UID-1000 file with mode 0600. Separate service replay acceptance is tracked in
SPA-146; the shared checks alone do not assert it.
