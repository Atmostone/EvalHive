# Toolathlon-GYM connector

This package adapts the GYM derivative pinned in
[`sources.lock.json`](../../benchmark-sdk/sources.lock.json), independently of the
official Toolathlon identity. Native task, environment and evaluation details
belong here; the backend must not import this package.

The CLI adapter binds the selected native task/phase, public workspace, private
groundtruth and report location as argv. One canonical UTC launch time is frozen;
the legacy native parser fallback removes the weekday from that same instant.
Neither retries nor evaluation take a fresh wall-clock value. Conflicting or
unsupported CLI arguments fail explicitly.

Native stdio MCP declarations retain executable/arguments, empty arguments,
working directories, timeouts and canonical aliases. Template variables resolve
inside the Task container; private native dataset directories never become MCP
working directories. The package uses the isolated native fixture PostgreSQL
coordinates, never a server owner's account. UV processes use already installed
environments with synchronization/network package installation disabled. Missing,
ambiguous or unsupported native settings are rejected before agent use.

Portal readiness will probe the actual public loopback HTTP resources in the
owned native network namespace. The URL adapter preserves prompt paths rather
than treating elapsed time or a running process as readiness.

The native dataset exporter creates a tar archive directly from the pinned Git
revision/tree, verifies its byte digest and indexes task/configuration metadata
without host extraction. All 503 native tasks can be represented as frozen SDK
case descriptors. Generated case metadata binds the complete native archive
digest, so changes to native scripts, data or MCP definitions require a new
dataset installation. Private groundtruth stays inside that archive; case prompts
contain only native public task documentation. Installed archive edits fail
validation before case resolution. Native writable task copies belong in the
owned private environment, not the administrator's source checkout.

Native corpus checks accept `TOOLATHLON_SOURCE_PATH` for the pinned public source
and `CASE_CORPUS_PATH` for existing EvalHive case templates. The current support
checks also accept `TOOLATHLON_SOURCE_ARCHIVE` and `TOOLATHLON_ARCHIVE_DIGEST` for
complete native archive verification.

The JSONL worker delegates agent execution to the common host. Its resource plan
uses an owned network, PostgreSQL instance, database volume and public workspace.
A private World container extracts the verified native archive, seeds the native
database, runs preprocessing and probes required portal resources. The agent Task
shares the public workspace and World network namespace, with no native task or
groundtruth mounts. Agent descendants must stop before state capture.

Capture preserves public files independently of the native verdict. A complete
private checkpoint includes the database, workspace and mutable native task
state. Evaluation-only workers restore it in fresh resources without resetting,
preprocessing or running an agent. Supported read-only native Python HTTP portals
have contained restart recipes and undergo the same HTTP readiness checks after
restore. Unknown live services disable checkpoint completeness. Incomplete
checkpoints explicitly disable
regrading. Native checker exit codes 0 and 1 represent correct and incorrect;
other exits are evaluator failures. Deferred quality evaluation remains unknown.

Unhandled checker exceptions are evaluator errors even when Python would normally
exit with code 1. A native-script wrapper preserves argv, imports and explicit
verdict exits. Checkpoints have a 248 MiB combined write budget before export,
including tar overhead; oversized logical files are rejected before reading.
Unavailable checkpoints retain the public workspace and a fixed reason code.
Native database operations share the owned command's deadline rather than a
second fixed timeout. Database restore runs in one transaction.

Restore replaces captured directories, preserving deletions rather than merging
them into the initial source. Private World preparation reserves 0.2 CPU; the
Task's separate 1 CPU limit is unchanged. Native owned commands explicitly
inherit the remaining parent deadline, so their maximum timeout cannot extend
the execution or evaluation budget.

`python -m evalhive_toolathlon.registration` publishes an installed manifest,
resource catalog and file map using immutable worker/environment/database image
pins. File grants use installation-relative paths and a host-supplied
`harness.runtime` binding. Publish only after building and checking the native
environment. The packaged manifest is not an enabled server installation.

Lifecycle and real JSONL subprocess contract tests are implemented. Complete
native image acceptance and migration of new legacy launches to the coordinator
remain tracked in SPA-145. These tests do not establish benchmark scores.

Public native system instructions are retained with Task-local workspace/time
bindings. Native local agent-loop helpers belong to the replaced baseline agent;
Pi/OpenCode own that loop. Unsupported native step/turn/stop policies fail instead
of silently changing their meaning. The installed rootless Task records
`PLAYWRIGHT_MCP_SANDBOX=false` because Chromium runs as container UID 0 inside the
existing rootless Docker boundary. This does not change Docker capabilities or
host security settings.

## Native environment image

Build `Dockerfile.environment` with the named `native-source` context pointing at
an unmodified checkout of the revision/tree in `source.py`. Check the source
checkout's HEAD and clean status before building. The image copies only native
MCP projects, configuration declarations and locked dependencies; tasks and
private groundtruth enter the World through its separate immutable archive.

Normal builds use the committed overrides in `locks/`, `uv sync --locked` and
`npm ci`. Dependency resolution is an explicit maintenance operation through the
`native-locks` target and `NATIVE_LOCK_MODE=resolve`. Export and review its
`/opt/build-info/generated-locks` before updating the committed overrides; never
use that mode as an automatic fallback in an installed run. Reviewed native
package repairs verify original package bytes and are recorded alongside the
lock digests. Node project compilation is separately cached and fails on errors.
The final image records Debian package inventory and Python/Node versions.

Playwright browsers are installed in `/opt/playwright-browsers`, independently of
the harness's HOME/cache directories. MCP declarations explicitly bind this path
and the recorded rootless Chromium setting. After building, resolve worker,
environment and PostgreSQL images to immutable IDs/digests before registration.
The first real acceptance checks image boundaries and browser execution offline;
full Trial acceptance additionally exercises native preparation and evaluation.
