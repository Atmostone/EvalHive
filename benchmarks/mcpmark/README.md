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

The native world keeps state managers and browser objects on one dedicated
thread. Private control uses a mode-0600 Unix socket in a private directory,
bound to one workspace, trial and generation. Mutating requests have repeatable
receipts; polling observes current progress. Broker effects can suspend native
work without granting it a Docker socket. The task container never mounts this
control directory or the native source/checker tree.

For Filesystem, the native manager copies an installed category into
`<native-root>/.mcpmark_backups`. That working volume is shared with the task;
the initial category is read-only. Installed initial-file digests, modes and
timestamps contribute to the dataset source digest and are checked before
preparation. Native setup produces a connection file on a separate volume that
the task mounts read-only. Missing categories are rejected before native code
can attempt a download during a trial.

Verification still runs after agent failure, as in upstream MCPMark. A normal
nonzero checker exit is a complete negative verdict; a checker exception or
timeout is incomplete grading. The retained native result preserves this
distinction. `MCP_MESSAGES` receives the documented normalized Harness message
projection, not a fabricated byte-identical upstream agent transcript.
