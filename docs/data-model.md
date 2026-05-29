# Data Model

> Schema snapshot as of 2026-05-04 (after migration `d0e1f2a3b4c5`).
> Whenever the model changes, update this file in the same PR.

## Migrations

Revision chain:

```
819cd4ea6d24  initial_schema
     ↓
a1b2c3d4e5f6  rename_skills_to_tools
     ↓
b2c3d4e5f6a7  memory_entities + memory_relations (P0)
     ↓
c3d4e5f6a7b8  per_template_model: templates.model nullable, provider_url/api_key, tasks.model_used (P4)
     ↓
d4e5f6a7b8c9  tasks.cost_usd (P5)
     ↓
e5f6a7b8c9d0  scheduled_jobs (P8)
     ↓
f6a7b8c9d0e1  tasks.depends_on UUID[] (P9)
     ↓
a7b8c9d0e1f2  workspace_id columns (P11)
     ↓
b8c9d0e1f2a3  template_versions (P14)
     ↓
c9d0e1f2a3b4  users + workspaces + workspace_members + service_tokens; NOT NULL workspace_id everywhere (R1)
     ↓
d0e1f2a3b4c5  webhook_deliveries (R2)
     ↓
e1f2a3b4c5d6  agent_log_chunks + agent_log_deliveries + tasks.log_archive_s3_path
     ↓
f7e8d9c0b1a2  providers + llm_models; templates.{model,provider_url,provider_api_key} dropped → templates.model_id;
              workspaces.{orchestrator,chat,memory_extractor}_model_id; tasks.{input,output}_price_per_1m_usd (R7)
     ↓
b1c2d3e4f5a6  quality_records — Quality Data Lake (E-01)
     ↓
c2d3e4f5a6b7  rubrics — Quality Rubric Engine (E-02); templates.rubric_id;
              workspaces.quality_judge_model_id
     ↓
d3e4f5a6b7c8  tasks.reference_answer — Reference-based Judge (E-03)
     ↓
e4f5a6b7c8d9  quality_records.trajectory_evidence_profile — TRACE Evidence Bank Judge (E-08)
     ↓
a8b9c0d1e2f3  tasks.canonical_trajectory + quality_records.trajectory_match_profile — Trajectory Matching (E-09)
     ↓
b3c4d5e6f7a8  tasks.replay_of_task_id + tasks.run_config + variance_runs — Variance / Robustness Harness + re-run core (E-11)
     ↓
c4d5e6f7a8b9  perturbation_runs — Adversarial / Perturbation Judge (E-12)
     ↓
d5e6f7a8b9c0  tasks.capability_spec + quality_records.capability_profile — Capability-isolation Tests (E-13)
     ↓
e6f7a8b9c0d1  tasks.benchmark_case_id/suite + quality_records.benchmark_case_id/suite — Benchmark Case Store (pre-E-23)
     ↓
f7a8b9c0d1e2  quality_records.failure_profile — Failure Mode Classifier (E-14)
     ↓
a9b0c1d2e3f4  quality_records.hallucination_profile — Hallucination Detection (E-15)
```

## Tables

### tasks

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| id | UUID PK | uuid4 | |
| parent_id | UUID FK→tasks.id | NULL | for decomposition |
| title | VARCHAR(500) | required | |
| description | TEXT | NULL | |
| status | VARCHAR(50) | 'backlog' | TaskStatus enum |
| priority | VARCHAR(20) | 'medium' | TaskPriority enum |
| template_id | UUID FK→templates.id | NULL | chosen by the orchestrator |
| agent_container_id | VARCHAR(255) | NULL | active container; cleared on kill |
| result_summary | TEXT | NULL | from the agent (event=completed) |
| reference_answer | TEXT | NULL | optional gold answer for reference-based scoring (E-03); compared against `result_summary` by `reference` rubric dimensions |
| canonical_trajectory | JSONB | NULL | optional gold trajectory for matching (E-09; migration `a8b9c0d1e2f3`); a list of tool names or a `{nodes, edges}` DAG. Non-null ⇒ the matcher applies |
| capability_spec | JSONB | NULL | optional capability-isolation spec (E-13; migration `d5e6f7a8b9c0`): `{required_tools[], category?, match?:all\|any}`. Non-null ⇒ the Glass-Box harness applies |
| benchmark_case_id | VARCHAR(128) | NULL | Benchmark Case Store (migration `e6f7a8b9c0d1`): the versioned case this instance was materialized from |
| benchmark_suite | VARCHAR(128) | NULL | Benchmark Case Store: the suite the case belongs to |
| replay_of_task_id | UUID FK→tasks.id ON DELETE SET NULL | NULL | re-run/replay lineage (E-11; migration `b3c4d5e6f7a8`) — the task this one was cloned from. Distinct from `parent_id` so variance/replay children are never rolled into a parent's subtask-completion check |
| run_config | JSONB | NULL | optional per-run overrides honored at spawn time (E-11): `{template_id?, model_id?, soul_md?, seed?, temperature?, tool_injection?}`. When set, the orchestrator skips decomposition + selection and pins this config (seam for E-21/E-24/U-03; E-11 sets `template_id`, E-12 adds `tool_injection` — a payload appended to the first tool response at runtime) |
| result_files | JSONB | [] | list of MinIO paths |
| token_usage | JSONB | {} | `{input_tokens, output_tokens}` |
| retry_count / max_retries | int | 0 / 1 | |
| user_feedback | TEXT | NULL | on reject |
| orchestrator_feedback | TEXT | NULL | from auto-review |
| model_used | VARCHAR(255) | NULL | denormalized api_name of the model used (kept even if LLMModel is later deleted) |
| input_price_per_1m_usd | NUMERIC(12,6) | NULL | denormalized at spawn time from `llm_models.input_price_per_1m_usd`; used by cost.py so deleting/repricing a model doesn't retro-change cost |
| output_price_per_1m_usd | NUMERIC(12,6) | NULL | denormalized at spawn time from `llm_models.output_price_per_1m_usd` |
| cost_usd | NUMERIC(10,6) | 0 | computed cost (input_price × input_tokens / 1M + output_price × output_tokens / 1M) |
| depends_on | UUID[] | {} | ids of dependency tasks (P9) |
| workspace_id | UUID NOT NULL | | scoping (post-R1, FK CASCADE) |
| created_at / updated_at / started_at / completed_at | TIMESTAMP | now() / onupdate | |

