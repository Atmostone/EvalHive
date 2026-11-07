# EvalHive Harbor connector

This package targets Harbor 0.25.0 and Terminal-Bench 2.0, pinned by
[`sources.lock.json`](../../benchmark-sdk/sources.lock.json). Native task parsing
uses Harbor's `Task`, including its instruction, canary and verifier-mode rules.
Additional validation rejects unknown settings instead of allowing Pydantic to
silently drop them. Native requirements and raw configuration remain available.
Tasks that prohibit network access require `host.model_gateway_v1`: model
transport must not widen the task's native network policy. Allowlisted networking
and other unavailable capabilities also remain explicit compatibility requirements.

The current package contains parsing and installation foundations only. It is not
an enabled server installation or a completed runtime connector. Native image
registration, service planning, readiness, shared/separate verifier execution,
trusted reward collection and engineering acceptance are tracked in SPA-146.

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
