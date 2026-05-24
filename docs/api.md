# API

> As of 2026-05-04 (R1 + R2 + R6) — 50+ paths in OpenAPI. Source of truth — `/openapi.json` from the live API. This file is a topical map.

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
- `GET/PUT /api/quality/records/{id}/feedback` (human feedback, E-05), `GET /api/quality/calibration`
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
| GET | `/api/tasks?status=&parent_id=` | List tasks |
| POST | `/api/tasks` | Create a task in backlog. Fields: title/description/priority/parent_id/`reference_answer`? (optional gold answer for reference-based scoring, E-03) |
| GET | `/api/tasks/{id}` | Single task + subtasks |
| PATCH | `/api/tasks/{id}` | title/description/status/priority/`reference_answer` |
| PATCH | `/api/tasks/{id}/approve` | From `awaiting_approval` → `done` |
| PATCH | `/api/tasks/{id}/reject` | Body `{feedback}`; sets `ready`, bumps `retry_count` |
| DELETE | `/api/tasks/{id}` | Delete the task |
| GET | `/api/tasks/{id}/decomposition` | Tree + per-attempt timeline for a parent task. Returns `{parent, subtasks: [{id, title, template_name, status, retry_count, max_retries, depends_on, started_at, completed_at, cost_usd, result_files_count, attempts: [{agent_container_id, spawned_at, finished_at, outcome, error}]}]}`. Attempts are grouped by `agent_container_id` from `agent_events` (`agent_spawned`/`agent_completed`/`agent_failed`/`agent_aborted`); outcome is the last terminal event or `running` if only spawned. Used by the Decomposition view (`/graph` → Decomposition tab). |

### Templates (`/api/templates`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/templates` | List. Each row includes `model_id` (FK → llm_models), denormalized `model_display_name`/`model_api_name`/`provider_name`. |
| POST | `/api/templates` | Create. Fields: name/description/soul_md/`model_id`/tools/mcp_servers/limits/tags. `model_id` must reference a model in the same workspace. |
| GET | `/api/templates/{id}` | Single |
| PUT | `/api/templates/{id}` | Update (creates a version snapshot before applying changes). Accepts `model_id`. |
| DELETE | `/api/templates/{id}` | |
| GET | `/api/templates/{id}/versions` | List versions |
| GET | `/api/templates/{id}/versions/{v}` | Snapshot v |
| POST | `/api/templates/{id}/rollback/{v}` | Apply snapshot v as the current state (creates two new versions: pre-rollback + post-rollback). Legacy snapshots with a `model` string are best-effort mapped to `model_id` via api_name. |

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
| POST | `/api/v1/agent-log/{task_id}` | Agent → orchestrator chunk ingest. Bearer agent_token + idempotency. Body: `{chunk_seq, content (≤256 KB), tool_name?, idempotency_key}`. Returns `{status: ok\|duplicate, chunk_seq}`. Auto-remap of chunk_seq on retry collision. |
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
| GET | `/api/quality/records/{task_id}/feedback` | `{task_id, human_feedback}` — stored human feedback (E-05) or null (404 if no task in workspace) |
| PUT | `/api/quality/records/{task_id}/feedback` | Upsert human feedback. Body `{verdict?: approve\|reject, overall_comment?, dimensions: [{key, name?, score 0-10, comment?}]}`. Builds the quality record on demand; returns `{task_id, human_feedback}` |
| GET | `/api/quality/calibration` | **owner/admin** — flattened judge-vs-human pairs (one row per rated dimension across records with human feedback): `{task_id, dimension_key, dimension_name, judge_score, human_score, band, judge_reasoning, human_comment, verdict, submitted_at}`. Calibration input for E-17 |
| GET | `/api/quality/records/{task_id}/trace` | Cleaned, judge-ready trajectory (E-06) — input for the trajectory judge (E-07). Query `tool_output_token_cap` (50–8000, default 600), `keep_tail_on_error` (bool). Returns `{task_id, cleaned_trace}`; computed on demand, not persisted (404 if no task in workspace) |
| GET | `/api/quality/records/{task_id}/trajectory` | `{task_id, trajectory_profile}` — 6-axis trajectory profile (E-07) or null until judged (404 if no record in workspace) |
| POST | `/api/quality/records/{task_id}/evaluate-trajectory` | **owner/admin** — on-demand trajectory judge (re-runs/overwrites). Returns `{trajectory_profile, skipped, detail?}`; `skipped=true` when the trajectory has no steps or no judge/orchestrator model is configured. Profile carries `axes:[{key,name,score 0-10,reason}]` (6), `overall_score`, `loop_detected`, `summary`, `judge_*`, `input_capped`, `status` |

`evaluator` ∈ `judge` (LLM-as-judge) \| `reference` (reference-based, E-03) \|
`objective` (E-04) \| `human` (E-05). The `human` evaluator dimension stays
`deferred` in the auto-profile; human ratings are collected as a **parallel signal**
via the feedback endpoints above (stored in `quality_records.human_feedback`) and do
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
| POST | `/api/providers` | `{name, api_key, endpoint}` → 201 with `api_key_masked` |
| PATCH | `/api/providers/{id}` | Partial. Omit `api_key` to keep current. 409 on name collision. |
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

See `production-readiness-tz.md`. In short:
- All endpoints will be reached primarily under `/api/v1/`.
- Auth: `/api/v1/auth/{register,login,refresh,me}`.
- Workspace: `X-Workspace-Id` header, scoping for every resource.