Indexes: `status`, `parent_id`, `workspace_id`.

### templates

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| id | UUID PK | uuid4 | |
| name / description / soul_md | string/text | required | |
| model_id | UUID FK→llm_models.id ON DELETE SET NULL | NULL | model used to run the agent. NULL → template not spawnable. |
| rubric_id | UUID FK→rubrics.id ON DELETE SET NULL | NULL | quality rubric for scoring this template's results (E-02); NULL → tag/default rubric |
| tools | JSONB | [] | list of built-in tools |
| mcp_servers | JSONB | [] | list of `{name, command, args, env}` |
| max_ram / max_cpu / timeout_minutes | string/int | "2g" / 100000 / 60 | docker limits |
| tags | TEXT[] | {} | |
| workspace_id | UUID NOT NULL | | (post-R1) |
| created_at / updated_at | TIMESTAMP | now() / onupdate | |

Legacy columns `model`/`provider_url`/`provider_api_key` were dropped by migration `f7e8d9c0b1a2`; the existing data was migrated into a Provider+Model pair per workspace.

### providers (R7)

LLM provider records (one or many per workspace). Created/updated through `/api/providers`.

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| id | UUID PK | uuid4 | |
| workspace_id | UUID FK→workspaces.id ON DELETE CASCADE | | |
| name | VARCHAR(200) | required | UNIQUE per workspace |
| api_key | VARCHAR(500) | required | full key (masked in API responses) |
| endpoint | VARCHAR(500) | required | base URL (e.g. `https://api.openai.com/v1`) |
| created_at / updated_at | TIMESTAMP | now() / onupdate | |

Index: `workspace_id`. UNIQUE constraint: `(workspace_id, name)`.

### llm_models (R7)

Models offered by a provider. Created/updated through `/api/providers/{id}/models` and `/api/models/{id}`.

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| id | UUID PK | uuid4 | |
| provider_id | UUID FK→providers.id ON DELETE CASCADE | | |
| display_name | VARCHAR(255) | required | UI label (e.g. "GPT-4o") |
| api_name | VARCHAR(255) | required | identifier sent to the LLM endpoint (e.g. `gpt-4o`). UNIQUE per provider. |
| input_price_per_1m_usd | NUMERIC(12,6) | 0 | tokens-in price per 1M tokens |
| output_price_per_1m_usd | NUMERIC(12,6) | 0 | tokens-out price per 1M tokens |
| created_at / updated_at | TIMESTAMP | now() / onupdate | |

Index: `provider_id`. UNIQUE constraint: `(provider_id, api_name)`.

### template_versions (P14)

A snapshot is taken before every PUT, enabling rollback.

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| template_id | UUID FK→templates.id ON DELETE CASCADE | |
| version | int | UNIQUE (template_id, version) |
| snapshot | JSONB | full Template copy at snapshot time |
| commit_message | TEXT | auto-generated ("auto: pre-update", "rollback to v1") |
| created_by | VARCHAR(50) | "user" |
| created_at | TIMESTAMP | now() |

### agent_events

Append-only event log. Source of truth for analytics and WS broadcast.

| Column | Type | |
|--------|------|--|
| id | BIGINT PK auto | |
| task_id | UUID FK→tasks.id NULL | |
| agent_container_id | VARCHAR(255) NULL | |
| event_type | VARCHAR(50) | see the catalogue below |
| source | VARCHAR(50) | 'agent' / 'orchestrator' / 'user' / 'system' |
| data | JSONB | arbitrary payload |
| workspace_id | UUID NOT NULL | (post-R1) |
| created_at | TIMESTAMP | now() |

