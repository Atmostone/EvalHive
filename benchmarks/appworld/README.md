# EvalHive AppWorld connector

The package targets native revision `42b5bcf3cd334fee33f0c37c02070a9f5807add5`,
package version `0.2.0.dev0`, and official data version `0.2.0`. Execution wiring
is in progress; this directory is not yet an installed executable connector.

`native.lock.json` records the official data bundle, source Git LFS objects and
the unmodified wheel built with the upstream build backend. Materialize the
verified `apps.bundle` and `tests.bundle` objects before building the wheel: a
repository archive contains Git LFS pointers. Install the pinned dependency
closure and run the original `install_package` before catalog inspection.
Pydantic 2.12.5 preserves the native `GenerateSchema.match_type` customization;
newer incompatible internals must not be patched inside the benchmark.

Native imports require explicit `APPWORLD_ROOT`, `APPWORLD_CACHE` and
`PYTHON_DOTENV_DISABLED=1`. The source guard verifies all 143 installed native
Python files and checks data and database versions. Native `load_task_ids` and
`Task.load(load_ground_truth=False)` define the complete split catalog. Receipts
retain task and scenario identity, source file hashes and exact membership,
without publishing ground truth. Original splits contain 90 train, 57 dev,
168 test_normal and 417 test_challenge tasks. Keep decrypted data and protected
source private; repository metadata contains hashes, not decrypted task files.

The public REST surface is derived from original API documentation. It excludes
administrative and API documentation applications, rejects path confusion and
uses a fixed private loopback backend. Both proxy requests and actual native
ASGI work must quiesce before persistence. Its deadlines use the OS monotonic
clock because the native world freezes Python clocks.

The complete public catalog has 469 tools across ten applications. Deterministic
groups preserve every app and tool within each harness MCP server budget. The
native server receives the group's app names; administrative tools are never
added. Native scenario grouping remains available for the official TGC/SGC
calculation, including explicit handling of incomplete groups.
