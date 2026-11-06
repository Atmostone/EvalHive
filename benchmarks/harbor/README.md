# EvalHive Harbor connector

This package targets Harbor 0.25.0 and Terminal-Bench 2.0, pinned by
[`sources.lock.json`](../../benchmark-sdk/sources.lock.json). Native task parsing
uses Harbor's `Task`, including its instruction, canary and verifier-mode rules.
Additional validation rejects unknown settings instead of allowing Pydantic to
silently drop them. Native requirements and raw configuration remain available.

The current package contains parsing and installation foundations only. It is not
an enabled server installation or a completed runtime connector. Native image
registration, service planning, readiness, shared/separate verifier execution,
trusted reward collection and engineering acceptance are tracked in SPA-146.