Indexes: `created_at`, `task_id`, `event_type`, `workspace_id`.

**Known event_type values** (extensible):
`task_created`, `task_status_changed`, `orchestrator_decision`, `orchestrator_reasoning`,
`orchestrator_feedback`, `agent_spawned`, `agent_message`, `agent_progress`, `agent_completed`,
`agent_failed`, `agent_aborted`, `agent_killed`, `agent_health`, `agent_feedback_sent`,
`agent_abort_signaled`, `agent_model_switched`, `task_retry`, `task_timeout`,
`memory_updated`, `memory_extracted`, `webhook_received`, `webhook_validation_failed`,
`scheduled_job_fired`, `daily_cost_summary`, `kill_all_agents`, `user_action`,
`decomposition_failed_cycle`, `quality_record_backfill`, `quality_record_retention`.

Note: the `agent_spawned` event payload is enriched (E-01) with a full state
snapshot — `soul_md`, `tools`, `mcp_servers`, model api_name + prices,
`resource_limits`, `memory_context`, and `flat_memory` (rules.md/memory.md
content) — the durable source for the data-lake `execution` section.

### chat_messages

| Column | Type | |
|--------|------|--|
| id | BIGINT PK auto | |
| role | VARCHAR(20) | 'user' / 'assistant' / 'tool' |
| content | TEXT | |
| related_task_id | UUID FK→tasks.id NULL | |
| token_usage | JSONB | |
| workspace_id | UUID NOT NULL | (post-R1) |
| created_at | TIMESTAMP | now() |

### knowledge_documents

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| filename | VARCHAR(500) | |
| s3_path | VARCHAR(1000) | path inside the MinIO bucket `spawnhive` |
| chunk_count | int | how many chunks landed in Qdrant |
| workspace_id | UUID NOT NULL | (post-R1) |
| created_at | TIMESTAMP | |

The matching chunks live in the Qdrant collection `spawnhive_docs`.

### memory_entities (P0)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| type | VARCHAR(50) | "person" / "project" / "decision" / any extensible value |
| name | VARCHAR(500) | |
| attributes | JSONB | arbitrary scalar key-values |
| embedding_id | UUID NULL | == id, link into the Qdrant `memory_entities` collection |
| created_by | VARCHAR(50) | 'orchestrator' / 'user' / 'agent' |
| workspace_id | UUID NOT NULL | (post-R1) |
| created_at / updated_at | TIMESTAMP | |

Indexes: `type`, `name`, `workspace_id`.
Qdrant: collection `memory_entities` (dim derived from the active embedding provider).

### memory_relations (P0)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| from_id / to_id | UUID FK→memory_entities ON DELETE CASCADE | |
| relation_type | VARCHAR(100) | free-form |
| attributes | JSONB | |
| workspace_id | UUID NOT NULL | (post-R1) |
| created_at | TIMESTAMP | |

Indexes: `from_id`, `to_id`, `workspace_id`.

### settings

| Column | Type | |
|--------|------|--|
| key | VARCHAR(255) PK | |
| value | JSONB | arbitrary |
| updated_at | TIMESTAMP | |

**Seeded keys** (seed_settings):
`max_concurrent_agents` (3), `task_timeout_minutes` (60), `max_retries` (1),
`embedding_provider` ('fastembed'), `embedding_model_local` ('BAAI/bge-small-en-v1.5'),
`embedding_api_url`, `embedding_api_key`, `embedding_model_api`,
`minio_endpoint`, `minio_access_key`, `minio_secret_key`,
`memory_mode` ('flat' | 'structured', default 'flat'),
`data_lake_retention_days` (0 = keep forever), `data_lake_public_opt_in_default` (false) — E-01.

LLM credentials (provider endpoint + API key) and model pricing moved to `providers` and `llm_models` in R7. The legacy keys `llm_base_url`/`llm_api_key`/`llm_model`/`model_pricing` were removed by the migration; the values are seeded as a default Provider+Model in the default workspace.

### quality_records (E-01)

Immutable, versioned snapshot of one task execution — the Quality Data Lake. One
row per task (UNIQUE `task_id`), built on a settled terminal state. The queryable
summary lives here; the full execution blob (decomposition tree, per-agent state
snapshot, tool calls, events) is a JSON object in MinIO at `record_s3_path`
(`data-lake/<workspace_id>/<task_id>.json`). The JSONB slots are nullable
placeholders filled by downstream eval features.

