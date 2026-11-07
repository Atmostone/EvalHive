# EvalHive MCPMark Verified connector

The adapter targets native revision
`cd45b7f57923b9b3985467f5139927575f83141c`. Native managers and verification
scripts remain in the benchmark worker; the upstream baseline agent is not used.
The backend consumes installed declarations and never imports this package.

The catalog uses official task managers and their metadata overrides. Service and
suite are explicit identities. At the pinned revision the standard catalog has
127 tasks: Filesystem 30, PostgreSQL 21, GitHub 23, Notion 28, Playwright 4 and
Playwright-WebArena 21. Easy contains ten tasks each for Filesystem, PostgreSQL,
GitHub, Notion and Playwright-WebArena. A separate Playwright/easy suite does not
exist and must not be synthesized from another dataset.

Administrative native imports require `PYTHON_DOTENV_DISABLED=1` and an explicit
`EVALHIVE_MCPMARK_NATIVE_ROOT`. All 60 native Python files are checked against the
packaged code lock before loading a manager. A previously imported foreign
`src` package is rejected. Private task inputs and verification scripts are
inventoried separately; descriptors expose paths and digests, without gold data.
Receipt resolution rejects changed cases, files and split membership.

The catalog alone does not advertise execution acceptance. Native state managers,
MCP server versions, transcript-based checks, owned service resources and cleanup
must pass lifecycle tests before a service is marked verified. Mutable CDN data
must be snapshotted and hashed during installation. External GitHub and Notion
variants require explicit dedicated test organizations/workspaces and private
credential references, never upstream defaults or an owner's existing account.
