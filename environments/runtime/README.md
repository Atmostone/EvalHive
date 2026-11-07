# Owned execution supervisor

This static Linux helper runs one native process tree inside a prepared task
container. It is independent of harnesses, model providers and benchmark formats.
The host keeps the container and completed process lease alive until grading and
artifact capture finish. Normal native exit retains background descendants;
`stop` and `release` terminate only the owned descendants, including detached
processes. Unrelated environment services and container PID 1 remain alive.

Supervisor 1.1 supports conditional `expire` for durable deadlines. The native
control loop checks main completion when processing that command; an already
completed main retains its background descendants. Runtime compatibility checks
require the `conditional_expire` capability, so bundles built with older helpers
must receive new immutable images and inventory receipts.

The helper requires Linux subreapers and pidfds under the default rootless Docker
security policy. It never uses host PIDs or requires added capabilities. Build
dependencies are installed inside a disposable image from a frozen Debian
snapshot; the final artifact contains only the statically linked executable.

```sh
docker build -f environments/runtime/Dockerfile --target artifact \
  -t evalhive-supervisor:development .
```

The server installs the executable in a registered runtime bundle, mounted
read-only at `/evalhive-runtime`. `app.runtime.managed_exec` validates container
ownership, image identity, resource identity and Docker exec configuration on
every operation. Public resource RPC must use opaque broker handles; it must not
accept these internal host declarations directly. Durable generation authority,
operation receipts and leases belong to the coordinator.

The private control key and length-prefixed private files travel through stdin.
Neither keys nor file contents appear in Docker argv/environment declarations.
Private files are mode 0600 and are removed after native completion. The helper
prevents child access to its control input through procfs. This protects control
authority; a process necessarily can read the configuration explicitly supplied
to it, and must still be subject to connector redaction policy.

Authenticated READY/EXIT records are removed by the host's streaming transport
before SDK JSONL decoding. Native completion differs from supervisor completion:
the latter is a retained cleanup lease. Output buffers and diagnostics are
bounded, and a separate control channel permits cancellation under output
backpressure. If the supervisor disappears without authenticated completion, the
host marks the environment unsafe to grade, stops that owned container and
preserves its stopped filesystem for capture.

Engineering checks require the locally built artifact and workspace environment
image. They run serially, create no model requests and remove their resources:

```sh
EVALHIVE_NATIVE_RUNTIME=1 PYTHONPATH=backend python -m pytest \
  --confcutdir=backend/tests/runtime backend/tests/runtime/test_owned_exec_native.py
```

Unit checks cover lifecycle framing at every stream boundary, foreign exec
identities, private input declarations, size bounds and uncertain transport.

## Portable connector bundles

Connector Dockerfiles own a `bundle` target. The shared Node build helper copies
the pinned package and its SDK dependencies, dereferences local npm links, and
includes Node, shared libraries, licenses and the supervisor. Other harnesses may
produce the same receipt with a different build recipe. Core code does not know
native executable names or package layouts.

`requirements.runtime_bundle` in a harness manifest registers the artifact image
and `/bundle` export. The server resolves the local immutable image ID before
installation; it does not pull arbitrary images from a worker request. A
`bundle.json` receipt follows the canonical Harness SDK `runtime_bundle` schema:
connector identity/version, Linux amd64/glibc platform, fixed mount path,
entrypoint, no-model compatibility probes and a SHA-256/size/mode inventory.
The image identity is stored outside the receipt to avoid a self-referential
image digest. Every installed file and the receipt fingerprint are verified.

The installer streams bounded archives, rejects traversal, duplicate paths,
links and special files, and never starts the artifact container. Atomic
installation is keyed by the immutable image ID. The CLI installs only an
administrator-provided catalog declaration; UI code installation is unavailable.

```sh
docker build -f environments/runtime/Dockerfile --target bundle-tools \
  -t evalhive-runtime-tools:development .
docker build -f harnesses/pi/Dockerfile --target bundle \
  -t evalhive-pi-bundle:1.5.0 .
docker build -f harnesses/opencode/Dockerfile --target bundle \
  -t evalhive-opencode-bundle:1.19.5 .
python scripts/install-runtime-bundles.py --bundle-root /path/to/installed-bundles \
  --data-root /path/to/evalhive-data --manifest harnesses/pi/manifest.json \
  --manifest harnesses/opencode/manifest.json
```