| Column | Type | Purpose |
|--------|------|---------|
| id | UUID PK | |
| task_id | UUID FK→tasks ON DELETE CASCADE | UNIQUE `uq_quality_records_task` |
| workspace_id | UUID FK→workspaces ON DELETE CASCADE | scoping |
| schema_version | int | default 1 — blob layout is tied to this |
| template_id / template_name / model_used | UUID? / str? / str? | denormalized (survive source deletion) |
| final_status | VARCHAR(50) | done / failed / awaiting_approval (reconciled by the backfill job) |
| is_decomposition_root | bool | parent task with subtasks |
| cost_usd | NUMERIC(10,6) | denormalized |
| input_tokens / output_tokens / duration_seconds / tool_call_count | int? | outcome metrics |
| quality_profile | JSONB? | **slot E-02** |
| trajectory_profile | JSONB? | **slot E-07** |
| trajectory_evidence_profile | JSONB? | **slot E-08** (TRACE evidence-bank judge; added by migration `e4f5a6b7c8d9`) |
| trajectory_match_profile | JSONB? | **slot E-09** (deterministic trajectory matcher; added by migration `a8b9c0d1e2f3`) |
| capability_profile | JSONB? | **slot E-13** (deterministic capability-isolation classification; added by migration `d5e6f7a8b9c0`) |
| failure_profile | JSONB? | **slot E-14** (multi-label failure-mode classification; added by migration `f7a8b9c0d1e2`) |
| hallucination_profile | JSONB? | **slot E-15** (4-category deliverable fact-check; added by migration `a9b0c1d2e3f4`) |
| benchmark_case_id / benchmark_suite | VARCHAR(128)? | Benchmark Case Store linkage, denormalized from the task (migration `e6f7a8b9c0d1`); `benchmark_suite` indexed for suite-scoped aggregation |
| human_feedback | JSONB? | **slot E-05** (filled by the feedback API) |
| longitudinal | JSONB? | **slot E-22** |
| reproducibility | JSONB? | **slot E-20** |
| record_s3_path | VARCHAR(500) | MinIO path of the full JSON blob |
| public_dataset_opt_in | bool | default false — privacy gate for the public benchmark (E-23) |
| created_at | TIMESTAMP | |

Indexes: `workspace_id`, `template_id`, `model_used`, `final_status`, `created_at`.

Built best-effort from the webhook terminal path (before log compaction prunes
the chunks) and reconciled/backfilled by the `quality_record_backfill` scheduled
job; pruned by `quality_record_retention` per the `data_lake_retention_days`
setting (0 = keep forever; opted-in records are never auto-deleted).

The `quality_profile` slot is filled by the Quality Rubric Engine (E-02) — see
`rubrics` below.

The `human_feedback` slot (E-05) is filled by `PUT /api/quality/records/{task_id}/feedback`
(building the record on demand if absent). It is a **parallel** signal — it does
not change the judge gate/weighted score. Shape (no migration — reuses the JSONB
slot): `{schema_version, verdict: approve|reject|null, overall_comment?,
dimensions: [{key, name, score 0-10, band: bad|improve|good, comment?, judge_score?}],
submitted_by, submitted_at}`. Bands map score → quality (1-3 bad / 4-7 improve /
8-10 good) with thresholds fixed for now (rubric-configurable in E-26); each
dimension mirrors a `quality_profile` axis and copies the judge's score for
calibration (E-17, exposed via `GET /api/quality/calibration`).

The `trajectory_profile` slot is filled by the Trajectory Judge (E-07) —
`POST /api/quality/records/{task_id}/evaluate-trajectory`, building the record on
demand if absent. It is the **process** counterpart of the outcome `quality_profile`:
an LLM scores the cleaned trace (E-06) on six axes in one call. Shape (no migration —
reuses the JSONB slot): `{schema_version, status: scored|skipped|error,
axes: [{key, name, score 0-10, reason}] (efficiency, tool_selection,
parameter_quality, error_recovery, goal_alignment, loop_detection),
overall_score (mean), loop_detected, summary, judge_model, judge_input_tokens,
judge_output_tokens, judge_cost_usd, input_capped, trace_stats, evaluated_at,
errors}`. The cleaned trace itself (E-06) stays transient
(`GET /api/quality/records/{task_id}/trace`) and is not persisted — E-07 rebuilds
it from the durable sources (`agent_events` + `agent_log_chunks`, or the MinIO log
archive after compaction) at judge time.

The `trajectory_evidence_profile` slot is filled by the Evidence Bank Judge (E-08,
TRACE) — `POST /api/quality/records/{task_id}/evaluate-trajectory-evidence`, building
the record on demand if absent (added by migration `e4f5a6b7c8d9`). Unlike E-07's
holistic single call, E-08 walks the cleaned trace step by step accumulating an
**evidence bank** threaded into each step's prompt, then scores the same six axes
informed by that bank. Shape: `{schema_version, status: scored|skipped|error,
axes (same 6 as E-07), overall_score, loop_detected, summary, groundedness (0-1,
share of grounded steps **among assessed steps**), redundant_steps, evidence_bank:
[{seq, kind, tool_name, redundant, grounded (bool, or null when not judged), assessed
(false for empty/label steps — no tool call and no content — which skip the per-step
LLM call and are excluded from groundedness), progress 0-10, execution 0-10, facts
[str], note, error?}],
judge_model, judge_calls (N+1), judge_input_tokens, judge_output_tokens,
judge_cost_usd, input_capped, trace_stats (incl. steps_assessed), evaluated_at,
errors}`. It coexists with `trajectory_profile` so the holistic (E-07) and
evidence-aware (E-08) judges can be compared side by side. Cost is bounded by
`trace_evidence_max_steps` (default 30) and `trace_evidence_max_input_tokens`
(default 12000).

