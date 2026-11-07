# EvalHive AppWorld connector

The package targets native revision `42b5bcf3cd334fee33f0c37c02070a9f5807add5`,
package version `0.2.0.dev0`, and official data version `0.2.0`. Native execution
and cancellation persistence have passed offline container probes. Full Pi
coordinator acceptance has passed with an original dev task, the 469-tool public
MCP catalog, persisted API effects and the native evaluator. OpenCode coordinator
acceptance is in progress. Offline model responses exercise infrastructure and
do not claim to solve the original task.

`native.lock.json` records the official data bundle, source Git LFS objects and
the unmodified wheel built with the upstream build backend. Materialize the
verified `apps.bundle` and `tests.bundle` objects before building the wheel: a
repository archive contains Git LFS pointers. Install the pinned dependency
closure and run the original `install_package` before catalog inspection.
Pydantic 2.12.5 preserves the native `GenerateSchema.match_type` customization;
newer incompatible internals must not be patched inside the benchmark.
FastAPI 0.135.1 retains the native `Path(embed=...)` interface; SQLAlchemy 2.0.46
preserves the native in-memory database URI identity. Installed service tests
remain in `/opt/appworld-installed-cache`, as required by the official API
server's installation check. The original source is never patched.

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

The worker drives an independently owned World service and Task container on an
internal network. The World owns protected data, evaluator reports and a private
state volume; the Task receives only a read-only public connection description,
its working directory and the installed runtime bundle. Both the World and Task
need the read-only runtime bundle for owned exec and finalization. The official
MCP server is invoked through its library entrypoint, so no protected dataset is
required inside the Task container.

The native API runs in a separate child process. Capture seals public requests,
waits for actual ASGI effects, calls `world.save`, then evaluates persisted state
in the parent process. Cleanup also saves after cancellation when state remains
available. A failed save cannot acknowledge successful cleanup. The registered
World finalizer is replayable and requires explicit host support. Task cleanup
precedes World cleanup.

Build `Dockerfile` with verified `runtime_wheels` and `native_wheel` contexts.
Administrative installation requires an explicit root/cache and disabled dotenv:

```bash
PYTHON_DOTENV_DISABLED=1 APPWORLD_ROOT=/installed/native-root \
APPWORLD_CACHE=/private/installation-cache \
python -m evalhive_appworld.installation /installed/native-root /new/package \
  --split dev --revision data-0.2.0-local-v1 \
  --worker-image sha256:<worker-image-id> --task-image sha256:<task-image-id>
```

Installation freezes complete split membership, data hashes, runtime assets and
image identities. Changed resource registrations require a new installation
revision. The initial measured engineering budget is 384 MiB for the worker,
1,024 MiB for the World and 1,280 MiB for the Task. This is an adapter budget,
not an upstream benchmark minimum; host reserves remain mandatory. Protected
state/report archives stay evaluator-only. Independent regrading is explicitly
unavailable until native state restoration is installed.
