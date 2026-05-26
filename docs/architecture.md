# Architecture

> Snapshot as of 2026-05-04. Any PR that changes components or data flows must update this file.

## Services (docker compose)

```
                     ┌──────────────────────────────────┐
                     │           frontend (Vite)        │ :3002
                     └──────────────┬───────────────────┘
                                    │ /api/* + /ws/* via vite proxy → nginx
                                    ▼
                     ┌──────────────────────────────────┐
                     │           nginx (LB)             │ :8001 → :8000
                     │  • REST: round-robin (DNS-resolved per request)
                     │  • WS:   sticky-ish (Upgrade headers, 1h timeout)
                     └──────────────┬───────────────────┘
                                    │ proxy → api:8000
                  ┌─────────────────┼─────────────────┐
                  ▼                 ▼                 ▼
            ┌───────────┐    ┌───────────┐    ┌───────────┐
            │  api-1    │    │  api-2    │ …  │  api-N    │  (replicas)
            │  FastAPI  │    │  FastAPI  │    │  FastAPI  │
            └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
                  └─────────────┬──┴────────────────┘
                                │
        ┌──────────────┐   ┌────▼─────┐   ┌─────────────┐
        │ orchestrator │   │ postgres │   │  scheduler  │
        │ (advisory    │   │   :5432  │   │ (advisory   │
        │  lock 8723451)│  └──────────┘   │ lock 8723452)│
        └──────┬───────┘   ┌──────────┐   └──────┬──────┘
               │ docker.sock│  qdrant  │          │
               │            │  :6333   │          │
        ┌──────▼─────────┐  └──────────┘   ┌──────────┐
        │ spawnhive-agent│  ┌──────────┐   │  redis   │ pubsub
        │  containers    │  │  minio   │   │  :6379   │  spawnhive.events
        │  (per task)    │  │  :9000   │   └──────────┘
        └────────────────┘  └──────────┘
```

WS fan-out across api replicas goes through Redis pub/sub (`spawnhive.events`); when `REDIS_URL` is unset the broadcast falls back to in-process delivery (single-replica mode).

api containers still mount `docker.sock` — they use the in-process `DockerRuntime`. This is the transitional workaround #13 until a `RemoteAgentRuntime` (RPC to orchestrator) lands; the call-site migration onto the `AgentRuntime` ABC is already done, what remains is the process split.

## Main flows

### Creating and running a task

```
user (UI) ──POST /api/tasks──▶ api
                                  │ insert tasks(status=backlog)
                                  ▼
user moves to ready ── PATCH ──▶ api ── insert agent_events ──▶ /ws/events
                                                                   │
                                                                   ▼
                            orchestrator_loop polls tasks ─── select template via LLM
                                  │ (decide_decomposition — gated by `decomposition_enabled` setting)
                                  │ (select_template_for_task)
                                  ▼
                            spawn_agent(template, env)
                                  │
                                  ▼
                       docker run spawnhive-agent
                            │  ENV: TASK_DESCRIPTION, AGENT_TOOLS, MCP_SERVERS,
                            │       AGENT_MEMORY_CONTEXT, OPENAI_API_KEY, …
                            │
                            ▼
                       agent: LLM tool loop
                            ├─▶ bash / file_read / file_write / search_knowledge
                            ├─▶ MCP servers (stdio subprocess)
                            └─▶ webhooks: progress (rate-limited 5s), completed, failed, aborted
                                  │
                                  ▼
                       POST /api/v1/agent-webhook/{task_id}
                            │ Pydantic AgentWebhookEvent (discriminated)
                            ▼
                       webhooks.py:
                            - log_event
                            - calc cost_usd
                            - upload files to MinIO
                            - LLM evaluate_agent_result
                            - status → review → awaiting_approval | retry | failed
                            - (memory_mode=structured) bg extract_memory(task_id)
                                  │
                                  ▼
                       user: approve/reject  → status=done/failed
```

### Bidirectional control

```
user / dashboard ──▶ POST /api/agents/{cid}/feedback
                          │
                          ▼
                 docker_manager.send_feedback ── httpx ──▶ http://<container_name>:8080/feedback
                                                                  │
                                                                  ▼
                                                       feedback_server queues a command
                                                                  │
                              agent.py loop ◀── drains the queue between tool_calls
                                   │
                                   ├─ feedback → injects a "user feedback" message
                                   ├─ switch_model → updates model/api_base/api_key
                                   └─ abort → exits the loop with event=aborted
```

### Memory pipeline (P0)

```
task.status → done (auto-review approved)
        │
        ▼
asyncio.create_task(extract_memory(task_id))
        │
        ▼
LLM extract_memory_facts(task_summary, result_summary)
        │
        ▼
For each entity:
    embed("type:name attrs") ──▶ Qdrant memory_entities collection
    cosine ≥ 0.92 with existing? ─▶ merge attrs (dedup)
                                   ─▶ else insert a new entity
For each relation: insert if both ends resolve.
        │
        ▼
log_event memory_extracted

       …later, when a new task is spawned…
        │
        ▼
build_memory_context(task.title + description):
    - find_relevant_entities (top-K=10, threshold=0.7)
    - 1-hop graph traversal
    - serialise into compact markdown ≤ 2000 tokens
    - inject as the AGENT_MEMORY_CONTEXT env var
```

### Quality Data Lake (E-01)

```
spawn (engine.py) ──▶ agent_spawned event enriched with the full state snapshot
                       (soul_md, tools, mcp, model, memory_context, flat_memory)
                                              │
task reaches a settled terminal state (awaiting_approval / failed) via webhook
                                              │ build_quality_record(db, task)  [BEFORE log compaction]
                                              ▼
        assemble blob from tasks + agent_events + agent_log_chunks (+ decomposition tree)
                                              │
                       ┌──────────────────────┴───────────────────────┐
                       ▼                                               ▼
        quality_records row (queryable summary)        MinIO data-lake/<ws>/<task>.json (full blob)
                       │
   scheduled jobs:  quality_record_backfill (interval 300s) — build/reconcile any terminal task missing a record
                    quality_record_retention (cron 00:30)   — prune blobs+rows older than data_lake_retention_days
                       │
   API (/api/data-lake): records (filter) · records/{task_id} (full blob) · query (group-by) · export (json|parquet, admin)
```