The `trajectory_match_profile` slot is filled by the **deterministic, LLM-free**
Trajectory Matcher (E-09) — `POST /api/quality/records/{task_id}/evaluate-trajectory-match`,
building the record on demand if absent (added by migration `a8b9c0d1e2f3`). It only
applies to tasks with a canonical trajectory (`tasks.canonical_trajectory`); it
compares the actual tool sequence (E-06 `kind == "tool"` steps) against the reference
on three metrics. Shape: `{schema_version, status: scored|skipped|error,
mode: exact|edit|dag, score, matched, threshold, metrics: {exact, edit, dag},
actual_sequence [str], reference_sequence [str], reference_form: sequence|dag,
detail, trace_stats: {steps_total, tool_steps}, evaluated_at, errors}`. `exact` =
sequence equality, `edit` = `difflib` ratio, `dag` = the actual run is a valid
topological order of the canonical DAG (node-instance Kahn check). No batch job —
the matcher is instant and applies to a rare task class.

The `capability_profile` slot is filled by the **deterministic** Capability-isolation
harness (E-13, part A) — `POST /api/quality/records/{task_id}/evaluate-capability`,
building the record on demand if absent (added by migration `d5e6f7a8b9c0`). It only
applies to tasks with a `capability_spec` (`{required_tools[], category?, match?}`).
Glass-Box matching (reusing E-09's `extract_tool_sequence`) checks whether the
required tools were actually called — sourced from the cleaned trace **unioned with
the durable E-01 blob** (`execution.tool_calls`) and matched prefix-aware (`web_search`
↔ `web__web_search`). The log archive now preserves `tool_name` (JSON-lines), so the
cleaned trace keeps tool steps named post-compaction; the blob union stays as
defense-in-depth (and for legacy plain-text archives, which lose it). Outcome
correctness reuses the E-02 judge (a
scored `reference` dimension when present, else `weighted_score ≥
capability_outcome_threshold`, default 7.0 — no new model). Shape:
`{schema_version, status: scored|error, category, required_tools [str], match: all|any,
tools_called [str], tool_used, missing_tools [str], outcome_correct,
outcome_signal: reference|judge|none, outcome_score, outcome_threshold,
classification: genuine|cheated|failed_with_tool|failed_no_tool, capability_passed,
trace_stats: {steps_total, tool_steps}, evaluated_at, errors}`. `cheated` = correct
outcome with the required tool **not** used (answered from memory). `aggregate_capability`
rolls these up into `capability_score = genuine/total` by model/category/template. The
off-by-default `capability_evaluate` job (setting `capability_eval_enabled`) batches it.

The `failure_profile` slot is filled by the **Failure Mode Classifier** (E-14) —
`POST /api/quality/records/{task_id}/evaluate-failure-modes`, building the record on
demand if absent (added by migration `f7a8b9c0d1e2`). One LLM call (reusing the E-02/E-07
judge model) labels the E-06 cleaned trace — with the existing E-02 outcome profile and
E-07 trajectory profile fed as grounding context, **never re-run** — with a multi-label
set of failure classes. It runs on every terminal task; a clean run yields an empty
`failures` list. Shape: `{schema_version, status: scored|skipped|error,
failures: [{class, confidence (0..1), reason}], summary, judge_model, judge_input_tokens,
judge_output_tokens, judge_cost_usd, input_capped, used_outcome_profile,
used_trajectory_profile, trace_stats, evaluated_at, errors}`. Classes (extensible):
`tool_confusion`, `parameter_blind`, `loop`, `premature_stop`,
`hallucinated_tool_result`, `ignored_error`. `aggregate_failure_modes` rolls runs up
into per-class counts + `rate` by class/model/template — the distribution of failure
types per (model, template). Input is bounded by `failure_judge_max_input_tokens`
(default 12000); the off-by-default `failure_mode_evaluate` job (setting
`failure_mode_eval_enabled`) batches it.

The `hallucination_profile` slot is filled by the **Hallucination Detector** (E-15) —
`POST /api/quality/records/{task_id}/evaluate-hallucinations`, building the record on
demand if absent (added by migration `a9b0c1d2e3f4`). It fact-checks the finished run's
deliverable (`task.result_summary`) against the E-06 cleaned trace across four
categories — **URLs / APIs / numbers / citations**. URLs and code-fence API symbols are
checked **deterministically** (a URL/symbol is "supported" iff it appears in some tool
argument/result of the trace — *in-trace only*, no live HTTP); numbers, claims and
unconfirmed APIs go to **one LLM call** (reusing the E-02/E-07 judge model, with the
E-02 outcome summary and E-08 evidence facts fed as grounding, **never re-run**). If
the deterministic pass leaves nothing to ask, no LLM call is made. Shape:
`{schema_version, status: scored|error, categories: {urls, apis, numbers, citations}
each {checked, hallucinated, items: [{value|claim, kind: deterministic|llm, supported,
reason, confidence?}]}, hallucination_count, items_total, hallucination_rate
(= count/items_total), summary, judge_model, judge_input_tokens, judge_output_tokens,
judge_cost_usd, input_capped, used_outcome_profile, used_trajectory_evidence,
trace_stats, evaluated_at, errors}`. A clean deliverable yields `hallucination_rate` 0.
The slot is **orthogonal** to `quality_profile` — `hallucination_rate` lives alongside
the E-02 profile on the same record, **not** as a rubric `dimension` in the E-02 engine
(same design as E-13 `capability_profile` / E-14 `failure_profile`). `aggregate_hallucinations`
rolls runs up into per-category `checked`/`hallucinated`/`rate` + `hallucinated_runs`
by category/model/template. Input is bounded by `hallucination_judge_max_input_tokens`
(default 12000); the off-by-default `hallucination_evaluate` job (setting
`hallucination_eval_enabled`) batches it.

### rubrics (E-02)

A multi-dimensional quality rubric: a set of independent dimensions used to score
a task result into a **profile** (vector of 0–10 scores) rather than one number.
Five built-ins are seeded into the default workspace (`seed_default_rubrics` in
`app/main.py`) and cloned to each new workspace on registration.

| Column | Type | Purpose |
|--------|------|---------|
| id | UUID PK | |
| workspace_id | UUID FK→workspaces ON DELETE CASCADE | scoping |
| name | VARCHAR(255) | e.g. "Code", "Analytical Report" |
| description | TEXT | |
| applies_to | VARCHAR(50) NULL | task-type tag for auto-selection (matches a template tag) |
| is_default | bool | default false — workspace's last-resort rubric |
| dimensions | JSONB | list of `{key, name, description, evaluator, reference_mode?, probe?, weight, threshold, critical}` |
| created_at / updated_at | TIMESTAMP | |

Index: `workspace_id`. A dimension's `evaluator` is one of `judge` (LLM-as-judge,
O2), `reference` (reference-based, E-03), `objective` (E-04 probes) or `human`
(E-05); `human` is recognized but scored as `deferred` until that feature lands. A
`reference` dimension carries `reference_mode ∈ {pointwise, exact, fuzzy, semantic}`
(E-03) and is scored only when the task has a `reference_answer` — otherwise
`skipped`. An `objective` dimension carries `probe ∈ {lint, types}` (E-04) and runs
that static-analysis tool over the task's Python result files — `skipped` when the
task produced none. Both `reference_mode` and `probe` are stored only for their
owning evaluator and cleared (null) otherwise; neither needs a schema migration
(they live in the `dimensions` JSONB).

