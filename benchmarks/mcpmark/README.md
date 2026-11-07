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
the initial category is read-only. Installed file digests and file/directory
modes and timestamps, including empty directories, contribute to the dataset
source digest and are checked before
preparation. Native setup produces a connection file on a separate volume that
the task mounts read-only. Missing categories are rejected before native code
can attempt a download during a trial.

Verification still runs after agent failure, as in upstream MCPMark. A normal
nonzero checker exit is a complete negative verdict; a checker exception or
timeout is incomplete grading. The retained native result preserves this
distinction. `MCP_MESSAGES` receives the documented normalized Harness message
projection, not a fabricated byte-identical upstream agent transcript.

The native Filesystem and PostgreSQL paths have passed serial coordinator tests
through real Pi and OpenCode with local fixture model responses. The original
Filesystem checker confirms six produced files. PostgreSQL restores all five
original backups, uses the native MCP SQL client and confirms the created index
through the original checker. These are engineering checks of selected cases;
they do not establish correctness or performance across the complete catalog.
Validation rejects Filesystem categories and GitHub templates that have not been
installed.

The PostgreSQL transport keeps the upstream state manager's restore algorithm
and sends only its CLI transport through a scoped broker effect. A fresh world
must confirm all five successful template restores; upstream warnings alone
cannot establish a valid baseline. The private-configuration helper accepts
credentials through an owned-exec private input, writes the world's private
configuration and a password file shared only with its PostgreSQL service, and
returns no credential values. Owned exec passes the private input's file path,
which the helper opens with a bounded read; it does not expect inline JSON in the
environment variable. PostgreSQL's private network has no public egress, and its data volume
explicitly preserves the native image's PostgreSQL UID through Docker copy-up.
The published backup directory and files are readable by that unprivileged UID;
the original source permissions remain unchanged. Bounded, redacted restore
diagnostics are retained in the private report even before grading starts.

The other native variants have install, plan, state-manager, client and checker
bindings. External service settings are closed, explicit installation inputs and
contribute to the source fingerprint. GitHub requires a dedicated evaluation
owner and rejects upstream fallback to the authenticated user's account. Notion
uses separate source/evaluation keys and a private browser state file. No owner
accounts or browser profiles are discovered. GitHub and Notion additionally
require `host.resource_finalizers_v1`: stopping a local container cannot prove
that external objects have been deleted. The current host does not advertise
this capability, so these plans remain unavailable pending confirmed generic
finalization and cold-recovery acceptance.

WebArena's native Docker commands are translated to one registered site handle.
The package cannot load images, select another container or publish host ports.
World and site share the task's network namespace; the per-instance native port
configuration uses port 80. The original category image/name/readiness rules and
native setup/check/cleanup logic remain authoritative. Explicit site resource
minima are retained, including values that exceed the current VPS policy. Browser
images, external accounts and heavy site images require separate installation
and live acceptance; constructor and mocked-transport tests do not establish it.

GitHub's prepared repository context and WebArena's prepared URL are applied to
the public prompt after native setup. The Harness run specification freezes that
actual prompt before execution while retaining its checked model and resource
bindings. External live state without a frozen snapshot has an unknown initial
asset digest, rather than a fabricated immutable identifier.

The PostgreSQL client uses `postgres-mcp==0.3.0`. Its required `pglast==7.2.0`
release is no longer downloadable from PyPI. `Dockerfile.pglast` rebuilds version
7.2 from the exact upstream tag and pinned parser submodule, with the complete
source inventory checked before compilation. The source, build tools and wheel
result are recorded in the `pglast-*.lock*` files. The task build requires a
verified `postgres_wheels` context and checks both package dependencies and an
actual SQL parse before accepting the image.

`Dockerfile` uses the verified `native_wheel` and `runtime_wheels` build contexts.
It installs the unmodified native wheel with `--no-deps` and only the pinned
manager/checker dependency closure. `Dockerfile.task` installs the native MCP
servers from `clients/package-lock.json`; the image contains neither the native
task/checker tree nor a Docker client. Its fixed launcher reads only the prepared
connection and restricts Filesystem access to the native working-copy directory.
It also embeds the official GitHub MCP Server v0.15.0 from an immutable image and
checks the binary hash and native version from `github-client.lock.json`.
For Filesystem, a bounded stdio bridge removes client-advertised MCP roots and
root-change notifications: the upstream server would otherwise replace its
explicit directory with the Harness workspace. All other MCP messages and
capabilities pass through. The upstream server package is unmodified. The
`tests/native_roots_probe.py` task-image probe confirms that advertised workspace
roots cannot change this boundary or permit reads outside it.
Both containers use rootless Docker; the installed resource declarations choose
the same container UID for native file ownership to remain consistent.

Administrative installation is performed with `python -m
evalhive_mcpmark.installation --source <native-task-root> --output <new-directory>
--initial-root <frozen-category-root> --categories file_context --suite easy
--revision <installed-revision> --worker-image <immutable-image>
--task-image <immutable-image>`. Installation never downloads initial state. The
result includes a manifest, installed dataset receipt, private frozen inputs,
file registrations and declarative resource assets. Runtime bundles are supplied
through the ordinary `harness.runtime` binding.

Message pages are read while the agent API's driving phase is still active.
Capture then transfers the confirmed projection in bounded, idempotent chunks
and seals its digest before verification. Live Filesystem verification runs in
the same native world. Independent regrading remains explicitly unavailable
until a restorable native state checkpoint is implemented.
