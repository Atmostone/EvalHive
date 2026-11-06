# Owned execution supervisor

This static Linux helper runs one native process tree inside a prepared task
container. It is independent of harnesses, model providers and benchmark formats.
The host keeps the container and completed process lease alive until grading and
artifact capture finish. Normal native exit retains background descendants;
`stop` and `release` terminate only the owned descendants, including detached
processes. Unrelated environment services and container PID 1 remain alive.

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