**Rubric selection for a task**: `Template.rubric_id` → a workspace rubric whose
`applies_to` matches a template tag → the workspace's `is_default` rubric → none
(evaluation skipped). The judge model is the workspace's `quality_judge_model_id`,
falling back to `orchestrator_model_id`. Profiles are written to
`quality_records.quality_profile` (schema_version 2 since E-03); the MinIO blob is
left immutable. `reference` dimensions reuse the same judge model for the
`pointwise` mode and the configured embedding provider for `semantic`; `exact`/
`fuzzy` are pure local comparisons (no model call).

### variance_runs (E-11)

One row groups N re-runs of a single scenario for the Variance / Robustness
Harness (migration `b3c4d5e6f7a8`).

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| id | UUID PK | uuid4 | |
| workspace_id | UUID FK→workspaces.id ON DELETE CASCADE | required | scoping |
| source_task_id | UUID FK→tasks.id ON DELETE SET NULL | NULL | replay an existing finished task N times |
| source_spec | JSONB | NULL | …or run a fresh spec N times: `{title, description?, reference_answer?}` |
| template_id | UUID | NULL | pinned template for children (denormalized); NULL ⇒ normal selection each run |
| n | int | required | number of runs (2..50) |
| parallel | bool | true | drain up to `max_concurrent_agents` at once vs sequentially |
| cost_cap_usd | NUMERIC(10,6) | NULL | stop creating children once accumulated cost (agent + judge) crosses this |
| status | VARCHAR(20) | 'pending' | pending / running / done / capped / failed |
| child_task_ids | JSONB | [] | the child task ids (each a clone via `clone_task_for_rerun`) |
| aggregate | JSONB | NULL | computed distribution: per-dimension mean/std/percentiles + values, success rate, tool-selection stability |
| accumulated_cost_usd | NUMERIC(10,6) | 0 | running total across children (agent runs + inline judge evals) |
| created_at / updated_at / completed_at | TIMESTAMP | now() / onupdate | |

Children are plain tasks (linked back via `tasks.replay_of_task_id`), spawned by
the existing orchestrator loop; the `variance_run_tick` scheduler job creates
the next children under the cost cap, judges finished ones (E-02/E-07 when a
judge model is configured) and writes `aggregate` once all children are
terminal. A child is a successful terminal at `done` **or** `awaiting_approval`.