Release helpers must replace development tool/artifact image defaults with pinned
digests. The ELF rewriter itself is the checksum-pinned upstream
[PatchELF 0.19.2 release](https://github.com/NixOS/patchelf/releases/tag/0.19.2).
Node's interpreter uses a short bundle-local path to avoid simultaneous growth of
interpreter and library-path sections. OpenCode's compiled binary is kept intact:
a connector-declared wrapper selects the bundled loader/libraries for that process.
Neither approach changes task PATH or exports a global library override. Pi's
unused optional X11 clipboard addon is explicitly excluded from its headless
server bundle. Exclusions and loader wrappers are recorded in the receipt.

Before mounting, inspect the immutable task image for a reserved-path collision;
initial task files must not be hidden. Probe the actual prepared environment with
`getconf GNU_LIBC_VERSION` and the bundle-declared commands, under owned exec and
bounded deadlines. A missing libc, native startup failure or uncertain probe
cleanup prevents a successful compatibility result. Compatibility still reports
`runtime_admission: pending`; it is independent of capacity reservation and the
coordinator's durable generation authority.

The opt-in `test_runtime_bundle_native.py` checks unchanged bundles in the
workspace and PostgreSQL task images using a local fake LLM. Native Pi/OpenCode
shell calls must see each image's own dependencies and initial files, preserve
results for independent capture, and keep the environment alive after completion.
A musl task must fail the supported-platform preflight before any model request.

## Group resource eligibility

`app.runtime.admission` measures the execution host's available memory, execution
storage free space and CPU count. Unknown measurements cannot fall back to static
policy maxima. The host provider normalizes each registered resource's native
minimum and requested limits; limits below the native minimum are rejected.
The group includes workers and services, checks the case's unchanged aggregate
requirements, host headroom, per-container maxima and group policy maxima.
Admission is serial across workspaces on the current host.

The pure check returns `eligible` with explicit reasons and the complete budget.
It always leaves `can_run: false` and `runtime_admission: reservation_pending`.
The coordinator must remeasure and check the budget under a durable global host
reservation lock, persist that reservation and fence the resource generation
before preparation. A capacity snapshot itself cannot authorize a launch.

## Resource broker boundary

`RootlessDockerProvider` implements the Benchmark SDK's six resource operations
against this runtime. Its authority is mandatory; production code has no fallback
to an in-memory resource map. The coordinator resolves immutable installed plans,
registered image/file assets, scope/phase rights, private input references and
deadlines into trusted grants. It persists a request before yielding an operation,
commits intents before native effects, and saves complete receipts before RPC
acknowledgement. A timeout/transport failure during a mutation is uncertain until
reconciled, never an invitation to repeat it blindly.

Worker requests have a small provider-owned vocabulary:

| Operation | Request payload | Result |
| --- | --- | --- |
| `allocate` | `{resource_id}` from the admitted plan | Opaque handle and kind |
| `exec` | Owned container handle, argv, optional cwd/user/environment/private refs/mode/deadline | Opaque exec handle; bounded stdout/stderr for `wait` |
| `inspect` | Owned handle and empty payload | Public lifecycle state |
| `stop` | Owned handle and empty payload | Owned process tree/container stopped |
| `export` | Owned container handle and saved `output_id` | Atomically stored SDK artifact |
| `release` | Owned handle and empty payload | Resource released idempotently |

Workers cannot supply host mounts, Docker socket paths, arbitrary image names or
export paths. Allocation reuses an already live resource from the same immutable
plan; it does not restart an exited container or retry an unresolved intent.
Exec preserves requested native settings and private inputs are ordered
canonically so JSON-object reordering cannot change the recovered argv or frames.
Normal completion retains owned background services. Native output is drained
without assuming JSONL, while returned diagnostics stay bounded. Full rootfs
exports are private. Missing/oversized/partially consumed evidence fails explicitly.

Wait-mode commands may explicitly request `process_policy: stop_after_command`.
The provider then stops owned descendants and releases the supervisor before
acknowledging the exit code, including nonzero exits. Uncertain release retains
the lease for recovery. The default `retain_until_cleanup` preserves native
background services; attached transports cannot request immediate release.
`host.ephemeral_exec_v1` identifies hosts supporting this optional policy.

SDK transports are a separate internal host operation. Only registered worker or
grader containers and owned execs can be attached. Public RPC returns handles,
not sockets or Python objects. A lost/already attached transport is not silently
reattached. Transport-triggered cancellation and unsafe-environment shutdown
reenter the authority fence, so an old runner cannot stop a new owner's resources.
An unsafe native exec marks recording failure and requests scoped quiescence.
Persisted exec deadlines are enforced by the coordinator, including attached
workers; completed processes keep their grading state.

Services can receive validated DNS aliases on an owned private network. Aliases
cannot select a foreign bridge or be silently ignored in a shared namespace.
There are no published host ports or host networking in this resource interface.

`test_docker_provider_native.py` runs a standalone Benchmark SDK package in a
separate container with neither backend modules nor a Docker client/socket. It
prepares another container, transfers private refs, handles a multi-megabyte
native output stream, retains a daemon and exports a file through RPC. Additional
native checks cover supervisor loss and isolated service DNS. Its explicit test
authority/sink verify this boundary; PostgreSQL durability and production launch
authorization are separate coordinator requirements, not properties of the test
fixture.

The `connector_container` checks also run the real pinned Pi/OpenCode workers in
their own root filesystems. A separate MCP environment performs the file change;
its filesystem and service survive harness shutdown. The local fake model lives
in that environment's isolated network namespace. This complements the bundle
checks for native shell execution with `in_environment` and avoids a combined
image for every harness/environment pair.