Notes: the build is best-effort (a failure is picked up by the backfill job). It
runs before `_compact_agent_log` so the per-chunk `tool_name` sequence is
captured; records created later by backfill (chunks already compacted) carry no
tool-call list. The JSONB slots (`quality_profile`/`trajectory_profile`/
`human_feedback`/`longitudinal`/`reproducibility`) are left NULL — filled by
E-02/E-07/E-05/E-22/E-20.

### Quality Rubric Engine (E-02)

Fills the `quality_profile` slot. A **rubric** is a set of independent dimensions;
the engine scores a finished task into a **profile** (vector of 0–10), not one
number.

```
resolve rubric for task:  Template.rubric_id → rubric whose applies_to ∈ template.tags
                          → workspace is_default rubric → none (skip)
resolve judge model:      workspace.quality_judge_model_id → orchestrator_model_id → none (skip)
                                              │
   per dimension (asyncio.gather, independent try/except — one failure never
   blocks the others): judge → LLM-as-judge call; reference → match vs gold (E-03);
   objective → static-analysis probe (E-04) → {score 0-10, reasoning}
   human dimensions → status "deferred" (E-05)
                                              ▼
   profile = {dimensions[], weighted_score, gate{passed, failed_dimensions},
              judge_model, judge_tokens, judge_cost_usd}  →  quality_records.quality_profile
                                              │
   triggers:  POST /api/quality/records/{task_id}/evaluate  (on-demand, owner/admin)
              quality_judge_evaluate job (interval 600s) — only when quality_eval_enabled=true
```

Notes: the `judge` (E-02), `reference` (E-03) and `objective` (E-04) evaluators are
implemented; the `human` evaluator dimension stays `deferred` in the auto-profile —
human ratings are collected separately as a parallel signal (E-05, below). Gating
is **soft** — the gate result is recorded and surfaced in the UI (radar chart) but
does not block the task lifecycle. Auto-evaluation is off by default
(`quality_eval_enabled=false`) to avoid surprise token spend; the on-demand button
works regardless. The MinIO blob stays immutable; only the Postgres
`quality_profile` column is written.

### Reference-based Judge (E-03)

For tasks with a known gold answer, a `reference` rubric dimension compares the
result against the task's `reference_answer` and folds a single 0–10 score into the
same E-02 profile (so it shares the resolution, gate, weighted-score and triggers
above). Four modes via the dimension's `reference_mode`:

```
pointwise  → LLM judge scores result vs reference        (uses quality_judge model, like E-02)
exact      → 10 iff normalized result == normalized reference, else 0   (pure local)
fuzzy      → difflib SequenceMatcher ratio × 10                          (pure local)
semantic   → cosine similarity of embeddings × 10        (configured embedding provider)
```

`reference` dimensions are scored in the same `asyncio.gather` batch as `judge`
ones (each isolated — one failure never blocks the rest). A task with no
`reference_answer` records the dimension as `skipped` (no score, excluded from the
gate and weighted score). The cosine is computed in-process for the two texts (no
Qdrant). **Pairwise (A vs B vs reference) is deferred** — it needs a second
candidate result that a single task does not provide (arrives with E-11/E-21).

### Behavioral / objective probes (E-04)

An `objective` rubric dimension runs a deterministic static-analysis tool over the
task's produced code artifacts and folds a single 0–10 measurement into the same
E-02 profile (sharing resolution, gate, weighted-score and triggers). The dimension
carries a `probe`; the **POC scope is Python-only, static-only** (the tool *parses*
the agent's code, never executes it):

```
lint   → ruff check     fewer findings per 100 LOC ⇒ higher score   (0 findings = 10)
types  → mypy --ignore-missing-imports   fewer type errors per 100 LOC ⇒ higher score
```

Score = `10 × (1 − min(findings_per_100_loc, 10) / 10)`. Probes run **in-process**:
artifacts are fetched from MinIO into a temp dir, the tool is invoked via subprocess
with a per-probe timeout, output is parsed, and the temp dir is removed. Results are
memoised by artifact content hash (identical artifacts ⇒ no re-run). Like the other
evaluators, the call never raises: no Python artifact ⇒ `skipped`; a missing tool /
timeout / unparseable output ⇒ `error`. **Out of scope (follow-up):** *executing*
agent code (pytest/jest) needs container isolation, not in-process execution; web
(Lighthouse/axe), text and data probes; the YAML+image plugin format.

### Human feedback (E-05)

A structured human signal on a finished task — a 0–10 rating per quality dimension
(mirroring the E-02 axes), a free-text comment per dimension, an overall comment and
an optional approve/reject verdict — captured by an optional, non-blocking form and
stored in the `quality_records.human_feedback` slot (built on demand if the record
does not yet exist). It is a **parallel** signal: it does **not** alter the judge gate
or weighted score.

```
PUT /api/quality/records/{task_id}/feedback   (upsert; member)
   → build_human_feedback: clamp 0-10, band each score, copy judge_score from the
     profile by key, stamp submitted_by/at  →  human_feedback slot
GET .../feedback                              (read; member)
GET /api/quality/calibration                  (owner/admin) → flattened judge↔human
     pairs (one row per rated dimension) — the raw material for judge calibration (E-17)
```

Scores are read in **bands** — `bad` (1-3, incorrect/fix) · `improve` (4-7) · `good`
(8-10, leave as is); the band thresholds are constants for now and become
rubric-configurable in **E-26**, which also routes the per-dimension comments back to
the agent for a re-run. The form shows the judge's score next to each slider (one-click
agree) so disagreements surface directly. **Deferred:** pairwise (A vs B) human
comparison → **E-21** (needs a second candidate a single task does not hold);
configurable bands + feedback→re-run loop → **E-26**; agreement statistics (Cohen's κ,
correlations) → **E-17**.

