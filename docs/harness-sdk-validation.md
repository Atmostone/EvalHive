# Harness SDK engineering validation

The acceptance suite runs pinned Pi 1.5.0 and OpenCode 1.19.5 against a local
OpenAI-compatible fixture. It does not run scientific benchmarks or call an
external model. Native executions are serial, with one CPU and 2 GiB limits on
the EvalHive rootless Docker daemon.

The common browser scenario drives Settings and Work through the real HTTP API,
runner, native harness and MinIO. For each connector it verifies a file write,
an environment MCP call, feedback acknowledgement, cancellation, a provider
failure, retained files after each outcome and authenticated downloads. It also
checks contiguous journals, a terminal final event, unknown cost and absence of
orchestrator calls in direct mode. See [the acceptance runbook](../tests/e2e/README.md).

The H14 checkpoint passed all eight Work scenarios. Cross-language SDK checks
passed 82 Python and 77 TypeScript tests; runner checks passed 25 tests. Frontend
unit checks passed seven tests and its production build passed. A 2,000-event,
approximately 2 MiB journal survived reopening and ordered acknowledgements.
The full backend run reached 70.45% coverage against the 60% requirement; 1,222
tests passed and two historical assertions were updated for the new environment
and replay contracts. The affected integration suites then passed all 28 tests.
Release acceptance repeats the full suite after the default-harness cutover.

Manual review and targeted native checks covered these measurement invariants:

- Pi compaction requests are observed through the public native model runtime,
  including summaries that bypass agent hooks. Every actual request has its own
  usage identity and purpose. Missing provider measurements stay unknown;
  explicitly reported zeros remain measured zeros.
- Recovered provider failures remain visible through `failure.observed`.
  Native success and infrastructure contamination are separate facts. The
  transport observer permits OpenCode's own retries after a connection failure.
- Requested model identity stays stable when a provider reports another alias.
  Native reported aliases remain available as metadata. Cumulative usage updates
  replace previous samples instead of adding them twice.
- Unknown execution cost remains nullable in tasks, records, experiment cells and
  attempt ledgers. Decimal storage preserves small positive amounts. Cost means
  expose measured sample counts; monetary rollups identify known subtotals.
- Native child sessions and tool calls retain parent identities without relying
  on event adjacency. The real OpenCode task tool and a nested read are checked
  against the local fixture. Portable aliases retain historical MCP names.
- Late attempts cannot replace current evidence. Archives preserve the source
  attempt and applied configuration. Post-execution evaluation annotations are
  audited separately from immutable execution evidence.
- A finished attempt refreshes the Work card after its task projection commits,
  including files retained after cancellation. The browser suite exercises this
  transition instead of relying on a page reload.

Native payload recording is bounded. Large Pi request snapshots carry an
explicit omission reason, byte count and digest rather than a claim that every
payload was retained verbatim. Missing reasoning, usage, tool results and lost
stream deltas remain explicit in their respective evidence/completeness fields.

These checks validate execution and recording behavior. They establish no model
ranking, benchmark score, remote-weight pinning or scientific comparison.

The H15 packaging checkpoint passed native Pi 23/23 and OpenCode 28/28 checks.
All eight browser scenarios also passed with the backend/SDK/runner loaded from
the built image and connector manifests pinned to immutable local image IDs.
The deployed-release smoke helper was rehearsed against the isolated stack: both
connectors produced and downloaded the fixture artifact, no orchestrator was
called, and the temporary tasks and user were removed. The paired production
backup was restored separately; upgrading and downgrading its ten templates
preserved their original field hash. Backup verification additionally detects
missing result-file arrays and artifacts retained only in older attempt journals.

The final H15 backend checkpoint passed all 1,177 tests with 71.24% coverage
against the required 60%. The rollback helper was also rehearsed on an isolated
copy upgraded to the SDK schema: restoring the previous paired backup removed
new tables and recovered the original schema and all template contents.

## Authorized live OpenCode Go smoke, 2026-11-05

After the owner enabled the Global region, both deployed connectors passed the
same live task using `deepseek-v4.1-flash` at
`https://opencode.ai/inference/go/openai/v1`. The tested release was
`491292180da6c122aaeef4302114382945307fd9`; no production code or images changed.
Results and diagnostic history are recorded in
[SPA-135](https://linear.app/spawnhive/issue/SPA-135).

Each task called the environment MCP echo tool with a unique nonce, wrote and
read `smoke-result.json`, then returned its completion marker. The authenticated
download matched the expected JSON. Both attempts finished successfully with
contiguous terminal journals, complete observed tool calls, applied configuration
saved before execution, and no orchestrator usage or observed provider failure.

| Connector | Agent requests | Input tokens | Output tokens | Cache read tokens | Downloaded bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pi 1.5.0 | 4 | 5,442 | 294 | 3,840 | 126 |
| OpenCode 1.19.5 | 4 | 13,104 | 265 | 11,264 | 118 |

These are engineering observations, not benchmark comparisons. Recorded USD cost
and cache-write usage remain unknown. Live cancellation and feedback were not
part of this smoke; their deterministic acceptance coverage remains separate.
The temporary principal, workspace, provider, tasks, stored objects and task
directories were removed, the scheduler resumed, and service health passed.
The retained reports and logs were checked for the supplied credential.

OpenCode Go requires a stable `x-opencode-session` and an identifying User-Agent.
Both deployed connectors already supplied them. An initial standalone preflight
omitted the session header; only the smoke helper required correction. See the
[provider requirements](https://opencode.ai/v2/docs/console/go/#where-can-i-use-it).
