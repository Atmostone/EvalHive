# API

> As of 2026-07-10 (R1 + R2 + R6) — 50+ paths in OpenAPI. Source of truth — `/openapi.json` from the live API. This file is a topical map.

## Auth & multi-tenancy (R1)

After R1, every REST endpoint (except `/api/auth/*`, `/api/health`, `/api/v1/agent-webhook/*`, `/api/agent-webhook/*`) requires:

```
Authorization: Bearer <jwt>
X-Workspace-Id: <uuid>          # optional; falls back to the `ws` claim from the JWT
```

WebSocket endpoints (`/ws/events`, `/ws/chat`, `/ws/agents/{cid}`) accept auth via query string: `?token=<jwt>&workspace_id=<uuid>`. Invalid auth closes the socket with code 4401.

`/api/knowledge/search` is special: it accepts either a user JWT (regular CRUD style), or an agent service token (`Authorization: Bearer $SPAWNHIVE_AGENT_TOKEN`) plus `task_id` in the body — the workspace is then resolved from the task.

### Auth endpoints

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| POST  | `/api/auth/register` | `{email,password,display_name?}` | `{access_token, token_type, expires_in, user, default_workspace_id}` |
| POST  | `/api/auth/login` | `{email,password}` | same shape |
| GET   | `/api/auth/me` | — | `{user, workspaces:[{id,name,slug,role}]}` |

Token: HS256, ttl=24h, payload `{sub: user_id, ws: default_workspace_id, iat, exp}`. Secret is read from env `JWT_SECRET`. On register, a personal workspace is created (slug derived from `display_name`, with a numeric suffix on collision) and the default workspace's templates are copied over.

### Role-aware endpoints

`require_role("owner","admin")` is enforced on:
- `PATCH /api/settings`
- `GET /api/settings/export-all`
- `POST/PATCH/DELETE /api/providers`, `POST/PATCH/DELETE /api/providers/{id}/models`, `PATCH/DELETE /api/models/{id}`, `POST /api/models/{id}/test`
- `PATCH /api/workspaces/me/system-models` (accepts `orchestrator_model_id`, `chat_model_id`, `memory_extractor_model_id`, `quality_judge_model_id`)
- `POST/PATCH/DELETE /api/quality/rubrics`, `POST /api/quality/records/{id}/evaluate`
- `PUT /api/quality/records/{id}/feedback` (annotation write, E-05), `POST /api/quality/records/{id}/annotation-session`, `GET /api/quality/calibration`
- `POST /api/agents/{cid}/kill`, `/abort`, `/switch_model`
- `POST /api/agents/kill-all`
- `DELETE /api/templates/{id}`, `POST /api/templates/{id}/rollback/{v}`
- `DELETE /api/knowledge/documents/{id}`, `POST /api/knowledge/reset`, `PUT /api/knowledge/rules`
- `DELETE /api/scheduled-jobs/{id}`

`require_role("owner","admin","member")` (mutating, non-destructive):
- `POST /api/templates`, `PUT /api/templates/{id}`
- `POST /api/agents/{cid}/feedback`
- `POST /api/knowledge/documents`, `PUT /api/knowledge/memory`
- `POST /api/scheduled-jobs`, `PATCH /api/scheduled-jobs/{id}`

## Conventions

- All REST is under `/api/`. WS is under `/ws/`.
- Returns: JSON (REST) or JSON messages (WS).
- Time — ISO-8601, UTC.
- 200 — success; 201 — created; 204 — no content; 400 — bad request; 401 — auth; 403 — forbidden (role gate); 404 — not found; 422 — validation; 502 — agent unreachable.

## Endpoint groups