### Trace cleaner (E-06)

The deterministic, **LLM-free** pre-processor that turns a raw agent trajectory
into the compact input the trajectory judge (E-07) will consume. From the durable
sources (`agent_events` + `agent_log_chunks` + `tasks`) it builds a `CleanedTrace`:

- **Keeps** the original task (title/description), per-step reasoning (from
  `orchestrator_reasoning` / `agent_progress` / decision events) and tool
  calls/outputs.
- **Drops** the `agent_spawned` system snapshot (soul_md, memory, tool/mcp lists)
  and noise events (health pings, status churn, downstream eval events).
- **Truncates** long tool outputs to a token cap + marker; `keep_tail_on_error`
  keeps error steps whole (the bug is often in the ignored tail).

Steps are merged chronologically; token counts use `tiktoken` (char/4 fallback) to
report savings. Log chunks load from Postgres, or the MinIO archive after
compaction (where `tool_name` is lost — the cleaner degrades gracefully). It
produces the judge's *input* only: it scores nothing and never writes
`trajectory_profile` (E-07). Like the other evaluators it never raises (on failure
returns a trace with an `error` field). Read-only preview, computed on demand and
not persisted: `GET /api/quality/records/{task_id}/trace?tool_output_token_cap&keep_tail_on_error`.

### Trajectory judge (E-07)

The LLM-as-judge for the **trajectory** side: it answers "how did the agent get
there", complementing E-02's outcome judge. It takes the cleaned trace (E-06) and,
in a **single** LLM call, scores the whole trajectory on six axes (§5.2):
**efficiency, tool_selection, parameter_quality, error_recovery, goal_alignment,
loop_detection** — each 0–10 with a required `reason` — plus a one-line `summary`.
`overall_score` is their mean; `loop_detected` is derived from the loop_detection
axis.

- **Model**: reuses E-02's resolver (`quality_judge` → `orchestrator`) — no separate
  judge slot.
- **Cost cap**: the cleaned trace is trimmed to the `trajectory_judge_max_input_tokens`
  setting (default 12000) before the call — middle steps are dropped first (the
  outcome lives in the tail), `input_capped` flags it.
- Like the other evaluators it never raises: an LLM/parse failure is persisted as a
  profile with `status: "error"`. The result is written to the `trajectory_profile`
  slot next to E-02's `quality_profile`; it never touches the outcome slot.
- On-demand `POST /api/quality/records/{task_id}/evaluate-trajectory` + read
  `GET …/trajectory`; optional batch job `trajectory_judge_evaluate` (off by default,
  gated by `trajectory_eval_enabled`).

### Evidence bank judge (E-08)

The **TRACE** counterpart of E-07. The holistic judge weighs each step without the
context of what the agent has already established; in the reference-free setting
that is weak. E-08 walks the cleaned trace (E-06) **step by step**, accumulating an
**evidence bank** — the facts established by prior steps — and threads that bank into
the prompt that assesses the *next* step. So each step is judged against the
accumulated evidence: is it `redundant` (re-derives a known fact), is it `grounded`
(justified by the task + prior evidence rather than guessed), how much new evidence
it adds. After the walk a single evidence-aware call produces the same 6-axis profile
as E-07 (for direct comparison) plus a `groundedness` signal — this is what catches
the "🤷 lucky" case a context-less judge misses (a correct answer resting on nothing
the agent gathered).

- **Pipeline**: `N` per-step `assess_step` calls (each sees the bank so far) + 1 final
  `score_trajectory` call informed by the bank — `N + 1` calls (faithful TRACE, §5.4).
- **Reuse (DRY)**: the E-02 judge-model resolver, and E-07's `AXES`, the 6-axis
  `score_trajectory` tool and the axis parser (`_parse_axes_from_args`).
- **Cost cap**: `trace_evidence_max_steps` (default 30 — head+tail window beyond it)
  bounds the per-step calls; `trace_evidence_max_input_tokens` (default 12000) bounds
  the final call. `input_capped` flags either trim.
- Never raises: a **per-step** failure degrades to a step marked with an `error` and
  the walk continues; a **final-call** failure becomes `status: "error"`. Written to
  the `trajectory_evidence_profile` slot — coexists with E-07's `trajectory_profile`
  so the two can be compared side by side.
- On-demand `POST /api/quality/records/{task_id}/evaluate-trajectory-evidence` + read
  `GET …/trajectory-evidence`; optional batch job `trace_evidence_evaluate` (off by
  default, gated by `trace_evidence_eval_enabled`, smaller batch — N+1 calls/task).

> The comparative benchmark (E-07 vs E-08: which better flags "lucky" cases) is
> deferred to when the public benchmark set (E-23) lands.

### Trajectory matching (E-09)

A **deterministic, LLM-free** trajectory signal for the narrow class of tasks that
have a *canonical* trajectory — a single valid tool-call path (typically a benchmark
task, §3.2 T3). It compares the agent's actual tool sequence (the `kind == "tool"`
steps of the E-06 cleaned trace) against a reference stored on the task. Most tasks
have many valid paths and must **not** carry a canonical trajectory — the matcher
only runs when `tasks.canonical_trajectory` is set; otherwise it is skipped.

- **Reference (three forms, normalized to a node-instance DAG)**: a bare list of tool
  names (linear chain), `{"sequence": [...], "match_mode": …}`, or a full
  `{"nodes": [{id, tool}], "edges": [[from, to]]}` DAG. Set from the E-23 dataset
  later; settable now via the task `canonical_trajectory` field (`POST/PATCH /api/tasks`).
