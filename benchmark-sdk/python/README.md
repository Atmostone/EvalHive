# EvalHive Benchmark SDK

This package defines the versioned benchmark and environment-provider process
contract. See the repository's `benchmark-sdk/README.md` for lifecycle, visibility,
transport, installation and conformance requirements. It imports neither the
EvalHive backend nor a Docker or model client.

## Public tool discovery

The optional `mcp` extra installs the pinned official MCP SDK. An external-step
benchmark can expose a read-only public catalog with:

```sh
python -m evalhive_benchmark.tool_catalog /public/tools.json
```

The bounded JSON document has `format_version: "1.0"` and a `tools` array of
`name`, `description` and unmodified `inputSchema` objects. Mount only these public
schemas into the task environment. Keep gold, user-simulator context, databases
and native effect handlers in the private benchmark worker. This process lists
tools but refuses execution: effects must be requested through the harness's
external-step interface and performed by the native benchmark driver.

Stateful adapters can use the optional `WorldClient`, `world_control.serve` and
`world_rpc.World` helpers for private, scoped native service operations. The
service validates scope and command identity, preserves mutation receipts and
keeps native objects on one thread. Its socket directory and socket require
private permissions. The task environment must not receive this socket.

`WorldClient` invokes the installed `world_wire.py` file directly. This transport
uses only the standard library; frequent readiness polls must not import native
frameworks or the SDK dependency graph. The service uses OS monotonic deadlines
and a fixed selector interval so native simulated clocks cannot break control
when they are frozen or restored. These helpers do not replace host resource
ownership, admission, cancellation or finalization authority.

Native aggregate receipts can be checked with
`evalhive_benchmark.aggregation.verify_record` and recomputed with an explicitly
supplied native adapter using `recompute`. The adapter receives a copy of the
recorded inputs; returning a different native result is an error.

`python -m evalhive_benchmark.archive FILE.json` verifies a native archive without
a database, object store or model calls. `recompute_archive(document, adapters)`
requires explicitly installed adapters keyed by connector ID, connector version,
worker image and dataset source digest. The archive never selects executable
code. Public archives omit opaque checker outputs and cannot be used for native
recomputation. Artifact bytes are explicitly outside this result archive.