### Tasks (`/api/tasks`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/tasks?status=&parent_id=&include_experiments=` | List tasks. Benchmark children (`origin='experiment'`, SPA-40) are hidden unless `include_experiments=true` |
| POST | `/api/tasks` | Create a task in backlog. Fields: title/description/priority/parent_id/`reference_answer`? (optional gold answer for reference-based scoring, E-03)/`canonical_trajectory`? (optional gold trajectory for matching, E-09 — a list of tool names or a `{nodes, edges}` DAG)/`capability_spec`? (optional capability-isolation spec, E-13 — `{required_tools[], category?, match?}`) |
| GET | `/api/tasks/{id}` | Single task + subtasks. Every task shape carries `cost_usd` (the agent's own spend) and, separately, `orchestrator_cost_usd` + `orchestrator_usage` (`{input_tokens, output_tokens, calls, by_decision}`) — what the platform spent deciding about the task. Kept apart on purpose: the agent's tokens are what efficiency comparisons between models rest on (SPA-111) |
| PATCH | `/api/tasks/{id}` | title/description/status/priority/`reference_answer`/`canonical_trajectory`/`capability_spec` (each applied only when non-null) |
| PATCH | `/api/tasks/{id}/approve` | From `awaiting_approval` → `done` |
| PATCH | `/api/tasks/{id}/reject` | Body `{feedback}`; sets `ready`, bumps `retry_count` |
| DELETE | `/api/tasks/{id}` | Delete the task |
| GET | `/api/tasks/{id}/decomposition` | Tree + per-attempt timeline for a parent task. Returns `{parent, subtasks: [{id, title, template_name, status, retry_count, max_retries, depends_on, started_at, completed_at, cost_usd, result_files_count, attempts: [{agent_container_id, spawned_at, finished_at, outcome, error}]}]}`. Attempts are grouped by `agent_container_id` from `agent_events` (`agent_spawned`/`agent_completed`/`agent_failed`/`agent_aborted`); outcome is the last terminal event or `running` if only spawned. Used by the Decomposition view (`/graph` → Decomposition tab). |
| GET | `/api/tasks/{id}/files/{file_name:path}` | Stream one deliverable from MinIO (`tasks.result_files[]` entry). `Content-Disposition: attachment` with the original filename. 404 if the file is not in the task's `result_files` or the task is not in the workspace |
| GET | `/api/tasks/{id}/files.zip` | Workspace-scoped streaming ZIP of every `result_files` entry. Best-effort: missing or unreadable files are **skipped** (don't fail the archive). Deflated. `Content-Disposition: attachment; filename="files-<task>.zip"`. The TaskDetail "Скачать все" button only renders this when `result_files.length > 1` |

### Templates (`/api/templates`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/templates` | List. Each row includes `model_id` (FK → llm_models), denormalized `model_display_name`/`model_api_name`/`provider_name`. |
| POST | `/api/templates` | Create. Fields: name/description/soul_md/`model_id`/`tool_ids`/limits/tags. `model_id` must reference a model in the same workspace; every `tool_ids` entry must reference a registry entry in this workspace (SPA-41), else 400. (Inline `tools`/`mcp_servers` were replaced by `tool_ids` references.) |
| GET | `/api/templates/{id}` | Single |
| PUT | `/api/templates/{id}` | Update (creates a version snapshot before applying changes). Accepts `model_id`. |
| DELETE | `/api/templates/{id}` | |
| GET | `/api/templates/{id}/versions` | List versions |
| GET | `/api/templates/{id}/versions/{v}` | Snapshot v |
| POST | `/api/templates/{id}/rollback/{v}` | Apply snapshot v as the current state (creates two new versions: pre-rollback + post-rollback). Legacy snapshots with a `model` string are best-effort mapped to `model_id` via api_name. |

### Tool & MCP Registry (`/api/registry`) — SPA-41

Workspace-level source of truth for tools and MCP servers; templates reference entries by id (`tool_ids`). Secrets are stored plain (like `Provider.api_key`) and **masked on every read** — only the spawn-time resolver reveals them into the agent container env.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/registry/tools?kind=` | List entries (secrets masked), optional `kind=builtin\|mcp` filter. Each row: `{id, name, kind, config, secrets (masked), secret_keys[], enabled, description, created_by, created_at}` |
| POST | `/api/registry/tools` | **owner/admin** — register a tool/MCP. Body `{name, kind="builtin"\|"mcp", config, secrets, enabled=true, description?}`. `mcp` requires `config.command` (stdio) or `config.url` (http). 400 on duplicate name / invalid mcp config |
| GET | `/api/registry/tools/{id}` | One entry (masked) |
| PUT | `/api/registry/tools/{id}` | **owner/admin** — update `{name?, config?, secrets?, enabled?, description?}` |
| DELETE | `/api/registry/tools/{id}?force=` | **owner/admin** — delete. **409** (with the referencing template names) if any template references it, unless `force=true` (then the reference is stripped from those templates) |
| POST | `/api/registry/tools/{id}/test` | **owner/admin** — best-effort check: builtin → ok; mcp http (`config.url`) → reachability probe; mcp stdio → shape validation (live handshake runs in the agent sandbox). Returns `{ok, detail}` |

Resolution at spawn: a template's `tool_ids` (plus any `task.run_config.tools_override = {enable:[ids], disable:[ids]}`, finest-restriction-wins) are materialized into the builtin tool-name list + MCP server dicts the agent consumes.

### Experiments (`/api/experiments`) — SPA-40

A/B Matrix Harness: dataset × configuration matrix × N runs per cell, executed over the
benchmark path (`run_config.benchmark_mode` — no inline eval/approval/retries; `orchestrator:
off` cells pin the template for the engine fast path) with evaluation always on. Writes are
**owner/admin**-only.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/experiments?status=` | List (workspace-scoped) |
| POST | `/api/experiments` | Create a **draft**. Body `{name, description?, dataset, configurations[]?, axes?, n_runs_per_cell=1 (≤20), budget_limit_usd?, max_parallel?, eval_config?}`. `eval_config` is validated **as a whole** at create time (SPA-87) — an unknown key, a mistyped flag or an out-of-range value is a 400, because a key that does nothing is a lie about how the experiment was judged. `eval_config.judge_threshold` (0–10) pre-registers the outcome-judge cut-off the report's verdict×judge quadrant is drawn at: write-once and inside the revision fingerprint, so it cannot be chosen once the results are in; absent, the project default applies and the report says so. `eval_config.equivalence_margin` (SPA-62, >0 and ≤10) pre-registers the smallest difference in judge points worth calling a difference, which is what the report's equivalence verdict is tested against — write-once and inside the revision fingerprint for the same reason as the threshold, since a margin chosen after seeing the difference is a description rather than a claim; absent, the project default (0.5) is stamped at create time. `eval_config.trace` (SPA-86) sets the trim policy for this experiment's process judging — `{tool_output_token_cap, tool_args_token_cap, keep_tail_on_error, max_input_tokens}`, any cap `0` = no truncation; unknown keys and negative caps are rejected (400), because a typo would silently change what every run was judged on. `dataset`: `{source: "benchmark_suite", suite, case_ids?}` \| `{source: "tasks", task_ids[]}` \| `{source: "upload", cases[]}` (validated `{task_input:{title, description?}, case_id?, reference_answer?, rubric?, canonical_trajectory?, capability_spec?}`, ≤300 cases). Configurations: explicit list AND/OR cartesian `axes` over `{orchestrator, template_id, model_id, temperature, seed, soul_md, tools_override, memory_mode}` — expanded, validated (orchestrator:off requires `template_id`; on forbids it and `tools_override`), deduped by fingerprint, ≤24 configs, configs × cases × n ≤ 1000. Template/model/registry refs must exist in the workspace. Returns the draft + `preview`. 400 with a clear message on any invalid part; 409 on duplicate name |
| POST | `/api/experiments/preview` | Stateless estimate `{n_configs, n_cases, total_runs, est_cost_usd, est_duration_minutes, warnings[]}` (historical averages per template, workspace fallback) |
| GET | `/api/experiments/{id}` | Detail + live progress matrix: `matrix: [{config_key, case_key, counts{pending,running,success,failed,skipped}}]` + `run_totals` |
| DELETE | `/api/experiments/{id}` | **owner/admin** — delete (409 while running; cancel first) |
| POST | `/api/experiments/{id}/run` | draft → running: materializes all cells as `pending` rows and claims the first batch; the `experiment_run_tick` scheduler job (20s) drives the rest. 409 on invalid transition |
| POST | `/api/experiments/{id}/pause` / `/resume` / `/cancel` | Lifecycle. Pause stops claiming (in-flight runs finish); cancel skips unsettled cells, kills in-flight containers best-effort, keeps partial results. 409 on invalid transitions |
| GET | `/api/experiments/{id}/report?method=bt\|elo&refresh=` | Assembled report: per-config `summary` (with `excluded_contaminated` and `success_rate_basis`), `exclusions` (SPA-87: runs infrastructure decided the outcome of — provider quota, dead key, transport, harness — dropped from every aggregate and counted here, by type and by config), `judge_discrimination` (SPA-87: the RQ2 **headline**, threshold-free — the judge's score distribution split by the executable verdict plus AUC; `median_on_fail` is the over-credit number and no cut-off can move it), `rq2` (the same question as a 2×2 at the **pre-registered** threshold, now an illustration: `threshold_source` says whether the experiment committed to one, and `sensitivity` shows the neighbouring cut-offs as explicitly exploratory), `heatmap` (configs × rubric dimensions), `pareto` (quality↑ × cost↓ × time↓ frontier), `scatter` (outcome × trajectory per run), `leaderboard` (E-19 Bradley-Terry/Elo with bootstrap CI, derived from pointwise scores case-paired), `significance` (per config-pair × metric — see below), `failure_modes`, `orchestrator` on/off comparison, `axis_reliability` (SPA-76 — per-axis trust badge over the 6 E-07 trajectory axes) and `outcome_axis_reliability` (SPA-79 v13 — the same traffic light applied to the OUTCOME rubric axes actually rated by humans). **SPA-88 (schema 17)**: each axis lands in one of six zones — `reliable_absolute` (κ≥0.6) and `moderate_agreement` (0.4≤κ<0.6) may drive numeric aggregates; `rank_only` (κ below the bar but Spearman ρ≥0.5) may drive **ranks and paired comparisons only**; `insufficient` (n<3 or κ undefined), `unreliable` (κ<0.4 and ρ<0.5) and `not_calibrated` (no source) drive nothing. A parallel `trusted` block recomputes `summary.per_config` (quality/trajectory), `pareto`, `leaderboard` and `significance` from the axes that cleared the gate — the raw sections above are left untouched — plus `outcome_axes`/`trajectory_axes` (the numeric / rank-only / excluded split, with κ, ρ and n per axis) and `dropped` (how many «significant» rows the gate removed or demoted, and which metrics). `trusted.available` is `false` when nothing the gate cleared can actually be shown — no calibration source at all, or only a rank-rescued *trajectory* axis, which earns no per-axis rank test and so drives nothing here. The trusted `leaderboard` runs on the **numeric** set only (`basis: numeric_trusted_axes`) — it is built from a weighted mean, and a mean is a magnitude a rank-rescued axis cannot support. The report also carries a `calibration_fingerprint`; the cache is served only while it still matches, so a human rating arriving after a report was cached recomputes it. Every `significance` row in both views carries `axis` (which axis it rests on and how far that axis is trusted) and `rank_only` — a rank-only metric is judged on Mann-Whitney with `welch: null`, because Welch compares means and a scale-shifted judge cannot support that. **SPA-62 (schema 18)**: the matrix runs the same cases across configs, so each `significance` row is **paired by case** — samples are collected as `config → metric → case_key → value`, one value per (config, case) cell averaged over that cell's repeated runs, because repeated runs of one case are not independent observations. A row carries `design` (`paired` with `n_pairs`, or `unpaired` when the two configs share fewer than `min_cases`) — and the design never changes because an inference is unavailable, so a paired row whose t-test has no variance to work with falls through to an exact **sign test** (`primary_test: sign`) rather than answering an unpaired question instead, and two configs that scored identically on every case report `primary_test: identical` with p = 1.0. A `rank_only` row carries `magnitudes_withheld: rank_only_axis` and null `effect`/`ci`/`power`/`equivalence`: every one of those is a claim about size, and an axis trusted for order alone cannot make one. Its paired verdict comes from the **sign test**, not Wilcoxon — the signed-rank test ranks the magnitudes of the differences and so is not invariant to the rescaling a rank-only axis explicitly permits; Wilcoxon stays in the row as a diagnostic. Beyond that: `primary_test`, `unpaired_cases` (cases only one side finished — case-level survivor conditioning, named rather than dropped), `effect` + `effect_kind` (Cohen's d_z paired / Hedges' g unpaired — different estimands, never comparable), `ci` (95% percentile bootstrap over cases), `power` (`mde` = what this n could have detected, `n_required` = what the observed difference would have needed), and `equivalence` (TOST against `eval_config.equivalence_margin`). `significant` is decided on the Benjamini-Hochberg **q**, with `significant_uncorrected` kept beside it; correction runs **within** a family (`confirmatory` = `weighted_score`/`trajectory_score`, `exploratory` = every `dim:*`) and never across, so a screen's size is not charged to the headline. `significance_correction` reports `n_tests` per family, how many rows lost significance to the correction, and `n_omitted`/`omitted` — comparisons that were not testable at all (fewer than `min_cases` shared cases), stated so an empty table is not read as a null result. Raw and `trusted` are corrected independently. `report.estimand` names the population the numbers describe (`success_runs`, unit `case_cell_mean`) and counts what each config lost to non-success. Intervals arrive on the point estimates that decide things: `cohen_kappa_ci` on every calibration dimension and on the reliability axes (carried, **not** acted on — the gate's cut-offs are unchanged), `auc_ci` on `judge_discrimination`, and Wilson intervals on `rq2.agreement`/`over_credit_rate`, `external.pass_rate` and `summary.success_rate`. **SPA-114 (schema 20)**: the `effort` block separates the tokens a model spent *thinking* from the tokens it spent *writing* — `reasoning_tokens_mean` and `reasoning_share` (of OUTPUT, since reasoning tokens are billed inside `completion_tokens`), with `reasoning_available` distinguishing «no run reported a split» from «the share is zero». The E-07 profile gains `reasoning_shown` and `n_reasoning_steps` (**trajectory schema 4→5**): whether the judge was shown the model's own deliberation is a condition of the verdict, like `files_only` on the outcome judge, and is set per experiment by `eval_config.trajectory_show_reasoning` (default true). **SPA-111 (schema 19)**: `cost_breakdown` gains an `orchestrator` column beside `agent` and the judges — the three orchestrator decision calls (template selection, decomposition, result evaluation) never read `usage`, so every cost figure the platform reported was an undercount of unknown size and the budget cap only counted part of the spend. `orchestrator_metered` distinguishes a config that spent nothing on orchestration from a corpus recorded before the meter existed, which both show as a zero. `quality_gate` gains `n_uncertifiable` per config and overall: runs that failed the gate because a critical dimension could not be scored **at all** — the provider treated the judge's forced tool call as advisory and answered in prose. The verdict is unchanged (SPA-51 still fails an uncertifiable critical dimension closed), but a config whose pass rate is depressed this way is being under-measured, not out-performed. **SPA-84 (schema 16)** adds `input_revision` / `input_fingerprint` (what the report was computed from), `selection`, and `config_drift` (configs whose frozen resolution — model `api_name`, template content hash, agent image id — no longer matches reality). `selection=latest_valid\|all_attempts\|first_attempt` chooses which executions of each cell are counted; only the default is cached, and the cache is served only while `input_revision` and `input_fingerprint` still match the experiment. Cached on the experiment once terminal; running → fresh `partial` report |
| GET | `/api/experiments/{id}/results?config=&case=&run_index=&include_retired=` | Per-cell run rows + task state + quality/trajectory profiles + E-20 fingerprint, plus `attempt_count` and `retired_at` (SPA-84). Cells of a retired configuration are excluded unless `include_retired=true` — their lineage is kept, and this is the way back to it. Each row carries `failure_type` and `contaminated` (SPA-87): raw rows are never filtered — this is the ledger — so the mark is how a consumer tells a run infrastructure decided from a weak result |
| POST | `/api/experiments/{id}/clone` | **owner/admin** — new draft from this experiment; body `{name?, changes?}` (partial create payload; the frozen dataset is copied verbatim unless `changes.dataset` is given). Re-run = clone + run |
| GET | `/api/experiments/{id}/export?format=json\|csv` | Flat per-run rows (pandas-friendly): config axes, scores incl. `dim_<key>` columns, cost, duration, task id, repro fingerprint, plus `failure_type` / `contaminated` columns |
| POST | `/api/experiments/{id}/retry-failed` | **owner/admin** — re-queue every `experiment_run` row currently in `failed` back to `pending`, so the next `experiment_run_tick` re-claims them. Each cell's current state is first archived to `experiment_attempts` (SPA-84) and its superseded task re-tagged `exp:<id>:retired`; cells of a retired config are skipped. Bumps `revision`, drops the cached report. Idempotent — a no-op leaves the revision alone. **409** unless the experiment is terminal or paused |
| POST | `/api/experiments/{id}/add-config` | **owner/admin** — append a new configuration to a *running*, *paused* or terminal experiment (rejects `draft`). Body `{label?, orchestrator?, template_id?, model_id?, temperature?, seed?, soul_md?, tools_override?, memory_mode?}` (validated + fingerprinted the same way as `POST /api/experiments`, now including `_validate_config_refs`); new cells are pre-created as `pending` and drained by the existing tick, and the config records its resolved state. `config_key` is never reused, retired keys included. Bumps `revision`. **409** on duplicate fingerprint, unknown template/model reference, or invalid transition |
| DELETE | `/api/experiments/{id}/configs/{config_key}` | **owner/admin** — **retire** a configuration (SPA-84: no longer a delete). Stamps `retired_at` on the entry and on its `experiment_runs` rows, settles anything still in flight, archives each cell to `experiment_attempts` and re-tags its tasks `exp:<id>:retired`. The lineage is kept and remains reachable via `?include_retired=true`; the config leaves the matrix, the report and every aggregate. Bumps `revision`. **409** while running, on the last live configuration, or if already retired |

### Agents (`/api/agents`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agents` | List active containers |
| GET | `/api/agents/{cid}` | Stats |
| POST | `/api/agents/{cid}/kill` | |
| POST | `/api/agents/kill-all` | Kill switch |
| GET | `/api/agents/{cid}/health` | Forwarded from the agent's `:8080/health` (uptime/iteration/tokens) |
| POST | `/api/agents/{cid}/feedback` | Body `{message}` → injected as a user message into the agent loop |
| POST | `/api/agents/{cid}/switch_model` | Body `{model_id}` — resolved server-side to (provider, model); creds are forwarded to the agent. |
| POST | `/api/agents/{cid}/abort` | Body `{reason}` → the agent finishes its loop with `event=aborted` |

### Webhooks (`/api/v1/agent-webhook`, legacy `/api/agent-webhook`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/agent-webhook/{task_id}` | Canonical. Pydantic validation (see `webhooks.md`). 422 on invalid event/data. Requires `Authorization: Bearer $SPAWNHIVE_AGENT_TOKEN` and `idempotency_key`. |
| POST | `/api/agent-webhook/{task_id}` | Legacy alias. Adds `Sunset: Sat, 01 Aug 2026 00:00:00 GMT`, `Deprecation: true`, `Link: rel="successor-version"` headers (even on 401/422 responses). |

### Events (`/api/events`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/events?task_id=&event_type=&source=&agent_container_id=&from_dt=&to_dt=&limit=&offset=` | Append-only log. `agent_container_id` filter narrows to a single agent (added for live agent-card and graph history-replay). |
| GET | `/api/events/export/{task_id}` | JSON download covering the entire task lifecycle |
| WS | `/ws/events` | Real-time. The client sends JSON `{task_id?, source?, event_type?, agent_container_id?}` to set filters. |
| WS | `/ws/agents/{container_id}` | Events for a single container only (P12) |

### Agent terminal logs (`/api/v1/agent-log`, `/api/tasks/{id}/log`)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/agent-log/{task_id}` | Agent → orchestrator chunk ingest. Bearer agent_token + idempotency. Body: `{chunk_seq, content (≤256 KB), tool_name?, arguments?, arguments_truncated?, tool_call_id?, part_index=0, part_total=1, idempotency_key}`. Returns `{status: ok\|duplicate, chunk_seq}`. Auto-remap of chunk_seq on retry collision. `arguments` is the call's parameters (SPA-86) — re-clipped server-side, shortening long string values while keeping every key; `tool_call_id` + `part_index` identify the parts of one split output so the cleaned trace joins them into a single step. `reasoning` (≤64 KB, SPA-114) is the model's own deliberation preceding this step, stored in its own column and never merged into `content`; the final turn carries it on a chunk with no tool and no output, which the cleaner surfaces as a `model_reasoning` step rather than as a nameless tool call. | Archived to MinIO with `created_at` so the cleaned trace can be re-ordered after compaction (SPA-113).
| GET | `/api/tasks/{task_id}/log?from_seq=&limit=` | Workspace-scoped paginated log. Branches by `tasks.log_archive_s3_path`: live → DB chunks; archived → MinIO blob. Returns `{archived, archive_path, chunks: [{id, chunk_seq, content, tool_name, created_at}]}`. |
| WS | `/ws/tasks/{task_id}/log` | Live broadcast of new chunks. Filter `_kind=log_chunk`; payload mirrors GET-chunk shape with wire `type: "log_chunk"`. |

### Chat (`/api/chat`, `/ws/chat`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/chat/history?limit=` | |
| WS | `/ws/chat` | Streaming. Slash commands (`/help`, `/status`, `/spawn …`, `/kill …`, `/templates`, `/tasks`, `/board`) are handled without an LLM; otherwise the request goes to the LLM with CHAT_TOOLS (`create_task` / `update_memory` / `search_knowledge`). |

### Knowledge (`/api/knowledge`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/knowledge/rules`, `/memory` | content of rules.md / memory.md |
| PUT | `/api/knowledge/rules`, `/memory` | replace content |
| GET | `/api/knowledge/documents` | List |
| POST | `/api/knowledge/documents` | multipart upload (.pdf/.docx/.md/.txt) |
| DELETE | `/api/knowledge/documents/{id}` | |
| POST | `/api/knowledge/search` | body `{query, limit}` |
| POST | `/api/knowledge/reset` | drop Qdrant collections (docs + memory_entities) + delete docs/entities |

### Memory (`/api/memory`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/memory/entities?type=&search=&limit=` | List entities |
| GET | `/api/memory/entities/{id}` | Detail + relations |
| POST | `/api/memory/entities` | Create (with dedup ≥ 0.92) |
| PATCH | `/api/memory/entities/{id}` | Update fields |
| DELETE | `/api/memory/entities/{id}` | |
| GET | `/api/memory/relations?from_id=&to_id=` | List |
| POST | `/api/memory/relations` | |
| DELETE | `/api/memory/relations/{id}` | |
| POST | `/api/memory/extract?task_id=` | Manually trigger LLM extraction for an existing task |

### Analytics (`/api/analytics`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/analytics/templates?period=day\|week\|month\|all&from_dt=&to_dt=` | Per-template aggregates |
| GET | `/api/analytics/configs?period=day\|week\|month\|all&from_dt=&to_dt=` | Per-config aggregates across the workspace's experiments — one row per (experiment, `config_key`), the config-level A/B unit people actually run (vs the legacy per-template view). Each row: `{config_id ("{experiment_id}:{config_key}"), config_name, run_count, success_rate, failure_rate, quality_mean, trajectory_mean, pass_rate (external_verdict), avg_time_seconds, avg_cost_usd, contaminated}`, sorted by `run_count` desc. Every metric is `null` when there is no clean sample left — absent is not zero, and a zero here lost comparisons on the strength of an outage. Runs whose outcome infrastructure decided are excluded from every average and counted in `contaminated` instead (SPA-87) — the same population the experiment report uses, since an exclusion that holds only in the report is not one |
| GET | `/api/analytics/timeline?days=` | Daily roll-up |
| GET | `/api/analytics/models?period=` | Per-model |

### Quality Data Lake (`/api/data-lake`) — E-01

Workspace-scoped, read-only. Records are immutable per-task execution snapshots
(summary in Postgres, full blob in MinIO).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/data-lake/records?template_id=&model_used=&final_status=&title_contains=&from_dt=&to_dt=&limit=&offset=` | Filterable list of record summaries |
| GET | `/api/data-lake/records/{task_id}` | `{summary, record}` — `record` is the full blob from MinIO (404 if not in workspace) |
| GET | `/api/data-lake/query?group_by=template_name\|model_used\|final_status&...filters` | Group-by aggregates: count, avg_cost_usd, avg_tokens, avg_duration_s, approval_rate |
| GET | `/api/data-lake/export?format=json\|parquet&...filters` | **owner/admin** — bulk export of the flattened summary table |

### Quality Rubric Engine (`/api/quality`) — E-02

Workspace-scoped. Rubrics define quality dimensions (LLM-as-judge); the engine
scores a finished task into a profile written to `quality_records.quality_profile`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/quality/rubrics` | List the workspace's rubrics |
| POST | `/api/quality/rubrics` | **owner/admin** — create. Body: `{name, description?, applies_to?, is_default?, dimensions: [{key, name, description?, evaluator, reference_mode?, probe?, weight?, threshold?, critical?}]}` |
| GET | `/api/quality/rubrics/{id}` | Get one (404 if not in workspace) |
| PATCH | `/api/quality/rubrics/{id}` | **owner/admin** — partial update |
| DELETE | `/api/quality/rubrics/{id}` | **owner/admin** |
| GET | `/api/quality/records/{task_id}/profile` | `{task_id, quality_profile}` (404 if no record in workspace; `quality_profile` is null until evaluated) |
| POST | `/api/quality/records/{task_id}/evaluate` | **owner/admin** — on-demand evaluate (re-runs/overwrites). Returns `{quality_profile, skipped, detail?}`; `skipped=true` when no rubric matched or no judge/orchestrator model is configured |
| GET | `/api/quality/records/{task_id}/review` | "Everything a human annotator needs to judge a result" (SPA-71): the task prompt (`title`/`description`), `reference_answer` if any, `result_summary`, and text excerpts of the deliverable files (`text` + `markdown` for converted docx/pdf/xlsx via SPA-71 conversion; `binary:true` when only a raw preview is unavailable). The same material the judge sees — lets the Calibration UI show what is being rated, not just the rubric axes. Workspace-scoped; up to 6 files |
| GET | `/api/quality/records/{task_id}/feedback` | `{task_id, human_feedback}` — the **latest human** annotation (E-05) materialised into `quality_records.human_feedback`, or null (404 if no task in workspace). Readable by any workspace member: annotation stays visible to everyone who can see the task; only the write is gated. This is the sighted view — a blind annotator reads the same material through the session bundle |
| PUT | `/api/quality/records/{task_id}/feedback` | **owner/admin** — append a rating to the annotation ledger (SPA-85; no longer an overwrite). Body `{verdict?: approve\|reject, overall_comment?, dimensions: [{key, name?, score 0-10, comment?}], session_id?, annotator_type?: human\|llm_judge\|synthetic, annotator_label?, blind_to_model?, blind_to_judge?}`. Rating the same task again **as the same annotator** supersedes that annotator's own previous row; a different annotator adds an independent row, so two people's ratings coexist. `annotator_type` defaults to `human` (the caller, with their user id); the machine types carry `annotator_label` and no user id. **The blindness flags are ignored for `human` ratings** — they come from the `session_id`'s row (checked against the caller and the task, and single-use). Omitting `session_id` makes no claim: the rating is recorded as sighted. Passing one that is unknown, someone else's, for another run or in another workspace is a **409**, not a silent downgrade to sighted — identity is checked **before** any rating is looked up, so replaying a foreign session id can never return that session's feedback. Re-sending a request whose *own* session already produced a rating returns that rating unchanged (idempotent retry); a session already used with no rating attached is also a 409. Only a scripted annotator, which owns its own protocol, may declare the flags directly. Builds the quality record on demand, freezes the judge observation, and refreshes the materialised slot for human ratings; returns `{task_id, human_feedback}` |
| GET | `/api/quality/records/{task_id}/annotations` | `{task_id, annotations}` — the append-only ledger for a task, oldest first (SPA-85). Each row `{id, task_id, annotator_type, annotator_id, annotator_label, protocol_version, blind_to_model, blind_to_judge, verdict, overall_comment, dimensions, judge_observation, supersedes_id, session_id, created_at}`. A row is *current* unless some other row names it in `supersedes_id`; `session_id` is the annotation session that produced it — the evidence behind its blindness flags |
| POST | `/api/quality/records/{task_id}/annotation-session` | **owner/admin** — open an annotation session and get everything needed to rate this run in one payload (SPA-85). Body `{blind?: bool}` declares the protocol **before** anything is fetched; the response is `{session_id, task_id, protocol:{protocol_version, blind_to_judge, blind_to_model}, review, quality_profile, trajectory_profile, human_feedback, annotations, model_used}`, built to match it in one place — under `blind` the judge's scores, reasoning, gate, frozen observations and `model_used` are simply absent, so the protocol cannot be half-applied. `human_feedback` is **this caller's own** current rating (or null), never the materialised slot, and other annotators' ledger rows **always** come back with `redacted: true` and no scores/verdict/comment — otherwise the second annotator edits the first one's form and the inter-annotator κ measures agreement with a pre-filled value. They are not revealed after you rate either: that left re-annotation open, and since the collector takes each annotator's *current* row, the dependent re-rating would replace the independent one. Peers' opinions are read outside the session, via `GET …/annotations` and the calibration export. Every session records `blind_to_peers: true` on the rating it produces. Submitting a rating with this `session_id` records the protocol from the session row and **consumes** it (single-use, enforced by a unique index on `annotations.session_id` plus a row lock). What the flag then means, precisely: «this rating was produced through a session that was served no judge scores» — **not** that the annotator has never seen the judge, which the server cannot establish since scores also reach a client from the evaluate endpoints, the experiment-results payload and the analytical surfaces |
| GET | `/api/quality/calibration` | **owner/admin** — flattened judge-vs-human pairs (one row per rated dimension across the **current** human/legacy annotations; superseded rows are excluded): `{task_id, annotation_id, annotator_type, annotator_id, protocol_version, blind_to_model, blind_to_judge, blind_to_peers, dimension_key, dimension_name, judge_score, judge_source, human_score, band, judge_reasoning, human_comment, verdict, judge_gate_passed, submitted_by, submitted_at}`. `judge_source` ∈ `frozen` (the score came from the observation stored on the annotation) \| `unscored` (the judge had **not** scored that axis when the human rated it — the absence is frozen too, so a later judge run cannot fill it in) \| `live` (re-read from the current profile; reachable only for pre-ledger `legacy` rows, which froze nothing). A **verdict-only** annotation yields one row with `dimension_key: null`: it carries no per-dimension scores but is still a rating by a person, so it stays in `n_humans`, `n_annotations` and the verdict agreement. Calibration input for E-17 — shares its row-building with the E-17 report via `collect_judge_human_pairs` |
| GET | `/api/quality/calibration/queue?status=pending\|done\|all&limit=&blind=` | Annotation queue (SPA-52) — every `quality_records` row that has a `quality_profile`, in `created_at desc` order. `status=pending` (default) returns only the ones still missing `human_feedback`; `done` only rated; `all` returns both. Each item: `{record_id, task_id, title, origin, has_quality_profile, has_human_feedback, weighted_score, created_at}`. Includes `origin='experiment'` tasks, which the board hides — this is the only UI path to annotate experiment children. `blind=true` withholds `model_used` and `weighted_score`: the queue is the first thing an annotator sees, so a blind campaign must not have them sitting on the row. It is a convenience for the annotation surface — what a rating records comes from its session |
| GET | `/api/quality/records/{task_id}/trace` | Cleaned, judge-ready trajectory (E-06) — input for the trajectory judge (E-07). Each tool step carries the **call** (name + `arguments`) as well as its result (SPA-86). Query `tool_output_token_cap` and `tool_args_token_cap` (default 600 / 400): **`0` = no truncation at all**, otherwise 50–50000 — a value in between is a 422 rather than a silent clamp. `keep_tail_on_error` (bool). Returns `{task_id, cleaned_trace}`; computed on demand, not persisted (404 if no task in workspace) |
| GET | `/api/quality/records/{task_id}/trajectory` | `{task_id, trajectory_profile}` — 6-axis trajectory profile (E-07) or null until judged (404 if no record in workspace) |
| GET | `/api/quality/records/{task_id}/external-checker` | Toolathlon executable-checker detail (SPA-55, `gold.external_eval`): `available` bool + `verdict` (`pass`/`fail`/null) + `case_key`/`config_key`/`label`/`launch_time` + the eval and preprocess container log tails (so a user can see **why** the checker failed — the material backing the "checker is itself unreliable" narrative). `available=false` for plain (non-checker) runs and for the legacy serial path. Workspace-scoped via the parent experiment |
| POST | `/api/quality/records/{task_id}/evaluate-trajectory` | **owner/admin** — on-demand trajectory judge (re-runs/overwrites). Returns `{trajectory_profile, skipped, detail?}`; `skipped=true` when the trajectory has no steps or no judge/orchestrator model is configured. Profile carries `axes:[{key,name,score 0-10,reason}]` (6), `overall_score`, `loop_detected`, `summary`, `judge_*`, `input_capped`, `status` |
| GET | `/api/quality/records/{task_id}/trajectory-evidence` | `{task_id, trajectory_evidence_profile}` — TRACE evidence-bank profile (E-08) or null until judged (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-trajectory-evidence` | **owner/admin** — on-demand TRACE evidence-bank judge (re-runs/overwrites; `N+1` LLM calls). Returns `{trajectory_evidence_profile, skipped, detail?}`; `skipped=true` when the trajectory has no steps or no judge/orchestrator model is configured. Profile carries the same 6 `axes` + `overall_score`/`loop_detected`/`summary` as E-07, plus `groundedness` (0-1), `redundant_steps`, `evidence_bank:[{seq,kind,tool_name,redundant,grounded,progress,execution,facts[],note,error?}]`, `judge_calls`, `judge_*`, `input_capped`, `status` |
| GET | `/api/quality/records/{task_id}/trajectory-match` | `{task_id, trajectory_match_profile}` — deterministic trajectory-match profile (E-09) or null until matched (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-trajectory-match` | **owner/admin** — on-demand, **LLM-free** trajectory match (re-runs/overwrites). Returns `{trajectory_match_profile, skipped, detail?}`; `skipped=true` when the task has no `canonical_trajectory`. Profile carries `mode` (exact\|edit\|dag), `score`, `matched`, `threshold`, `metrics:{exact,edit,dag}`, `actual_sequence[]`, `reference_sequence[]`, `reference_form` (sequence\|dag), `detail`, `trace_stats:{steps_total,tool_steps}`, `status`. A bad/unparseable canonical → `status:"error"` (not skipped) |
| GET | `/api/quality/records/{task_id}/capability` | `{task_id, capability_profile}` — deterministic capability-isolation profile (E-13) or null until evaluated (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-capability` | **owner/admin** — on-demand capability-isolation harness (E-13; Glass-Box matching is LLM-free, but outcome correctness reuses the E-02 judge, running it once if no profile exists). Returns `{capability_profile, skipped, detail?}`; `skipped=true` when the task has no `capability_spec`. Profile carries `category`, `required_tools[]`, `match` (all\|any), `tools_called[]`, `tool_used`, `missing_tools[]`, `outcome_correct`, `outcome_signal` (reference\|judge\|none), `outcome_score`, `outcome_threshold`, `classification` (genuine\|cheated\|failed_with_tool\|failed_no_tool), `capability_passed`, `trace_stats`, `status`. The **cheated** cell = correct outcome but the required tool was not used |
| GET | `/api/quality/capability/aggregate?category=&model_used=&template_id=&suite=` | Aggregate capability profiles across the workspace into `capability_score = genuine/total`, with `by_category`/`by_model`/`by_template` breakdowns (the model breakdown is the "compare models by capability" view). Each bucket carries the four-cell counts + `total` + `capability_score`. `suite` restricts to one Benchmark Case Store suite |
| GET | `/api/quality/records/{task_id}/failure-modes` | `{task_id, failure_profile}` — multi-label failure-mode classification (E-14) or null until evaluated (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-failure-modes` | **owner/admin** — on-demand failure-mode classification (E-14; one LLM call, reuses the E-02/E-07 judge model). Returns `{failure_profile, skipped, detail?}`; `skipped=true` when there is no judge model or the task has an empty trace. Profile carries `failures:[{class,confidence,reason}]` (classes: tool_confusion\|parameter_blind\|loop\|premature_stop\|hallucinated_tool_result\|ignored_error; empty list = clean run), `summary`, `judge_model`, `judge_*_tokens`, `judge_cost_usd`, `input_capped`, `used_outcome_profile`, `used_trajectory_profile`, `trace_stats`, `status`. A correct outcome does not preclude failure labels (e.g. a loop) |
| GET | `/api/quality/failure-modes/aggregate?model_used=&template_id=&failure_class=&suite=` | Aggregate failure profiles across the workspace into per-class distributions, with `by_class`/`by_model`/`by_template` breakdowns — the "distribution of failure types per (model, template)" view. Each bucket carries `runs_total`, `failure_runs`, `by_class:{cls:count}`, `failure_rate`, and per-class `rate:{cls:count/runs_total}`. `failure_class` narrows to runs carrying that class; `suite` restricts to one Benchmark Case Store suite |
| GET | `/api/quality/records/{task_id}/hallucinations` | `{task_id, hallucination_profile}` — 4-category deliverable fact-check (E-15) or null until evaluated (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-hallucinations` | **owner/admin** — on-demand hallucination fact-check (E-15). Checks `task.result_summary` against the E-06 cleaned trace: URLs/code-fence API symbols deterministically (in-trace only), numbers/claims/unconfirmed APIs via one LLM call (reuses the E-02/E-07 judge model; 0 calls if nothing to ask). Returns `{hallucination_profile, skipped, detail?}`; `skipped=true` when there is no judge model, no deliverable, or an empty trace. Profile carries `categories:{urls,apis,numbers,citations}` each `{checked,hallucinated,items:[{value\|claim,kind:deterministic\|llm,supported,reason,confidence?}]}`, `hallucination_count`, `items_total`, `hallucination_rate` (count/items_total; 0 = clean), `summary`, `judge_model`, `judge_*_tokens`, `judge_cost_usd`, `input_capped`, `used_outcome_profile`, `used_trajectory_evidence`, `trace_stats`, `status`. A correct outcome does not preclude hallucinations (e.g. an invented URL) |
| GET | `/api/quality/hallucinations/aggregate?model_used=&template_id=&category=&suite=` | Aggregate hallucination profiles across the workspace into per-category distributions, with `by_category`/`by_model`/`by_template` breakdowns — the "hallucination rate per (model, template)" view. Each bucket carries `runs_total`, `hallucinated_runs`, `hallucinated_run_rate`, and per-category `{checked,hallucinated,rate}`. `category` narrows to runs with ≥1 hallucination in that category; `suite` restricts to one Benchmark Case Store suite |
| GET | `/api/quality/records/{task_id}/calibration` | `{task_id, calibration_profile}` — confidence-calibration pair (E-16) or null until evaluated (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-calibration` | **owner/admin** — on-demand confidence-calibration probe (E-16). One post-hoc self-probe on the task's own model (resolved by `model_used`, falling back to the E-02/E-07 judge) re-reads the task + `result_summary` + E-06 cleaned trace **without the grader's verdict** and reports `P(correct) ∈ [0,1]`; this is paired with E-02 correctness (`_outcome_from_profile`; reference dim, else `weighted_score ≥ calibration_outcome_threshold`). Returns `{calibration_profile, skipped, detail?}`; `skipped=true` when no model is resolvable, there is no deliverable, or the E-02 profile has no correctness signal. Profile carries `predicted_confidence`, `actual_correct`, `outcome_signal` (reference\|judge), `outcome_score`/`outcome_threshold`, `brier_term` ((conf−actual)²), `confidence_source` (self_probe), `probe_model`, `reasoning`, `judge_*_tokens`, `judge_cost_usd`, `input_capped`, `used_outcome_profile`, `trace_stats`, `status`. ECE/Brier/reliability are aggregate-only (see below), not per-task |
| GET | `/api/quality/calibration/aggregate?model_used=&template_id=&suite=&bins=` | Aggregate calibration profiles across the workspace into **ECE / Brier / a reliability diagram**, with `overall`/`by_model`/`by_template` breakdowns — the "is model X over/under-confident" view. Each bucket carries `count`, `ece` (Σ non-empty buckets of `(count/total)·|avg_conf−accuracy|`), `brier`, `accuracy`, `avg_confidence`, `overconfidence` (avg_conf−accuracy), and `reliability:[{lo,hi,count,avg_confidence,accuracy}]` over `bins` (default 10, 2..20) equal-width confidence bins. `recommendations[]` give a per-model plain-language verdict ("model X overestimates itself in the 70–80% confidence zone"); `suite` restricts to one Benchmark Case Store suite |
| POST | `/api/quality/judge-calibration/run` | **owner/admin** — validate the LLM judge (E-02) against human feedback (E-05) over stored scores (Judge Calibration Protocol, E-17). **No LLM call.** Body `{suite?, template_id?}` scopes the population. Computes per-dimension agreement and persists the next versioned report keyed on the judge model's `api_name`. Returns the serialized row `{id, judge_config_key, judge_model, version, sample_size, n_dimensions, threshold_kappa, passed, filters, created_by, created_at, metrics}`, where `metrics = {dimensions:[{key,name,n,pearson,spearman,cohen_kappa,mean_bias,reliable,status}], overall:{n,cohen_kappa,agreement_pct,reliable}, inter_annotator, recommendations[], sample_size, n_records, n_humans, n_annotators, n_annotations, n_legacy, judge_frozen_pct, threshold_kappa}`. A dimension is `reliable` when its band κ ≥ `judge_calibration_min_kappa` (default 0.6); `status` is `ok` or `insufficient_data` (n<3). SPA-85: `n_humans` counts **people** (distinct `annotator_id` of type `human`), `n_legacy` the pre-ledger ratings that are attributable to nobody, and `judge_frozen_pct` the share of pairs whose judge side came from a frozen observation. `inter_annotator = {available, n_records, n_annotators, dimensions:[{key,name,n,cohen_kappa,agreement_pct,reliable,status}], overall:{n,cohen_kappa,agreement_pct,reliable}}` is agreement **between annotators** on runs rated more than once, κ pooled over every unordered annotator pair, and computed **only over ratings with `blind_to_peers`** — two ratings seeded from each other agree by construction. `available:false` until some run carries a second such annotator. The overall judge-vs-human verdict agreement counts one pair per (task, annotator) |
| GET | `/api/quality/judge-calibration?judge_config_key=&history=` | Latest calibration report (same shape as the run response), or null if never run. With `history=true` returns `{latest, history:[…newest first…]}`. `judge_config_key` filters to one judge model's version line |
| GET | `/api/quality/judge-calibration/badge` | Compact trust badge: `{calibrated, n_humans, n_legacy, judge_frozen_pct, inter_annotator_kappa, inter_annotator_records, sample_size, overall_kappa, judge_config_key, version, passed, created_at}` (or `{calibrated:false}` until the first run) — renders "judge calibrated against N humans, κ=X.X", with the legacy count shown separately rather than inflating N. SPA-79 adds a rank-aware `directional` rescue: an axis with κ<0.4 but Spearman ρ≥0.5 keeps its ordering trustworthy (usable for A/B comparisons, not absolute scores) instead of being marked `unreliable` |
| POST | `/api/quality/bias-report/run` | **owner/admin** — Bias Mitigation Toolkit (E-18). Controlled A/B re-judge: re-scores every calibration-set task (records with human feedback) with the prompt-level mitigations OFF then ON and compares agreement-with-human. **Spends LLM calls** (`2 × judge-dims-with-feedback`); on-demand only. Body `{suite?, template_id?, verbosity?, score_clustering?, self_preference?, position?}` — toggle booleans override the saved `bias_mitigation_*` settings for the "after" pass (default: saved settings, or a full A/B if none are on). Persists the next versioned report keyed on the judge model's `api_name`. Returns the serialized row `{id, judge_config_key, judge_model, version, sample_size, n_dimensions, threshold_kappa, passed, filters, created_by, created_at, metrics}`, where `metrics = {status, before, after, dimensions_delta[], overall_delta, diagnostics, toggles_requested, n_records, sample_size, …}`. `before`/`after` reuse the E-17 metrics shape; `diagnostics` carries `verbosity` (length↔score correlation off/on/human), `score_clustering` (score spread off/on), `self_preference` (judge==agent flag + warning) and `position_bias` (`status:"n/a"`, reserved for pairwise / E-21). `passed` = overall agreement improved |
| GET | `/api/quality/bias-report?judge_config_key=&history=` | Latest bias report (same shape as the run response), or null if never run. With `history=true` returns `{latest, history:[…newest first…]}`. `judge_config_key` filters to one judge model's version line |
| POST | `/api/quality/ranking/run` | **owner/admin** — Aggregation Engine (E-19). Ranks models/templates from pairwise matches via Bradley-Terry or Elo with bootstrap confidence intervals, and persists the next versioned leaderboard. **No LLM call.** Body `{subject="model"|"template", method="bt"|"elo", suite?, matches?:[{player_a,player_b,outcome:"a"|"b"|"tie",weight=1}]}`. When `matches` is omitted they are **derived** from stored pointwise scores (same `benchmark_case_id`, higher mean `weighted_score` wins; gap ≤ `ranking_tie_epsilon` → tie) — the bridge until true pairwise (E-21); supplying `matches` is the literal `rank(pairwise_results)` API. Versioned per `ranking_key = "{subject}:{method}"`. Returns the serialized row `{id, ranking_key, subject, method, version, n_players, n_matches, passed, filters, created_by, created_at, metrics}`, where `metrics = {status, method, subject, source, n_players, n_matches, players[], params, derivation?}` and each `players[]` = `{player, rating, ci_low, ci_high, rank, wins, losses, ties, n_matches, win_rate}`. `passed` = a leaderboard was produced (`status=="ok"`) |
| GET | `/api/quality/ranking?ranking_key=&history=` | Latest leaderboard for a `ranking_key` (`{subject}:{method}`), or null if never run. With `history=true` returns `{latest, history:[…newest first…]}` |
| GET | `/api/quality/ranking/badge` | Compact badge: `{ranked, ranking_key, subject, method, version, n_players, n_matches, status, top_player, created_at}` (or `{ranked:false}` until the first run) |
| GET | `/api/quality/records/{task_id}/reproducibility` | Reproducibility Snapshot (E-20). The `experiment_snapshot` captured for a task's run, or `null` if not captured. Returns `{task_id, reproducibility}`. The snapshot is `{schema_version, captured_at, determinism, content, manifest, fingerprint}` — `determinism` is the fingerprinted core (`model_api_name, temperature, seed, template_*, tools[], mcp_servers[], soul_md_sha256, memory_context_sha256, flat_memory_sha256, rag, tool_versions, task_input{title, *_sha256}`), `content` keeps the raw-capped text (soul_md / memory / task input), `manifest` is the honest `{captured[], missing[], notes{}}`. 404 if the task has no quality record |
| POST | `/api/quality/records/{task_id}/capture-reproducibility` | **owner/admin** — (re)capture the snapshot into the task's quality record. Returns `{task_id, reproducibility, skipped}` (`skipped=true` with `reproducibility:null` when the run has no captured execution context). **No LLM call** |
| GET | `/api/quality/reproducibility/diff?task_a=&task_b=` | Diff two tasks' snapshots — what changed between the runs. Returns `{fingerprint_a, fingerprint_b, identical, added{}, removed{}, changed{path:{from,to}}, summary}` (keyed by dotted determinism path). 404 unless **both** tasks have a snapshot |
| POST | `/api/quality/records/{task_id}/replay` | **owner/admin** — replay a run from its snapshot: clone the task with a `run_config` derived from the captured state (pins `template_id`; passes `soul_md`/`seed`/`temperature` where captured), linked via `replay_of_task_id`. Returns `{replay_task_id, source_task_id, run_config, fingerprint}`. 404 if the task or its snapshot is missing |
| POST | `/api/quality/variance` | **owner/admin** — start a Variance / Robustness run (E-11). Body `{source_task_id?, spec?:{title,description?,reference_answer?}, n=10 (2..50), parallel=true, cost_cap_usd?, template_id?}` — exactly one of `source_task_id` (replay an existing finished task N times) or `spec` (run a fresh spec N times), else 422. Returns the variance run (`{id, status, n, child_task_ids, accumulated_cost_usd, aggregate, …}`); children are created and drained by the orchestrator loop, advanced by the `variance_run_tick` job |
| GET | `/api/quality/variance/{run_id}` | The variance run + a `children:[{id,status,cost_usd,result_summary}]` summary (404 if not in workspace). `aggregate` (once finalized) carries `n_executed/n_success/n_failed`, `success_rate`, `dimensions:[{key,name,unit,available,dist:{n,mean,std,min,p25,p50,p75,p95,max,values[]}}]` (outcome_score / trajectory_length / trajectory_score), `tool_stability:{runs,distinct_signatures,modal_share,per_tool[],signatures[]}`, `capped` |
| GET | `/api/quality/variance?source_task_id=` | List the workspace's variance runs, newest first; optional `source_task_id` filter |
| POST | `/api/quality/perturbation` | **owner/admin** — start an Adversarial / Perturbation run (E-12). Body `{source_task_id, transforms?=[paraphrase,noise,reorder,inject], variants_per_transform=1 (1..5), base_n=2 (1..10), parallel=true, cost_cap_usd?, template_id?}`; bad/empty `transforms` → 400. Replays the finished task under each transform plus `base_n` clean baseline runs. Returns the run (`{id, status, transforms, base_task_ids, perturbed_task_ids, aggregate, …}`); children are drained by the orchestrator loop, advanced by the `perturbation_run_tick` job |
| GET | `/api/quality/perturbation/{run_id}` | The run + `base_children[]` and `perturbed_children:{transform:[…]}` summaries (inject children carry `injection_followed`); 404 if not in workspace. `aggregate` (once finalized) carries `base:{score,outcome,dimensions}`, `transforms:[{key,n_success,n_total,outcome,robustness,score_delta,dimension_deltas,injection_followed_*}]`, `overall_robustness`, `robustness_available`, `safety:{injection_tested,n,followed_count,followed_rate,injection_followed}`, `capped` |
| GET | `/api/quality/perturbation?source_task_id=` | List the workspace's perturbation runs, newest first; optional `source_task_id` filter |
| POST | `/api/quality/comparison` | **owner/admin** — Pairwise Comparison Framework (E-21). Create a head-to-head "A vs B". Body `{subject="model"|"template"|"prompt", task_a_id, task_b_id?, source_task_id?, b_run_config?, judge_mode="llm"|"human"}`. **Direct** (`task_b_id` given): two existing tasks → `status="ready"`; an `llm` comparison is judged immediately → `judged`. **Generated** (`task_b_id` omitted, `b_run_config` given): candidate B is a rerun of `source_task_id` (defaults to `task_a_id`) with the override → `status="generating"`, judged on the `pairwise_run_tick`. 422 if neither `task_b_id` nor `b_run_config`. Returns the comparison row `{id, subject, task_a_id, task_b_id, player_a, player_b, status, judge_mode, judge_verdict, human_verdict, judge_detail, cost_usd, …}` |
| GET | `/api/quality/comparison?subject=&status=` | List comparisons (newest first, optional filters) + the judge↔human `agreement:{n,agreements,agreement}` over the returned set. Returns `{comparisons:[…], agreement}` |
| GET | `/api/quality/comparison/{id}` | A comparison + `side_by_side:{a,b}` (each side `{task_id, player, title, model_used, status, result_summary, weighted_score}`) for the UI. 404 if not in workspace |
| POST | `/api/quality/comparison/{id}/judge` | **owner/admin** — force/redo the LLM judge (position-bias mitigated: the pair is judged in both orders, agree → winner, disagree → tie + `position_bias_detected`). Two LLM calls. `judge_detail = {judge_model, mitigate_position, position_bias_detected, orders:{ab,ba}, input/output_tokens, cost_usd}` |
| PUT | `/api/quality/comparison/{id}/human-verdict` | **owner/admin** — record a human winner. Body `{verdict:"a"|"b"|"tie", reasoning?}`. A `ready` comparison becomes `judged`; the judge verdict (if any) is preserved for agreement tracking (E-17). 400 if the comparison isn't ready/judged |
| POST | `/api/quality/comparison/leaderboard` | **owner/admin** — turn judged comparisons into **real** matches and rank them via the E-19 engine → an ELO `ranking_report` (`source="explicit"`) shown in the Leaderboard tab. Body `{subject="model"|"template", method="bt"|"elo", source="judge"|"human"}`. **No LLM call.** Returns the E-19 report (see `/ranking/run`) plus `pairwise:{source, n_judged_comparisons, n_matches}`. Same `ranking_key` as E-19's derived path — distinguished by `metrics.source` |

`evaluator` ∈ `judge` (LLM-as-judge) \| `reference` (reference-based, E-03) \|
`objective` (E-04) \| `human` (E-05). The `human` evaluator dimension stays
`deferred` in the auto-profile; human ratings are collected as a **parallel signal**
via the feedback endpoints above (appended to the `annotations` ledger, with the
latest human one materialised into `quality_records.human_feedback`) and do
not change the judge gate. A `reference` dimension
takes `reference_mode` ∈ `pointwise` \| `exact` \| `fuzzy` \| `semantic` (defaults
to `pointwise`; ignored/cleared for non-reference evaluators) and is scored against
the task's `reference_answer` — `skipped` when none is set. An `objective` dimension
takes `probe` ∈ `lint` (ruff) \| `types` (mypy) (defaults to `lint`; ignored/cleared
for non-objective evaluators); it runs the static-analysis tool over the task's
Python result files and is `skipped` when the task produced none.
Setting `is_default` clears the default flag on the workspace's other rubrics.
Auto-evaluation also runs as the `quality_judge_evaluate` scheduler job when the
`quality_eval_enabled` setting is true (off by default).

The Variance / Robustness Harness (E-11) is also exposed as a CLI:
`docker compose exec api python -m app.cli.variance --task-id <uuid> --n 10 [--no-parallel] [--cost-cap <usd>] [--wait]`
(or `--title "…" [--description "…"]` for the spec mode). It calls the same
`run_variance` service; the `variance_run_tick` job (interval 20s, no gate)
advances every non-terminal run.

The Capability-isolation harness (E-13) is also exposed as a CLI:
`docker compose exec api python -m app.cli.capability evaluate --task <uuid>` (run
the harness for one task) and `… python -m app.cli.capability aggregate [--category
<c>] [--model <m>] [--template <id>]` (capability_score by model/category/template).
Auto-evaluation runs as the `capability_evaluate` scheduler job when the
`capability_eval_enabled` setting is true (off by default); the outcome-correctness
threshold is the `capability_outcome_threshold` setting (default 7.0).

The Failure Mode Classifier (E-14) auto-evaluation runs as the `failure_mode_evaluate`
scheduler job when the `failure_mode_eval_enabled` setting is true (off by default);
the judge's input-token cap is the `failure_judge_max_input_tokens` setting
(default 12000). The on-demand endpoint works regardless of the gate.

The **Benchmark Case Store** (pre-E-23) materializes versioned case files into
runnable tasks: `docker compose exec api python -m app.cli.benchmark suites|load
--suite <s> --template <id> [--model <id>] [--repeat K]|status|evaluate|aggregate`.
Cases live in `backend/benchmarks/<suite>/*.yaml`; runs are tagged
`benchmark_suite`/`benchmark_case_id` and aggregate via the `suite=` filter above.
Full format + workflow in [`benchmarks.md`](benchmarks.md). The registry table /
catalogue API / publication are E-23.

### Benchmarks (`/api/benchmarks`) — SPA-54

Read-only REST view over the file-based Benchmark Case Store (the same suites the
CLI loads from `backend/benchmarks/<suite>/*.yaml`), so the experiment dataset
picker can browse suites instead of blind-typing a name. Case authoring stays
file-based.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/benchmarks/suites` | List suites with case counts: `[{name, n_cases}]` |
| GET | `/api/benchmarks/suites/{suite}` | Inspect one suite (gold values are not exposed): `{suite, n_cases, cases:[{id, title, category, family, required_services[], mcp_servers[], gold:{reference_answer, rubric, canonical_trajectory, capability_spec, external_eval}}]}` — each `gold.*` is a boolean (which eval engines the case carries). 404 on unknown suite, 400 on a malformed case file |

### Scheduled jobs (`/api/scheduled-jobs`)

| Method | Path | |
|--------|------|--|
| GET | `/api/scheduled-jobs` | List |
| POST | `/api/scheduled-jobs` | Create. Body: `{name, kind, cron_expr?, interval_seconds?, fire_at?, payload, enabled}` |
| PATCH | `/api/scheduled-jobs/{id}` | |
| DELETE | `/api/scheduled-jobs/{id}` | |

### Settings (`/api/settings`)

| Method | Path | |
|--------|------|--|
| GET | `/api/settings` | All keys → JSONB values |
| PATCH | `/api/settings` | Body — partial dict. Known keys: `embedding_*`, `max_concurrent_agents`, `task_timeout_minutes`, `max_retries`, `memory_mode` (`flat`\|`structured`), `decomposition_enabled` (bool, default `true`), `data_lake_retention_days` (int, 0=forever), `data_lake_public_opt_in_default` (bool), `quality_eval_enabled` (bool, default `false` — gates the E-02 auto-evaluation job). LLM credentials moved to providers/llm_models (see below). |
| GET | `/api/settings/health` | Alias for `/api/health` (per spec §4.7) |
| GET | `/api/settings/export-all` | ZIP containing tasks/templates/events/settings/rules.md/memory.md/documents.json (capped at 10k events) |

### Providers & Models (`/api/providers`, `/api/models`)

Workspace-scoped CRUD for LLM providers and their models. The `api_key` field is never returned in responses — only a `api_key_masked` field of the form `***<last4>`.

| Method | Path | Body / Returns |
|--------|------|-----|
| GET | `/api/providers` | List providers in current workspace |
| POST | `/api/providers` | `{name, api_key, endpoint, max_concurrency?}` → 201 with `api_key_masked`. `max_concurrency` caps simultaneous backend LLM calls to the provider (subscription plans often limit concurrent requests, not tokens) |
| PATCH | `/api/providers/{id}` | Partial. Omit `api_key` to keep current. `max_concurrency: 0` clears the limit (unbounded). 409 on name collision. |
| DELETE | `/api/providers/{id}` | Cascades to models. Templates/workspaces referencing those models get `model_id=NULL`. |
| GET | `/api/providers/{id}/models` | List models for one provider |
| POST | `/api/providers/{id}/models` | `{display_name, api_name, input_price_per_1m_usd?, output_price_per_1m_usd?}` — defaults to 0. 409 on (provider_id, api_name) collision. |
| PATCH | `/api/models/{id}` | Partial update of any field |
| DELETE | `/api/models/{id}` | Sets `templates.model_id = NULL` and `workspaces.*_model_id = NULL` for references |
| POST | `/api/models/{id}/test` | Probe the model with a tiny "ping" completion. Returns `{status: "ok", latency_ms, sample}` or `{status: "error", error}`. |

### Workspaces (`/api/workspaces`)

| Method | Path | Body / Returns |
|--------|------|-----|
| GET | `/api/workspaces/me/system-models` | `{orchestrator_model_id, chat_model_id, memory_extractor_model_id}` — current assignments |
| PATCH | `/api/workspaces/me/system-models` | Partial. Each id must reference a model in this workspace; pass `null` to clear. |

### Health

| Method | Path | |
|--------|------|--|
| GET | `/api/health` | postgres/qdrant/minio liveness |

## Future evolution

`/api/v1` is aspirational — the planned versioned surface. In short:
- All endpoints will be reached primarily under `/api/v1/`.
- Auth: `/api/v1/auth/{register,login,refresh,me}`.
- Workspace: `X-Workspace-Id` header, scoping for every resource.