### perturbation_runs (E-12)

One row groups a robustness probe of a single finished scenario for the
Adversarial / Perturbation Judge (migration `c4d5e6f7a8b9`).

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| id | UUID PK | uuid4 | |
| workspace_id | UUID FK→workspaces.id ON DELETE CASCADE | required | scoping |
| source_task_id | UUID FK→tasks.id ON DELETE SET NULL | NULL | the finished scenario whose input is perturbed |
| template_id | UUID | NULL | pinned template for every child (denormalized) |
| transforms | JSONB | [] | enabled transform keys ⊆ `paraphrase, noise, reorder, inject` |
| variants_per_transform | int | 1 | perturbed children per transform (1..5) |
| base_n | int | 2 | clean baseline re-runs of the original input (1..10) |
| parallel | bool | true | drain up to `max_concurrent_agents` at once vs sequentially |
| cost_cap_usd | NUMERIC(10,6) | NULL | stop creating children once accumulated cost crosses this |
| injection_canary | VARCHAR(64) | NULL | unique marker the `inject` payload asks the agent to emit; its presence in a child's output ⇒ the agent followed the injection |
| status | VARCHAR(20) | 'pending' | pending / running / done / capped / failed |
| base_task_ids | JSONB | [] | clean baseline child ids |
| perturbed_task_ids | JSONB | {} | perturbed child ids grouped by transform: `{transform: [task_id,…]}` |
| aggregate | JSONB | NULL | per-transform + overall robustness, baseline profile, dimension deltas, injection safety flag |
| accumulated_cost_usd | NUMERIC(10,6) | 0 | running total across children (agent runs + inline judge evals) |
| created_at / updated_at / completed_at | TIMESTAMP | now() / onupdate | |

Children are plain tasks (linked back via `tasks.replay_of_task_id`): the
baseline ones reproduce the original input, the perturbed ones carry a
paraphrased / noised / reordered input (or, for `inject`, the original input plus
a `run_config.tool_injection` payload). The orchestrator loop spawns them; the
`perturbation_run_tick` scheduler job creates the next children under the cost
cap, judges finished ones (E-02/E-07 when configured) and writes `aggregate`
once all children are terminal. Robustness per transform = how much the perturbed
outcome score degraded vs the baseline mean (1.0 = no degradation); the `inject`
group additionally yields a deterministic safety flag via `injection_canary`.

### scheduled_jobs (P8)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| name | VARCHAR(200) | |
| kind | VARCHAR(20) | 'cron' / 'interval' / 'once' |
| cron_expr | VARCHAR(200) NULL | for kind=cron |
| interval_seconds | int NULL | for kind=interval |
| fire_at | TIMESTAMP NULL | for kind=once |
| payload | JSONB | `{action: ..., ...}` |
| enabled | BOOL | default true |
| last_fired_at | TIMESTAMP NULL | |
| workspace_id | UUID NOT NULL | (post-R1) |
| created_at | TIMESTAMP | |

Index: `enabled`.

**Built-in jobs** (seed_default_jobs in `app/scheduler.py`):
- `daily_cost_rollup` — cron `0 0 * * *`, action `daily_cost_rollup`.
- `agent_progress_check` — interval 60s, action `agent_progress_check`.
- `quality_record_backfill` — interval 300s, action `quality_record_backfill` (E-01: build/reconcile records for terminal tasks; global).
- `quality_record_retention` — cron `30 0 * * *`, action `quality_record_retention` (E-01: prune old records per `data_lake_retention_days`).
- `quality_judge_evaluate` — interval 600s, action `quality_judge_evaluate` (E-02: score `done` records lacking a `quality_profile`; only runs when the `quality_eval_enabled` setting is true).
- `trajectory_judge_evaluate` — interval 600s, action `trajectory_judge_evaluate` (E-07: judge `done` records lacking a `trajectory_profile`; only runs when the `trajectory_eval_enabled` setting is true).
- `trace_evidence_evaluate` — interval 600s, action `trace_evidence_evaluate` (E-08: TRACE evidence-bank judge for `done` records lacking a `trajectory_evidence_profile`; batch capped at 5/tick since each task is N+1 LLM calls; only runs when the `trace_evidence_eval_enabled` setting is true).
- `capability_evaluate` — interval 600s, action `capability_evaluate` (E-13: run the Glass-Box capability harness on terminal tasks carrying a `capability_spec` but no `capability_profile`; only runs when the `capability_eval_enabled` setting is true).
- `failure_mode_evaluate` — interval 600s, action `failure_mode_evaluate` (E-14: classify failure modes on `done` records lacking a `failure_profile`; only runs when the `failure_mode_eval_enabled` setting is true).
- `hallucination_evaluate` — interval 600s, action `hallucination_evaluate` (E-15: fact-check `done` records lacking a `hallucination_profile`; only runs when the `hallucination_eval_enabled` setting is true).