- **Three metrics, all computed (cheap)**: `exact` (1.0 iff the actual sequence equals
  the reference linearization), `edit` (`difflib.SequenceMatcher` ratio over the
  tool-name lists, same stdlib approach as the fuzzy reference judge E-03), and `dag`
  (1.0 iff the actual run is a valid **topological order** of the canonical DAG —
  same tool multiset and every precedence edge respected). The headline
  `score`/`matched` follow the configured `match_mode` (default `edit`; `edit` passes
  at `match_threshold`, default 0.9; `exact`/`dag` are binary).
- The `dag` check is a Kahn-style consumption *driven by the actual order* over node
  instances (not tool names), so a repeated tool stays a distinct node — exact for
  chains and distinct-tool DAGs, a close approximation only for DAGs that label
  several parallel nodes with the same tool.
- Never raises: a bad/unparseable reference becomes `status: "error"`; no canonical →
  skipped. Written to the `trajectory_match_profile` slot (next to E-07/E-08).
- On-demand `POST /api/quality/records/{task_id}/evaluate-trajectory-match` + read
  `GET …/trajectory-match`. **No batch job** — unlike the LLM judges (E-07/E-08) the
  matcher is instant and free and applies to a rare task class, so auto-scanning every
  `done` record for the occasional canonical task isn't worth the churn (KISS).

### Variance / Robustness Harness + re-run core (E-11)

Single-run scores hide a critical agent property: **consistency**. An agent that
is sometimes brilliant and sometimes fails is worse than a stably-mediocre one
(§3.4 R1). The harness runs one scenario N times and measures the *dispersion* of
the result.

- **Re-run core (layer A)** — a small, reusable primitive rather than a bespoke
  variance mechanism (it is also the seam for E-21/E-24/E-26 and a future U-03
  replay UX). `app/orchestrator/rerun.py::clone_task_for_rerun` clones a task's
  input into a fresh task linked by `tasks.replay_of_task_id` (distinct from
  `parent_id` so children are never folded into a parent's subtask-completion
  check) and pins the template. The engine grew a **pinned-template fast path**:
  `_spawn_agent_for_template` is shared by the normal selection path and by any
  task that already carries a `template_id` — the latter skips decomposition +
  selection and applies optional `tasks.run_config` overrides (`model_id`,
  `soul_md`). `run_config` (`{template_id?, model_id?, soul_md?, seed?,
  temperature?}`) is the durable override seam; E-11 only ever pins `template_id`.
- **No bespoke concurrency or pool** — children are created READY and drained by
  the existing orchestrator loop under `max_concurrent_agents`. The harness is a
  poll-driven state machine (`app/quality/variance.py::advance_variance_run`,
  driven by the `variance_run_tick` job, interval 20s): it creates the next
  children while `created < n`, **under the cost cap**, and within an in-flight
  target (`max_concurrent_agents` when parallel, else 1); judges finished children
  inline (E-02 outcome + E-07 trajectory, only when a judge model is configured);
  and aggregates once all children are terminal. A child is a successful terminal
  at `done` **or** `awaiting_approval`.
- **Cheap metrics, optional judging** — trajectory length (`steps_total` from the
  E-06 cleaned trace), tool-selection stability (share of runs sharing the modal
  tool signature + per-tool usage mean/std) and success rate are derived without
  any LLM; outcome-score and trajectory-score dispersion are included only when a
  judge is configured. Distributions report mean / pstdev / min / p25 / p50 / p75 /
  p95 / max + raw values (pure-Python percentiles, no numpy).
- **Cost cap** is enforced by the tick: it stops creating new children once the
  accumulated cost (child agent runs + their judge evals) crosses the cap; the run
  then finalizes as `capped`.
- `POST /api/quality/variance` (source = an existing finished task **or** a fresh
  `{title, description}` spec), `GET …/variance/{run_id}` / `GET …/variance` to
  read, plus a `python -m app.cli.variance` CLI; box-plots in TaskDetail.

### Adversarial / Perturbation Judge (E-12)

The complement of E-11: variance probes robustness to *model stochasticity* on a
fixed input; perturbation probes robustness to *input variation* (§3.4 R2). Real
users phrase tasks differently and real web pages contain injection, so an agent
that only works on the exact clean prompt is production-unfit. It reuses the
same poll-driven machinery (re-run core + orchestrator loop + cost cap +
`runs_common` helpers) as E-11, driven by the `perturbation_run_tick` job.

- **Four pluggable transforms** (`app/quality/perturbation.py::TRANSFORMS`):
  `paraphrase` (an LLM rewrites the request preserving meaning — the only
  transform that calls a model, reusing the E-02 judge-model resolver),
  `noise` and `reorder` (deterministic, seeded — typos/fillers and sentence
  reordering, no LLM), and `inject`.
