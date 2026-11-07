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
