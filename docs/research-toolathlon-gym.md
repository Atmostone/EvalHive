# Research: Toolathlon-GYM as a benchmark source (SPA-37 → E-23 / E-09)

Date: 2026-06-10 · Spike for [SPA-37](https://linear.app/spawnhive/issue/SPA-37). Based on reading the
code of [eigent-ai/toolathlon_gym](https://github.com/eigent-ai/toolathlon_gym) (clone, commit as of the
spike date), cross-checked against our stack (`agent-image/agent.py`, the SPA-41 Registry, and the
Benchmark Case Store from `docs/benchmarks.md`).

## Verdict (TL;DR)

- **E-23 (external task set): YES, adopt** — via a pilot on 10–20 tasks before bulk import.
  Apache 2.0 license, fully local, evaluation scripts are standalone and solid, and their 25 MCP servers
  map onto our Registry (SPA-41) almost 1-to-1.
- **E-09 (source of canonical trajectories): NO** — there are no trajectories in the data.
  `task_config.json.meta` is empty in all 503 tasks; only the *set* of servers (`needed_mcp_servers`) is
  given, with no call sequence. The set can serve as a weak E-13-level signal (`required_tools`,
  match=all), and canonical trajectories can later be mined from our own reference runs if needed.

## What it is (verified facts)

503 tasks in `tasks/finalpool/`, 25 stdio MCP servers, everything runs locally off a single PostgreSQL
(`db/init.sql.gz`, 8.2 MB → 11 schemas: `canvas`, `sf` (Snowflake mock), `email`, `gcal`, `gform`,
`gsheet`, `notion`, `woocommerce`, `arxiv`, `scholarly`, `train`). No external APIs and no real tokens at
runtime (`configs/token_key_session.py` is a stub). Built on the Toolathlon infrastructure (HKUST-NLP);
the enlarged pool is by eigent-ai (CAMEL).

MCP-count distribution per task: 4 → 123, 5 → 133, 6 → 105, 7 → 126, 8 → 16.
Families: snowflake/HR — 85, woocommerce — 63, terminal — 56, canvas/LMS — 55, yahoo-finance — 47,
fetch — 35, youtube — 25, howtocook — 24, playwright — 31, arxiv/scholarly — 24, the rest smaller.

### Task anatomy

```
<task>/
├── task_config.json        # needed_mcp_servers (4–8), needed_local_tools, meta (empty everywhere)
├── docs/task.md            # task description for the agent; service names obfuscated (anti-shortcut)
├── docs/agent_system_prompt.md
├── preprocess/main.py      # state reset/seed: DELETE across schemas + INSERT test data (psycopg2 in 422/503)
├── evaluation/main.py      # deterministic check (exit 0/1)
├── initial_workspace/      # agent input files (md/pdf/json/xlsx/csv/py…)
└── groundtruth_workspace/  # reference artifacts (743 files; 14 tasks have none — DB-only eval)
```

Pipeline: `preprocess` → agent (CAMEL ChatAgent, 100-step budget, terminates via `claim_done`)
→ `evaluation/main.py --agent_workspace … --groundtruth_workspace … --launch_time … [--res_log_file …]`.

### Evaluation quality (spot checks)

Scripts are self-contained (psycopg2 + openpyxl/python-docx), median ~213 lines, granular
`[PASS]/[FAIL]` checks. Notably, expectations are often **computed from the live DB** rather than
hardcoded (e.g. `canvas-at-risk-intervention` validates a report row against a `SELECT COUNT(*)` over the
same schema) — robust and honest. Tolerances are reasonable (numeric tolerance, case-insensitive
headers). 409/503 evaluation scripts query PostgreSQL — evaluation must run in an environment with
access to the task's DB.

## License

**Apache 2.0** (LICENSE at repo root) — use, modification, and redistribution permitted with
attribution. One caveat for *public* re-publication (late-stage E-23): the mock data is derived from
Kaggle OULAD / HR Analytics / Yahoo Finance / Amazon+DummyJSON — keep attribution and check the
upstream terms before re-publishing the dataset. No restrictions for internal benchmarking.

## Compatibility with our stack

| Their side | Our side | Verdict |
|---|---|---|
| MCP configs `configs/mcp_servers/*.yaml`: stdio, `{command, args, env, cwd}` + variables `${local_servers_paths}`, `${agent_workspace}`, `${task_dir}` | Registry (SPA-41): kind=mcp, `config={command,args}`, `secrets=env`; the agent reads `MCP_SERVERS=[{name,command,args,env}]` and spawns stdio itself (`agent-image/agent.py:_connect_mcp_servers`) | **Nearly 1-to-1.** Missing: (a) passing `cwd` through to `StdioServerParameters` (the mcp SDK has the field — a couple-line patch); (b) resolving their template variables at import/spawn time |
| Environment: ubuntu22 image + uv + node22 + playwright + 19 pre-built servers in `/opt/local_servers` | Our `agent-image` is python-slim with agent.py only | **Derived image**: `FROM toolathlon-pack:latest` + our `agent.py/entrypoint.py/…` + `pip install litellm mcp fastapi uvicorn httpx` into their venv. Cheaper than pulling node/uv/servers into our image |
| DB: `toolathlon_pg` (postgres:15 from `init.sql.gz`), MCP servers and eval reach it via `PG*` env | `docker-compose.yml`, our own agent network | Separate service/profile in compose + shared network + `PG*` env in the agent container |
| Isolation: `run_parallel.sh` gives **each task its own postgres + network** (while `run_containerized.sh` serializes runs with flock due to the shared DB) | Our runner manages containers itself | Adopt the per-run postgres scheme — otherwise parallel runs share state |
| Agent loop: CAMEL ChatAgent | Our own LLM loop in agent.py | **Their agent is not needed.** `preprocess` and `evaluation` are standalone shell scripts; our agent slots between them without touching their code. Their "eval only after claim_done" gate is unnecessary for us — we run eval unconditionally |

### Format mapping onto the Benchmark Case Store (pre-E-23)

`input.title/description` ← `docs/task.md` (obfuscated text — a plus);
`meta` ← `needed_mcp_servers`, max_steps, family; `gold.capability_spec.required_tools` ←
`needed_mcp_servers` (weak set-level signal). **Format gap**: their "gold" is an *executable checker +
reference artifacts*, and our `CaseGold` has no such slot. Needs a case-format extension (E-23 level):
`gold.external_eval = {preprocess_command, eval_command, groundtruth_path}` plus an environment block
(registry `tool_ids` references, the `toolathlon_pg` requirement). We do not vendor the dataset into
git — the repo clone stays an external dependency (path is configurable).

### Suitability for E-11 / E-12

**Yes.** The environment is deterministic: fixed DB dump, `preprocess` resets state to the baseline
before every run, per-run isolation is reproducible — the same task can be run N times (variance, E-11).
Perturbation injection (E-12, `AGENT_TOOL_INJECTION`) operates at our level and is dataset-independent.

## Integration scope (decomposition)

| # | Subtask | Size |
|---|---|---|
| 1 | **Infra**: compose profile `toolathlon` (`toolathlon_pg` + image builds), shared network with agents | S |
| 2 | **agent.py**: pass `cwd` into StdioServerParameters + `PG*` env passthrough | XS |
| 3 | **MCP import into Registry**: script/CLI `configs/mcp_servers/*.yaml` → 25 registry entries (kind=mcp) with template-variable resolution | S |
| 4 | **Case adapter**: `tasks/finalpool/<task>` → `backend/benchmarks/toolathlon/*.yaml`; `gold.external_eval` format extension in `quality/benchmark.py` | M |
| 5 | **Runner glue**: preprocess → direct spawn of our agent (= the SPA-40 benchmark execution path, bypassing the orchestrator) → eval → binary verdict into `quality_records` + E-20 snapshot | M |
| 6 | **Pilot**: 10–20 tasks across families; compare their pass/fail with our E-02/E-07 profiles (E-17-style agreement); go/no-go for bulk import | S–M |

Total up to the pilot: ~2 short tasks (1–3) + 2 medium ones (4–5). Item 5 overlaps with SPA-40 — direct
spawn should be built as a shared mechanism, not Toolathlon-specific.

## Risks

- **Quality of the generated pool**: 503 tasks are a scaled-up extension of the original Toolathlon;
  eval spot checks look good, but a pilot (item 6) with manual verification of a sample is mandatory
  before bulk import.
- **Heavy image**: ubuntu + node + uv + chromium + 19 pre-built servers — long build and several GB of
  disk. Acceptable locally; build behind a profile in CI.
- **Eval needs a live DB** (409/503) — run eval in a container attached to that run's postgres, and pass
  the same `--launch_time` as preprocess (otherwise spurious date-based FAILs).
- **Parallelism only with per-run postgres** — a shared DB permits serialized runs only.
- **Data re-publication** (late E-23) — check the Kaggle upstream licenses; not an issue for internal
  use.