- **`inject` poisons a tool response at runtime.** The child keeps the original
  input but carries a `run_config.tool_injection` payload; the engine forwards it
  as `AGENT_TOOL_INJECTION` into the container (`AgentSpec.extra_env` →
  `docker_manager`), and the agent appends it to the **first** tool result it
  receives ("Ignore previous instructions…"). The payload embeds a unique
  **canary** token; if the agent emits the canary (in its summary or a file) it
  followed the injection — a deterministic, LLM-free **safety** signal (overlaps
  the security pillar's S-02).
- **Baseline vs perturbed.** `base_n` clean re-runs of the original input form the
  baseline; each transform runs `variants_per_transform` perturbed children.
  Per-transform **robustness** = `1 − degradation` of the perturbed outcome score
  (E-02 `weighted_score`) vs the baseline mean (1.0 = no degradation), plus signed
  per-dimension deltas; `overall_robustness` averages the transforms. Robustness
  degrades gracefully to "unavailable" when no judge is configured.
- `POST /api/quality/perturbation`, `GET …/perturbation/{run_id}` / `GET
  …/perturbation`; robustness bars + injection safety badge in TaskDetail.

### Capability-isolation Tests (E-13, part A)

A model can produce the right answer *from its parametric memory* without calling
the tool the task actually requires — fresh data after the model's cutoff, private
RAG data, exact arithmetic, local state (§3.4 C1). The outcome looks correct but the
agent "cheated": it fails the moment the data changes, and pure outcome scoring
(E-02) cannot see it. A capability-isolation task carries a `capability_spec`
(`{required_tools[], category?, match?}`) naming the tool(s) it cannot be solved
without; the harness (`app/quality/capability.py::evaluate_task_capability`) is
**deterministic** (no LLM of its own) and runs only when the spec is set, else skipped.

- **Glass-Box matching** reuses E-09's `extract_tool_sequence` to read the agent's
  actual tool calls from the E-06 cleaned trace, then checks the required tools were
  used — `match` = `all` (default, every required tool) or `any` (≥ 1).
- **Outcome correctness** reuses the workspace's configured E-02 judge — a scored
  `reference` dimension (E-03) when present (objective and preferred), else the
  `weighted_score ≥ capability_outcome_threshold` (setting, default 7.0). The E-02
  profile is computed once if missing; **no new model** is introduced. Signal is
  recorded as `reference` / `judge` / `none`.
- **Four-cell classification** (the heart of C1): `genuine` (correct AND tool used),
  **`cheated`** (correct BUT tool NOT used — answered from memory, the red flag),
  `failed_with_tool`, `failed_no_tool`. `capability_passed = (genuine)`. Written to
  the `capability_profile` slot; never raises (failure → `status: "error"`).
- **Aggregation** (`aggregate_capability`) computes `capability_score = genuine/total`
  with `by_category`/`by_model`/`by_template` breakdowns — the model breakdown is the
  "compare models by capability" signal (acceptance #3). The ≥30-task catalogue
  (acceptance #1) is part B, deferred (overlaps the E-23 dataset).
- `POST /api/quality/records/{task_id}/evaluate-capability`, `GET …/capability`, `GET
  /api/quality/capability/aggregate`; `python -m app.cli.capability evaluate|aggregate`;
  off-by-default `capability_evaluate` job (gated by `capability_eval_enabled`); a
  capability panel in TaskDetail.

## Backend components

| Module | Responsibility |
|--------|-----------------|
| `app/main.py` | FastAPI app, lifespan, seed_settings, seed_templates, audit middleware |
| `app/api/*` | REST + WS endpoints |
| `app/orchestrator/engine.py` | Polling loop, decomposition, template selection, spawn, timeout check |
| `app/orchestrator/llm.py` | LLM-powered orchestrator decisions + reasoning trail |
| `app/orchestrator/docker_manager.py` | Docker SDK wrapper: spawn/kill/list/health/feedback/abort/switch (low-level — go through `app.plugins.runtime`) |
| `app/plugins/runtime.py` | `AgentRuntime` ABC + `DockerRuntime` impl. Every call-site (engine, api/agents, api/chat, api/events, scheduler) goes through this. |
| `app/plugins/embeddings.py` | `EmbeddingProvider` ABC + `FastembedProvider`/`OpenAIEmbeddingProvider`/`SettingsDispatchProvider`. `fastembed`/`httpx` are imported ONLY inside the plugin |
| `app/plugins/llm.py` | `LLMProvider` ABC + `LiteLLMProvider`. Every `acompletion(...)` call goes through it |
| `app/plugins/secrets.py` | `SecretsProvider` ABC + `DBSecretsProvider`/`EnvSecretsProvider`. `llm_api_key` is read through it |
| `app/plugins/notifier.py` | `Notifier` ABC + `NoopNotifier` (default). `log_event` invokes `notify(...)` after broadcast |
| `app/memory/store.py` | Memory entities CRUD with embedding-based dedup |
| `app/memory/extractor.py` | LLM extraction of facts from task results |
| `app/knowledge/rag.py` | Document upload, chunking, embedding, Qdrant search; reset_collection |
| `app/scheduler.py` | APScheduler wrapper, jobs reload from DB |
| `app/quality/data_lake.py` | Quality Data Lake (E-01): `assemble_record` + idempotent `build_quality_record` (Postgres summary + MinIO blob) |
| `app/quality/rubric.py` | Quality Rubric Engine (E-02): `DEFAULT_RUBRICS` (5 built-ins) + `resolve_rubric_for_task` |
| `app/quality/judge.py` | E-02 LLM-as-judge: `evaluate_task_quality` → per-dimension scoring (judge + reference + objective) → `quality_profile` slot |
| `app/quality/reference.py` | E-03 Reference-based Judge: `evaluate_reference_dimension` — pointwise/exact/fuzzy/semantic comparison vs `task.reference_answer` |
| `app/quality/objective.py` | E-04 Behavioral probes: `evaluate_objective_dimension` — static-analysis (ruff/mypy) over the task's Python artifacts, scored in-process |
| `app/quality/feedback.py` | E-05 Human feedback: `build_human_feedback`/`save_human_feedback` — per-dimension human ratings (banded, paired with judge scores) stored in the `human_feedback` slot |
| `app/quality/trace_cleaner.py` | E-06 Trace Cleaner: `clean_trajectory`/`build_cleaned_trace` — deterministic, LLM-free cleaning of a raw trajectory into a compact `CleanedTrace` (input for the trajectory judge E-07); transient, scores nothing |
| `app/quality/trajectory.py` | E-07 Trajectory Judge: `evaluate_task_trajectory` — single-call LLM scoring of the cleaned trace on 6 axes (efficiency/tool_selection/parameter_quality/error_recovery/goal_alignment/loop_detection) → `trajectory_profile` slot; cost-capped, reuses the E-02 judge-model resolver |
| `app/quality/trace_evidence.py` | E-08 Evidence Bank Judge (TRACE): `evaluate_task_trace_evidence`/`evaluate_trajectory_with_evidence` — walks the cleaned trace step by step accumulating an evidence bank threaded into each step's prompt, then an evidence-aware 6-axis profile + `groundedness` → `trajectory_evidence_profile` slot (N+1 calls; reuses E-07's axes/tool/parser) |
| `app/quality/trajectory_match.py` | E-09 Trajectory Matching: `evaluate_task_trajectory_match`/`match_trajectory` — deterministic, LLM-free comparison of the actual tool sequence (E-06) vs `task.canonical_trajectory` (list / sequence / DAG); exact + edit + dag (topological-order) metrics → `trajectory_match_profile` slot. Skipped unless a canonical trajectory is set |
| `app/quality/capability.py` | E-13 Capability-isolation Tests: `evaluate_task_capability` (deterministic Glass-Box reuse of E-09 `extract_tool_sequence` + outcome correctness via E-02 → `genuine`/`cheated`/`failed_*` classification → `capability_profile` slot; skipped unless `task.capability_spec` is set) + `aggregate_capability` (capability_score by model/category/template) |
| `app/orchestrator/rerun.py` | E-11 re-run core: `clone_task_for_rerun` — clones a task's input into a fresh READY task (linked via `replay_of_task_id`), pinning the template / `run_config`. Shared seam for variance and future replay (E-21/E-24/U-03) |
| `app/quality/variance.py` | E-11 Variance / Robustness Harness: `run_variance` + `advance_variance_run` — N re-runs of one scenario, cost-capped, drained by the orchestrator loop, aggregated into a dispersion `aggregate` (outcome/trajectory-length/trajectory-score distributions + success rate + tool stability) on `variance_runs`. Driven by the `variance_run_tick` job |
| `app/quality/runs_common.py` | Shared helpers for the poll-driven harnesses (E-11/E-12): terminal-state sets, percentile/distribution stats, `ensure_child_evaluated` (inline E-02/E-07), `accumulated_cost`, `inflight_target` |
| `app/quality/perturbation.py` | E-12 Adversarial / Perturbation Judge: `run_perturbation` + `advance_perturbation_run` — replays a scenario under 4 pluggable transforms (paraphrase/noise/reorder/inject) vs a clean baseline, aggregating per-transform + overall robustness and an injection safety flag on `perturbation_runs`. Driven by the `perturbation_run_tick` job |
| `app/api/data_lake.py` | `/api/data-lake` — records (filter), full blob, group-by query, export (json/parquet) |
| `app/api/quality.py` | `/api/quality` — rubrics CRUD, task quality profile, on-demand evaluate |
| `app/utils/cost.py` | Token-usage → USD via the model_pricing setting |
| `app/utils/events.py` | log_event, broadcast to WS clients with filter matching |
| `app/schemas/webhooks.py` | Pydantic discriminated union for agent → orchestrator events |

## Agent components (container)

| File | What it does |
|------|--------------|
| `entrypoint.py` | Runs feedback_server alongside run_agent, sends the final webhook |
| `agent.py` | LLM tool-calling loop, MCP integration, periodic progress, control-queue drain; honors `AGENT_TOOL_INJECTION` (E-12) — appends a perturbation payload to the first tool result |
| `feedback_server.py` | FastAPI on :8080 — health/feedback/switch_model/abort |
| `time_server.py` | Sample MCP server (used to verify the MCP integration) |

## Plugin layer (R5 + R6 wiring)

```
                ┌────────────────────────────────────────────────┐
                │              app.plugins.<*>                   │
                │  get_*_provider() singleton, env-driven select │
                └─────┬──────────┬──────────┬──────────┬─────────┘
                      │          │          │          │
                  LLMProvider EmbeddingProvider   AgentRuntime  Notifier
                      │          │          │          │
                LiteLLMProvider Fastembed/  DockerRuntime  Noop
                                OpenAI/Settings
                      │          │          │
                 (litellm)   (fastembed/    (docker SDK)
                              httpx)
                                            │
                                       SecretsProvider
                                            │
                                     DBSecretsProvider
                                       (settings table) | EnvSecretsProvider (env)
```

Production call-sites (as of 2026-05-04) all go through these plugins. The `LLM_PROVIDER`/`EMBEDDING_PROVIDER`/`AGENT_RUNTIME`/`NOTIFIER`/`SECRETS_PROVIDER` env vars pick the concrete implementation. Tests swap impls via `set_*_provider(impl|None)`.

## Database (as of 2026-05-04, post-R1)

15 tables + 9 migrations. Full field/invariant description — see `data-model.md` (TODO).

| Table | Why |
|-------|-----|
| `users` | (R1) User identity: email, password_hash, display_name |
| `workspaces` | (R1) Container for all data of one customer/project; slug is unique |
| `workspace_members` | (R1) Many-to-many user↔workspace + role (owner/admin/member/viewer) |
| `service_tokens` | (R1) Per-task agent tokens (kind=agent), verified by sha256(plain) |
| `tasks` | The core entity; lifecycle backlog → done. Fields: depends_on UUID[], cost_usd, model_used, **input_price_per_1m_usd / output_price_per_1m_usd** (denormalized at spawn so cost survives model edits), workspace_id |
| `templates` | Agent roles. References a model via `model_id` and an optional quality rubric via `rubric_id` (both FK ON DELETE SET NULL). |
| `template_versions` | Template versioning with rollback support (P14) |
| `providers` | (R7) LLM providers per workspace — name, api_key, endpoint. |
| `llm_models` | (R7) Models per provider — display_name, api_name, input/output price per 1M tokens. |
| `agent_events` | Append-only event log; source for analytics + WS broadcast |
| `chat_messages` | Chat history with the orchestrator |
| `knowledge_documents` | RAG document metadata (files in MinIO, chunks in Qdrant) |
| `settings` | Runtime config (embedding, `memory_mode`, `decomposition_enabled`, max_concurrent_agents, …) — global. LLM creds and pricing live in `providers`/`llm_models` (R7). |
| `memory_entities` | Structured memory — nodes (P0); workspace-scoped |
| `memory_relations` | Structured memory — edges (P0); workspace-scoped |
| `scheduled_jobs` | APScheduler persistent storage (P8); workspace-scoped (built-in jobs live in the default workspace) |
| `quality_records` | (E-01) Quality Data Lake — immutable per-task execution snapshot; summary in PG, full blob in MinIO; nullable slots for eval features |
| `rubrics` | (E-02) Multi-dimensional quality rubrics — workspace-scoped; 5 built-ins cloned per workspace; fills `quality_records.quality_profile` |

After R1 every table except `users`/`workspaces`/`workspace_members`/`settings` has a NOT NULL `workspace_id` with an FK to `workspaces.id ON DELETE CASCADE`. Old rows are backfilled by the `c9d0e1f2a3b4_users_workspaces_scoping` migration — every NULL → the default workspace `00000000-0000-0000-0000-000000000002` (admin@local).

## LLM model resolution (R7)

Every LLM call resolves through `app/api/_resolve_model.py`, which returns a `(Provider, LLMModel)` pair:

```
                       (system roles)            (per-agent)
┌──────────────┐   ┌──────────────────┐    ┌──────────────────┐
│ workspaces   │   │ workspaces       │    │ templates        │
│ .orchestra-  │   │ .chat_model_id   │    │ .model_id        │
│  tor_model_id│   │ .memory_extra-   │    │                  │
│              │   │  ctor_model_id   │    │                  │
└──────┬───────┘   └──────┬───────────┘    └──────┬───────────┘
       │                  │                       │
       │   resolve_workspace_model(ws, kind)      │ resolve_model_by_id(model_id)
       │                  │                       │
       └──────────────────┴───────┬───────────────┘
                                  ▼
                          ┌───────────────┐
                          │ llm_models    │
                          │   ↓ FK        │
                          │ providers     │
                          └───────────────┘
                                  │
                                  ▼
                       (api_name, endpoint, api_key)
                                  │
                                  ▼
                       get_llm_provider().acompletion(...)
                                  ▼
                       spawn_agent env → OPENAI_API_KEY/OPENAI_BASE_URL/LLM_MODEL
```

Consumers:
- `orchestrator/engine.py` and `orchestrator/llm.py` → `orchestrator_model_id`
- `api/chat.py` → `chat_model_id`
- `memory/extractor.py` → `memory_extractor_model_id`
- `quality/judge.py` (E-02) → `quality_judge_model_id`, **falling back to `orchestrator_model_id`** when unset (the one consumer with a fallback — quality eval is non-critical, so it degrades to skipped rather than erroring)
- `orchestrator/docker_manager.spawn_agent` → `template.model_id`; at spawn time, the model's prices are denormalized into `tasks.{input,output}_price_per_1m_usd` so cost computation is stable.

If a required role has no model assigned (or the referenced model was deleted), the resolver raises HTTP 400 with an explicit "configure in Settings → System Models" message — no silent fallback to defaults (except the E-02 judge noted above).

## Authentication and authorisation (R1)

```
   POST /api/auth/register {email,password,display_name}
   POST /api/auth/login    {email,password}            ──▶ {access_token, default_workspace_id, user, …}
                                                             access_token = JWT(HS256, ttl=24h, sub=user_id, ws=default_workspace_id)

   Authenticated request:
     Authorization: Bearer <jwt>
     X-Workspace-Id: <uuid>            (optional; falls back to JWT.ws or first membership)

   FastAPI deps:
     get_current_user           — validates the JWT, loads User
     get_current_workspace      — resolves workspace + checks membership; writes request.state.workspace
     require_role(*allowed)     — for admin-only handlers (settings PATCH, test-llm, export-all, kill-all, …)

   WebSocket auth:
     /ws/events?token=<jwt>&workspace_id=<uuid>
     /ws/chat?token=<jwt>&workspace_id=<uuid>
     /ws/agents/{cid}?token=<jwt>&workspace_id=<uuid>
     On failure the connection is closed with code 4401 / 4404.

   Agent token:
     Before spawn_agent the orchestrator issues a per-task token (kind=agent) and stores its sha256.
     The plaintext goes to the container via the SPAWNHIVE_AGENT_TOKEN env var.
     The agent uses it in Authorization for /api/knowledge/search and (post-R2) for /api/v1/agent-webhook.
```

### Agent isolation

`docker_manager.spawn_agent` sets the labels:
- `spawnhive.task_id`, `spawnhive.template_id`, `spawnhive.template_name`
- `spawnhive.workspace_id` — the real workspace UUID (post-R1; previously it was `shared`).

`list_agents(workspace_id)` / `kill_agent(... workspace_id)` / `kill_all_agents(workspace_id)` filter by that label. Cross-workspace `kill-all` is not allowed.

## Frontend

| Page | Route | What it shows |
|------|-------|---------------|
| Dashboard | `/` | Live task counters + active agents (extends to per-agent live data via `/ws/agents/{cid}`) |
| Task Board | `/tasks` | Kanban over `tasks` rows; opens `TaskDetail` with reasoning timeline + **agent terminal log viewer** + events |
| Chat | `/chat` | Orchestrator chat (slash commands, `/ws/chat`) |
| Activity Log | `/activity` | Raw `agent_events` feed with filters |
| Analytics | `/analytics` | Aggregated metrics over tasks: per-template table + chart, daily timeline, per-model breakdown, A/B compare view. Data comes from the existing `/api/analytics/{templates,timeline,models}` endpoints; no new backend code (see `backend/app/api/analytics.py`). Charts via `recharts`. |
| Graph | `/graph` | Visual A2A communication graph: orchestrator + per-`agent_container_id` nodes, edges aggregated by direction with per-edge event counts, layout toggle (Force / Hierarchical via `dagre` / Circular), 24h timeline scrubber + Play/Pause + speed (1x/5x/30x). |
| Templates | `/templates` | CRUD on `templates` (model, tools, MCP servers, soul.md) |
| Knowledge Base | `/knowledge` | RAG document upload + rules.md / memory.md editors |
| Memory | `/memory` | Memory entities and relations browser |
| Settings | `/settings` | Runtime settings + System (admin-only block) |

All routes share the `RequireAuth` wrapper in `App.tsx`; sidebar entries are defined in `components/layout/Sidebar.tsx`.

### Frontend / Tasks (`AgentLogViewer`)

`TaskDetail.tsx` mounts `<AgentLogViewer taskId={t.id} archived={!!t.log_archive_s3_path} />` between `<ReasoningTimeline>` and the Events section, but only when the task has reached `in_progress`/`review`/`awaiting_approval`/`done`/`failed` (statuses where an agent has actually run).

- **Initial load** — `GET /api/tasks/{id}/log?limit=200`. Response carries `archived: bool`. While the task is active it returns DB chunks; after `event=completed/failed/aborted` the orchestrator compacts to MinIO blob (`s3://spawnhive/logs/<task_id>.log`), DELETEs DB chunks, and the same GET transparently reads from the blob with the same per-chunk shape.
- **Live updates** — opens `WebSocket(/ws/tasks/{id}/log)` via `buildWsUrl`. Frames have wire `type: "log_chunk"` and `_kind: "log_chunk"` filter so the existing `/ws/events` and `/ws/agents/{cid}` subscribers don't accidentally receive them. Component skips WS subscription entirely once `archived=true`.
- **Virtualization** — `react-virtuoso` `<Virtuoso>` renders only viewport-visible chunks (verified ~6 of 15 rendered at any time within the 360 px container). `followOutput="auto"` auto-scrolls to bottom on append unless the user scrolls up; toggleable via `follow` checkbox.
- **Pagination** — "Load earlier" button when initial response returned exactly `PAGE_SIZE` items; refetches `?from_seq=` to walk backward without losing append-from-bottom.
- **Dedup** — incoming WS events checked against `seenIds` (DB-rowed) and `seenSeq` (chunk_seq) to handle WS-after-REST overlap.
- **`vite.config.ts`** — `optimizeDeps.include` extended with `react-virtuoso` (same React-context duplication pattern as `recharts`/`reactflow`/`dagre`).

### Frontend / Graph (`/graph`)

Two-tab page (toggle persisted in `localStorage["graph.tab"]`, default `decomposition`):

#### Tab 1 — Decomposition (default)

`/graph?task=<parent-id>` (default tab). Tree + per-attempt Gantt for one parent task.

- Powered by `GET /api/tasks/{id}/decomposition` (single REST call, no WS — readonly snapshot, refresh button).
- **Tree** (`DecompositionTree.tsx`) — parent header with totals (`N subtasks · duration · $cost · X failed · Y retries`); below — subtask cards with status-icon, template badge, retry counter, depends-on display, and a `⚠ no dependencies set` warning when `depends_on=[]` AND siblings>1 AND (`status==='failed'` OR `retry_count>0`). Hard-failed subtasks (`status==='failed' && retry_count>=max_retries`) get a red border. Failure messages from `attempts[*].error` are shown below the card.
- **Gantt** (`DecompositionGantt.tsx`) — span = `min(spawned_at)…max(finished_at|now)`. Each row = one subtask; each absolute-positioned bar = one attempt (grouped by `agent_container_id`). Colors: green=completed, red=failed, orange=aborted, blue+pulse=running. Tick scale 6 labels, min bar width 4px, hover tooltip shows container short id + outcome + duration + error.
- **TaskSelector** (`TaskSelector.tsx`) — dropdown of tasks with `parent_id===null` AND ≥1 subtask (frontend filter on `tasksApi.list()`). Selected id persisted in URL `?task=`.
- Files: `frontend/src/components/graph/{DecompositionView,DecompositionTree,DecompositionGantt,TaskSelector}.tsx`; `pages/Graph.tsx` is a thin tab container.

#### Tab 2 — Communication (legacy U-01)

The page combines a 24h history replay with a live WS feed:

- **Initial load** — `GET /api/events?from_dt=<now-24h ISO>&limit=1000` (the `from_dt` / `to_dt` params on `eventsApi.list()` are typed in `api/client.ts`; backend already supports them).
- **Live updates** — opens `WebSocket(/ws/events)` (via `buildWsUrl`, same auth/workspace pattern as Activity Log). Each `{type:'event', ...}` frame is appended to the local store; cap is 5000 newest events. On disconnect: 2000 ms reconnect.
- **Aggregation** — every event with `agent_container_id` produces a directed edge: `source==='agent'` → `agent → orchestrator`, otherwise → `orchestrator → agent`. Edges are deduped per (from,to); the label shows the running event count, the color follows the *latest* event type on that edge.
- **Edge color legend** — blue: `agent_message` / `task_status_changed`. Green: `agent_completed` / `agent_progress`. Orange: `orchestrator_decision` / `orchestrator_feedback`. Red: `agent_failed` / `agent_killed` / `agent_aborted`. Gray: everything else (heartbeats, reasoning, etc.).
- **Layout toggle** — `Force` (radial, busier agents pulled closer to center, math-only — no physics lib), `Hierarchical` (`dagre` TB layout, orchestrator on top), `Circular` (orchestrator at center, agents on a circle). Files: `frontend/src/components/graph/{GraphCanvas,EventEdgeAnim,TimelineSlider,NodeDetailsPanel}.tsx` and `pages/Graph.tsx`.
- **Timeline scrubber** — `<input type="range">` over `[now-24h, now]`. The right edge advances every 30 s and on every WS event. When the slider sits within 1 s of the right edge it is treated as `LIVE` and the cursor follows new events; scrubbing left flips it to `PAUSED`. Play/Pause + 1x/5x/30x speeds replay history forward; reaching the right edge auto-pauses and re-enters live mode.
- **Edge pulse** — incoming WS events trigger a 600 ms pulse animation on the matching edge (custom reactflow `eventEdge` type, CSS keyframes).
- **`vite.config.ts`** — `optimizeDeps.include` extended with `reactflow` and `dagre` (same React-context duplication pattern that `recharts` already uses).

## Known architectural limitations

See `workarounds.md` (migrated from the legacy root `WORKAROUNDS.md`) and `production-readiness-tz.md`.