### users (R1)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| email | VARCHAR(320) UNIQUE | |
| password_hash | VARCHAR(255) NULL | bcrypt; NULL for the seeded admin@local until first login |
| display_name | VARCHAR(200) NULL | |
| is_active | BOOL | default true |
| created_at | TIMESTAMP | |

### workspaces (R1)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| name | VARCHAR(200) | |
| slug | VARCHAR(120) UNIQUE | URL-safe identifier |
| created_by | UUID FK→users.id | |
| orchestrator_model_id | UUID FK→llm_models.id ON DELETE SET NULL | model used for decomposition / template selection / result evaluation (R7) |
| chat_model_id | UUID FK→llm_models.id ON DELETE SET NULL | model used by the chat panel (R7) |
| memory_extractor_model_id | UUID FK→llm_models.id ON DELETE SET NULL | model used by the structured-memory extractor (R7) |
| quality_judge_model_id | UUID FK→llm_models.id ON DELETE SET NULL | LLM-as-judge for rubric scoring (E-02); falls back to orchestrator when unset |
| created_at | TIMESTAMP | |

### workspace_members (R1)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| user_id | UUID FK→users.id ON DELETE CASCADE | |
| workspace_id | UUID FK→workspaces.id ON DELETE CASCADE | |
| role | VARCHAR(20) | 'owner' / 'admin' / 'member' / 'viewer' |
| created_at | TIMESTAMP | |

Unique: `(user_id, workspace_id)`.

### service_tokens (R1)

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| kind | VARCHAR(20) | 'agent' for the per-task agent token |
| token_hash | VARCHAR(128) | sha256 hex of the plaintext |
| task_id | UUID FK→tasks.id ON DELETE CASCADE | |
| workspace_id | UUID FK→workspaces.id ON DELETE CASCADE | |
| expires_at | TIMESTAMP | naive UTC |
| created_at | TIMESTAMP | |

### webhook_deliveries (R2)

Stores the `(task_id, idempotency_key)` pairs that have already been processed; lets a replay return `{"status":"duplicate"}` cleanly.

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| task_id | UUID FK→tasks.id ON DELETE CASCADE | |
| event_type | VARCHAR(50) | |
| idempotency_key | VARCHAR(80) | |
| received_at | TIMESTAMP | |

Unique: `(task_id, idempotency_key)`.

### agent_log_chunks (Foundations Этап 1)

Append-only stream of full agent stdout/stderr per tool call. Replaces the 500-char `recent_output` ticker for browseable history.

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| task_id | UUID FK→tasks.id ON DELETE CASCADE | |
| workspace_id | UUID FK→workspaces.id ON DELETE CASCADE | |
| chunk_seq | int | per-task monotonically increasing, UNIQUE `(task_id, chunk_seq)` |
| content | TEXT | ≤256 KB per row (Pydantic-enforced) |
| tool_name | VARCHAR(255) NULL | bash / file_read / mcp tool name |
| created_at | TIMESTAMP | |

Indexes: `(task_id, chunk_seq)`, `workspace_id`. After event=completed/failed/aborted the orchestrator serializes rows → MinIO blob `s3://spawnhive/logs/<task_id>.log` (**JSON-lines, one `{tool_name, content}` per chunk** — `tool_name` is preserved so the cleaned trace E-06 / matcher E-09 stay tool-aware post-compaction; `encode_log_archive`/`decode_log_archive` in `minio_client`, legacy `\n␞\n` plain-text archives still decode with `tool_name=None`), sets `tasks.log_archive_s3_path`, and DELETEs all chunks (best-effort, atomic).

### agent_log_deliveries (Foundations Этап 1)

Per-chunk idempotency table. Mirror of `webhook_deliveries`.

| Column | Type | |
|--------|------|--|
| id | UUID PK | |
| task_id | UUID FK→tasks.id ON DELETE CASCADE | |
| idempotency_key | VARCHAR(64) | |
| received_at | TIMESTAMP | |

Unique: `(task_id, idempotency_key)`.

### tasks.log_archive_s3_path (Foundations Этап 2)

Added to `tasks`: `VARCHAR(500) NULL`. NULL while task is active or never had any chunks; populated to `logs/<task_id>.log` after compaction. GET `/api/tasks/{id}/log` branches on this column.

## Invariants

- `tasks.status` ∈ {backlog, ready, decomposing, in_progress, review, awaiting_approval, done, failed}.
- `tasks.priority` ∈ {low, medium, high, urgent}.
- During decomposition: parent → in_progress, children are created with status=ready (or with `depends_on` filled in).
- `agent_events` is append-only — rows are never modified.
- `template_versions.version` grows monotonically per template; rollback creates a **new** version from the old one, never overwrites.
- `memory_entities.embedding_id` ≡ `id` after a successful Qdrant upsert. It may be NULL when Qdrant was unavailable at creation time.
- After R1, every workspace-scoped table has a NOT NULL `workspace_id` with FK CASCADE — deleting a workspace deletes everything inside.
