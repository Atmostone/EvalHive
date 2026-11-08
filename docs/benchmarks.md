# Case Store compatibility and native execution

Existing YAML/JSON suites and historical results remain readable. New benchmark
execution uses installed Benchmark SDK packages, registered dataset revisions
and explicit workspace benchmark/harness profiles. The historical core
Toolathlon executor and direct task materialization have been retired.

Use `evalhive-cases` for supported file-store cases and `toolathlon-gym` for
native GYM tasks. Package installation, private gold isolation and evaluation
policies are documented in the [Cases package](../benchmarks/evalhive-cases/README.md),
[GYM package](../benchmarks/toolathlon-gym/README.md) and [SDK architecture](benchmark-sdk.md).

## Where cases live

```
backend/benchmarks/<suite>/*.yaml        # or *.yml / *.json
```

Git is the store and its history. One file = one case. Suites currently in the
store:

- `capability-isolation` — hand-authored capability cases (`exact_compute`,
  `fresh_data`, `private_data`, `local_state`).
- `toolathlon` — imported Toolathlon gym tasks (`external_eval` gold, real MCP
  tool environments).
- `toolathlon-open` — open-ended Toolathlon variants.

## Case format

```yaml
id: cap-compute-001                 # unique within the suite
suite: capability-isolation         # must equal the directory name
category: exact_compute             # free label (capability uses: fresh_data|private_data|exact_compute|local_state)
input:
  title: "Multiply 48271 by 92837"
  description: "Return only the exact product."
  context: []                       # reserved: RAG doc refs / attachments (used by E-23)
gold:                               # pluggable gold envelope — each key feeds one eval engine
  capability_spec:                  # E-13: tool(s) the task cannot be solved without
    required_tools: [bash]
    match: all                      # all (default) | any
  reference_answer: "4481..."       # E-03 / outcome correctness (optional)
  rubric: null                      # E-02 rubric ref or inline (optional)
  canonical_trajectory: null        # E-09 (optional)
environment:                        # optional — services/tools the case needs to run
  required_services: [toolathlon_pg]  # historical declaration; native adapter provisions services
  mcp_servers: [filesystem, terminal] # MCP servers to expose to the agent
repro:                              # optional pins (reproducibility; E-20 seam)
  template_id: null
  model_id: null
  seed: null
meta:
  source: "hand-authored"
  license: "CC0"
  public: false                     # E-23 publication gate
  valid_until: null                 # for categories whose answer expires (fresh_data, local_state)
```

Only `id`, `suite` and `input.title` are required. The `gold` keys are all optional
— a case supplies whatever signals its eval engine needs (the loader maps
`capability_spec` / `reference_answer` / `canonical_trajectory` onto the task; new
keys are added as new eval engines arrive, with no schema migration).

## Running an installed suite

First install immutable packages/images and register the dataset revision, then
configure workspace benchmark and harness profiles. In Experiments choose a
native benchmark matrix; Work also supports a single installed case. Both use
the shared durable coordinator and expose grading separately from execution.

The CLI starts the same native matrix:

```bash
python -m app.cli.benchmark load --suite capability-isolation \
  --workspace-id <workspace_uuid> \
  --benchmark-profile <benchmark_profile_uuid> \
  --dataset-revision <registered_revision_uuid> \
  --harness-profile <harness_profile_uuid> --repeat 1
```

`--suite` must match the registered dataset identity or its recorded suite.
The command freezes the selection and queues serial cells; it does not install
code, materialize ordinary READY tasks or select a model through old overrides.
`--template`, `--model`, legacy `benchmark_suite` experiment creation and
`n_toolathlon_lanes` launch controls fail with migration errors. Choose new
profiles/settings explicitly; an old floating image cannot be replayed exactly.

Historical `suites`, `status`, `evaluate` and `aggregate` CLI commands remain
available for the original case/result format. The Toolathlon pilot `status`
reader remains available; its `create`, `ready` and `verdict` writes are retired.
Historical experiments preserve frozen cases, checker logs, verdicts and lane
metadata. An old running matrix is paused with a migration reason before it can
claim work; its historical cells are not rewritten or automatically restarted.
Retire or drain any historical in-flight resources under the release procedure
before cutover; the SDK does not adopt containers from the removed executor.

## Catalogue API (read-only)

`backend/app/api/benchmarks.py` (SPA-54) exposes the file store over HTTP for historical browsing and compatibility tooling. Both
endpoints are workspace-scoped and read-only — case authoring stays file-based.

- `GET /api/benchmarks/suites` — list every suite with its case count:
  `[{ "name": "capability-isolation", "n_cases": 30 }, …]`.
- `GET /api/benchmarks/suites/{suite}` — inspect one suite. Returns
  `{ "suite", "n_cases", "cases": [...] }` where each case carries `id`, `title`,
  `category`, `family`, `required_services`, `mcp_servers` and a `gold` map of
  booleans (`reference_answer` / `rubric` / `canonical_trajectory` /
  `capability_spec` / `external_eval`) — which signals are present, **not** the
  gold values. Returns `404` for an unknown suite, `400` for a malformed case file.

## Linkage

Each historical materialized task carries `benchmark_case_id` / `benchmark_suite`; these are
denormalized onto its `quality_record` (so aggregation survives task deletion).
Migration `e6f7a8b9c0d1`.

## Notes & limits

- **Outcome correctness** for a run reuses the E-02 judge (a scored `reference`
  dimension when the rubric has one — objective and preferred — else the weighted
  score ≥ `capability_outcome_threshold`). For verifiable answers (exact compute,
  private RAG facts), wire a rubric with a `reference` dimension.
- **Expiring categories** (`fresh_data`, `local_state`) need periodic re-curation;
  use `meta.valid_until`.
- **`private_data`** cases need their RAG document ingested into the knowledge base
  before running, so the fact is retrievable but not in pretraining.
