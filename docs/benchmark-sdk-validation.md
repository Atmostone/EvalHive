# Benchmark SDK engineering acceptance

Epic G acceptance is tracked in [SPA-153](https://linear.app/spawnhive/issue/SPA-153).
The release and restore rehearsal are tracked separately in
[SPA-154](https://linear.app/spawnhive/issue/SPA-154). This inventory records
engineering evidence, not model quality or benchmark rankings.

## What the checks establish

Adapters are installed as Python distributions outside the backend. CI builds
each wheel and source distribution, installs the wheel, verifies that `app`
cannot be imported, and runs its contract tests. Benchmark SDK checks cover the
Python/TypeScript wire contract, worker lifecycle and source lock. Harness SDK
checks cover process transport, persistent conversations and external tool steps.

Native acceptance uses pinned Pi 1.5.0 and OpenCode 1.19.5 with deterministic
local model endpoints. On the VPS, native trials run one at a time through the
EvalHive rootless daemon. Native checkers and datasets remain unchanged; a
correctly recorded failing native verdict is an expected engineering result for
some fixture answers. A successful RPC alone is never evidence of benchmark
correctness.

The coordinator checks ownership and workspace isolation, idempotent delivery,
late events, deadlines, cancellation, resource admission and cleanup. Private
tests, user-simulator state and checkpoint contents remain outside agent inputs
and public archives. Independent regrading is accepted only where retained
evidence can restore the required native state. Missing reasoning, tool results,
cost or usage measurements remain explicit rather than being filled with zeros.

## Native coverage inventory

| Adapter | Actual native engineering coverage | Limits of the evidence |
| --- | --- | --- |
| `evalhive-cases` | Pi/OpenCode, native file work, matrix execution, retry, cold regrade and archive recomputation | Local deterministic case answers; no quality comparison |
| Toolathlon-GYM | Original registered cases, reset/launch-time checks, isolated dependencies and both harnesses | Representative tasks only; official Toolathlon retains a different identity |
| Harbor / Terminal-Bench 2 | Shared and separate verifier fixtures, Pi cold regrade, original `fix-git` through both harnesses, native reward and CTRF report | No full Terminal-Bench run; unsupported Compose/network features are rejected |
| Tau2 v1.0.1 | Original English airline task with native user, orchestrator, tools and evaluator; both harnesses; cold regrade without agent/user calls | Airline/retail/telecom catalogs and contracts do not imply live coverage of every domain or task |
| MCPMark Verified | Original filesystem, PostgreSQL and browser tasks through real harnesses and native checkers; browser startup and shutdown probes | GitHub/Notion use mocked transport checks, not live accounts; WebArena needs unavailable infrastructure; independent regrade remains unsupported |
| AppWorld | Original dev/train tasks, official MCP tool catalog, persisted state, explicit save, native checker and both harnesses; official TGC/SGC aggregation checks | No test-set evaluation or complete benchmark; native aggregate input is private |
| SWE-bench 5.0.2 | Original Verified `pytest-dev__pytest-10051`, native test patch/checker, both harnesses and Pi cold regrade | Lite/Full/Verified catalog support is broader than installed task images; missing images are unavailable, not passed |

Native frameworks, source revisions, dependency locks and installation receipts
are documented in the individual packages under `benchmarks/`. Candidate image
IDs and full logs are retained in the VPS engineering results directory. Updating
an adapter creates a new image and installation revision; older receipts are not
rewritten to point to new code.

## Product and archive checks

Work and Experiments use the same execution coordinator. API and browser checks
cover preflight failures, live phase display, files, feedback/cancel, immutable
matrix plans, repeat history, frozen versus changed-condition clones, profile
edits, retired configurations and workspace roles. Unknown monetary cost remains
nullable. Public archive export excludes private native inputs; exporting those
inputs requires owner/admin access.

The recorded aggregate population keeps missing cases and source evaluation
identities. Different execution conditions cannot be merged silently. Native
aggregation remains adapter-owned; unsupported aggregation is explicit. Archive
hashes detect content changes but are not signatures. Result archives retain
artifact identities and do not claim to include every environment or artifact
byte needed for complete execution replay.

## Current release gate

The first complete local backend batch produced 1,983 passing and 65 explicitly
skipped tests, with 76.06% coverage against the required 60%. Four failures exposed
an omitted Alembic configuration mount and stale version assertions in fixtures;
the corrected affected batch passed all eight tests. Runner checks passed all
25 tests. Clean CI and the final refreshed native images must pass before B16 is
closed. A verified paired database/MinIO backup, isolated restore, profile-version
upgrade, deployed smoke and rollback rehearsal remain separate B17 gates.

No owner provider credentials, external model calls, external account setup or
scientific experiments are part of this acceptance. Unavailable variants retain
their explicit availability reasons in the product.
