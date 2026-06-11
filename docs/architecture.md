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

### Failure Mode Classifier (E-14)

"Pass/fail" is too coarse. For research signal — "model X suffers tool confusion in
23% of runs" (§3.4 F1) — the *type* of failure must be classified. On top of the
trajectory judge (E-07), an LLM (`app/quality/failure_modes.py::evaluate_task_failure_modes`)
labels the trajectory with zero or more failure classes. It runs on **every** terminal
task (a clean run yields no labels — so it also surfaces "succeeded with a defective
process"), and reuses the E-02/E-07 judge resolver (`quality_judge` → `orchestrator`),
the E-06 cleaned trace, and the input-token cap pattern — no new model.

- **Input** = the E-06 cleaned trace plus, as grounding context when already present,
  the E-02 outcome profile (correct/incorrect + score) and the E-07 trajectory profile
  (axis scores + `loop_detected`). The existing profiles are read as-is — **never
  re-run** (cheap); `used_outcome_profile`/`used_trajectory_profile` record what was fed.
- **Six base classes** (extensible via `FAILURE_CLASSES`): `tool_confusion`,
  `parameter_blind`, `loop`, `premature_stop`, `hallucinated_tool_result`,
  `ignored_error`. One LLM call returns a **multi-label** list — each label a
  `{class, confidence (0..1), reason}` — validated against the taxonomy (unknown
  classes dropped, de-duplicated keeping the highest confidence). Written to the
  `failure_profile` slot; never raises (failure → `status: "error"`).
- **Cost** is bounded by the `failure_judge_max_input_tokens` setting (default 12000),
  trimming the trace to fit before the call (`input_capped` flag).
- **Aggregation** (`aggregate_failure_modes`) rolls runs up into per-class counts with
  `by_class`/`by_model`/`by_template` breakdowns and a per-class `rate` (count / runs) —
  the "distribution of failure types per (model, template)" deliverable (feeds E-24 and
  SPA-30 multi-agent failure attribution). `failure_class` narrows to runs carrying a
  class; `suite` restricts to one Benchmark Case Store suite.
- `POST /api/quality/records/{task_id}/evaluate-failure-modes`, `GET …/failure-modes`,
  `GET /api/quality/failure-modes/aggregate`; off-by-default `failure_mode_evaluate` job
  (gated by `failure_mode_eval_enabled`); a failure-mode panel in TaskDetail.

### Hallucination Detection (E-15)

Outcome correctness (E-02) and trajectory quality (E-07) can both look fine while the
agent's text fabricates **URLs, non-existent APIs, unsourced numbers, or invented
claims** (§3.4 Q-09). E-15 (`app/quality/hallucination.py::evaluate_task_hallucinations`)
is a **fact-checker over the finished run's deliverable** (`task.result_summary`) across
four categories — **URLs / APIs / numbers / citations** — writing a per-category
breakdown to the orthogonal `hallucination_profile` slot. It reuses the E-02/E-07 judge
resolver and input-token cap — no new model.

- **Hybrid approach** (precision where cheap, recall where needed):
  - **URLs — deterministic, in-trace only.** A URL extracted from the deliverable is
    "supported" iff it appears as a substring in some tool argument/result of the E-06
    cleaned trace (case-insensitive, scheme-less and trailing-slash tolerant). There is
    **no live HTTP check** — that avoids SSRF/rate-limit/external-dependency risk; a
    `hallucination_check_urls_live` flag is reserved for a future v2.
  - **APIs — hybrid.** Dotted `pkg.func(` symbols inside code fences are matched against
    the trace; a confirmed symbol is `kind: deterministic`. Unconfirmed symbols are
    handed to the LLM for a plausibility verdict (the LLM knows popular public libraries).
  - **Numbers & citations — one LLM call.** All number candidates (years and bare single
    digits filtered out) and claim sentences go to a single `classify_hallucinations`
    tool call alongside the unconfirmed APIs. **At most one LLM call per task**, and
    **zero** when the deterministic pass leaves nothing to ask.
- **Grounding, never re-run.** The E-02 outcome summary and, when present, the E-08
  evidence-bank facts are fed as context so the LLM knows what the trajectory actually
  established; the existing profiles are read as-is (`used_outcome_profile` /
  `used_trajectory_evidence` record what was fed).
- **Profile.** Each category has `{checked, hallucinated, items[]}` with every item
  tagged `kind: deterministic|llm` (LLM verdicts also carry `confidence`); the top-level
  `hallucination_rate` = `hallucination_count / items_total`. A clean deliverable yields
  rate 0. Never raises (failure → `status: "error"`); skipped when there is no judge
  model, no deliverable, or no trace.
- **Orthogonal slot, not a rubric dimension.** "Hallucination rate as a measurement in
  the E-02 profile" is realized as a separate slot on the same `QualityRecord` (like
  E-13 `capability_profile` / E-14 `failure_profile`), **not** as a `dimension` inside
  the E-02 rubric engine — the fact-check is independent of the outcome rubric.
- **Aggregation** (`aggregate_hallucinations`) rolls runs up into per-category
  `checked`/`hallucinated`/`rate` + `hallucinated_runs` with `by_category`/`by_model`/
  `by_template` breakdowns — the hallucination rate per (model, template). `category`
  narrows to runs with ≥1 hallucination in that category; `suite` restricts to one
  Benchmark Case Store suite.
- `POST /api/quality/records/{task_id}/evaluate-hallucinations`, `GET …/hallucinations`,
  `GET /api/quality/hallucinations/aggregate`; off-by-default `hallucination_evaluate`
  job (gated by `hallucination_eval_enabled`); a hallucination panel in TaskDetail.

### Confidence Calibration (E-16)

A model can be confident in a wrong answer and unsure of a right one; without
knowing how well stated confidence tracks actual correctness you cannot delegate
agent → agent (§3.4 Q-10). E-16 (`app/quality/calibration.py::evaluate_task_calibration`)
records, per finished task, the pair **(predicted_confidence, actual_correctness)**
in the orthogonal `calibration_profile` slot, and rolls the population up into
**ECE / Brier / a reliability diagram** with a per-model recommendation.

- **actual_correctness — reused from E-02.** Read via
  `app.quality.capability._outcome_from_profile` (a scored `reference` dimension if
  present, else `weighted_score ≥ calibration_outcome_threshold`, default 7.0). When
  neither exists the signal is `"none"` and the run is **skipped** — there is no
  ground truth to calibrate against. The E-02 profile is reused as-is and run once
  only when missing.
- **predicted_confidence — post-hoc self-probe.** Confidence exists nowhere in the
  system, so E-16 elicits it with **one LLM call**: the model re-reads the task + its
  own answer (`result_summary`) + the E-06 cleaned trace and reports
  `P(answer is correct) ∈ [0,1]` through a forced `assess_confidence` tool call. The
  probe **never sees the grader's verdict**, so correctness cannot leak into the
  confidence. The system prompt asks for calibrated estimates (reserve >0.9 for near-
  certain answers). Input is bounded by `calibration_judge_max_input_tokens` (default
  12000) via `_fit_trace_to_budget`. Never raises (failure → `status: "error"`).
- **Probe runs on the doer model.** `_resolve_doer_model` resolves the task's own model
  by `model_used` (api_name) within the workspace's providers, falling back to the
  E-02/E-07 judge resolver (`quality_judge → orchestrator`). Thus the per-model
  breakdown reflects each model's calibration of *itself*.
- **Profile.** The slot stores the raw pair plus its `brier_term`
  (`(confidence - actual)²`): `predicted_confidence`, `actual_correct`, `outcome_signal`
  (`reference|judge`), `outcome_score`/`outcome_threshold`, `confidence_source`
  (`self_probe`), `probe_model`, `reasoning`, judge tokens/cost, `input_capped`,
  `used_outcome_profile`. The headline calibration metrics are inherently population-
  level and computed at aggregate time, not per task.
- **Orthogonal slot, not a rubric dimension.** Calibration is a separate slot on the
  same `QualityRecord` (like E-13/E-14/E-15), **not** a `dimension` in the E-02 rubric
  engine — it measures confidence-vs-correctness, independent of the outcome rubric.
- **Aggregation** (`aggregate_calibration`) over scored profiles computes, overall and
  `by_model`/`by_template`: `ece` (Σ over non-empty buckets of `(count/total)·|avg_conf
  − accuracy|`), `brier`, `accuracy`, `avg_confidence`, `overconfidence` (avg_conf −
  accuracy), and a `reliability` diagram of `bins` (default 10) equal-width confidence
  buckets `{lo, hi, count, avg_confidence, accuracy}`. `_recommendation_for` turns each
  model's largest-gap bucket into plain language ("model X overestimates itself in the
  70–80% confidence zone"). `suite` restricts to one Benchmark Case Store suite.
- `POST /api/quality/records/{task_id}/evaluate-calibration`, `GET …/calibration`,
  `GET /api/quality/calibration/aggregate`; off-by-default `calibration_evaluate` job
  (gated by `calibration_eval_enabled`); a calibration panel (confidence bar + Brier +
  reliability diagram) in TaskDetail.

### Judge Calibration Protocol (E-17)

An LLM-judge metric is meaningless until it is validated against humans — the central
source of doubt about eval (RQ1, §7.1). E-17
(`app/quality/judge_calibration.py::run_judge_calibration`) answers "how far can the
judge be trusted" by comparing the judge's per-dimension scores (E-02,
`quality_profile.dimensions[]`) with human ratings on the same axes (E-05,
`human_feedback.dimensions[]`) over every record that carries both. It makes **no LLM
call** — pure agreement statistics over already-stored scores.

- **Pure-Python stats** (`app/quality/stats.py`, no scipy/numpy): `pearson`,
  `spearman` (Pearson on average-tie ranks), `cohen_kappa` (over the fixed band set),
  `score_to_band` (the human-feedback cuts: bad 0–3 / improve 4–7 / good 8–10),
  `mean_bias`. Any metric with fewer than `MIN_SAMPLES` (3) pairs returns `None` and the
  dimension is marked `insufficient_data`.
- **Shared pair collection.** `collect_judge_human_pairs` flattens one row per rated
  dimension across records with human feedback — the single source of truth for **both**
  the `GET /api/quality/calibration` export and the E-17 report (DRY). Each row carries
  the judge score (from the human dim's `judge_score`, falling back to the matching
  `quality_profile` dimension), the human score/band, the per-task `verdict`,
  `judge_gate_passed` (`quality_profile.gate.passed`) and `submitted_by`.
- **Report** (`_compute_report`). Per dimension: `n`, `pearson`, `spearman`,
  `cohen_kappa` (on bands), `mean_bias`, and `reliable` = band κ ≥
  `judge_calibration_min_kappa` (default 0.6, a workspace setting). An **overall
  verdict-agreement** dedupes to one (judge gate → approve/reject, human verdict) pair
  per task and reports κ + raw agreement %. `recommendations[]` turn each dimension into
  plain language ("judge reliable for Correctness (kappa=0.71, r=0.81)" / "judge diverges
  on Tool Selection …"). `n_humans` = distinct `submitted_by`.
- **Versioned artifact, not a per-task slot.** A judge calibration is per-(workspace,
  judge model), so it lives in its own `judge_calibrations` table — append-only,
  versioned per `judge_config_key` (the judge model's `api_name`; `_resolve_judge_model`,
  `unknown` when none). Re-running after a judge/rubric change keeps the old curves.
  `suite`/`template_id` filters scope the population (the loose mapping of the
  acceptance's `dataset_id`); they are recorded but do not fork the version line.
- `POST /api/quality/judge-calibration/run` (**owner/admin**), `GET …/judge-calibration`
  (latest, `?history=true` for the version list), `GET …/judge-calibration/badge`; a CLI
  (`python -m app.cli.judge_calibration run|show`); a workspace-level panel on the
  Analytics page (per-dimension reliability table + overall + history) and a
  "judge calibrated against N humans, κ=X.X" trust badge (also surfaced on the TaskDetail
  quality panel). No scheduler job — on-demand only.

### Bias Mitigation Toolkit (E-18)

Where E-17 *measures* judge↔human divergence, E-18 (`app/quality/bias_mitigation.py`)
*counteracts* the four known LLM-judge biases (§7.2). Two layers:

- **Live judge config.** Four `bias_mitigation_*` settings (free-form settings table,
  default off) are read by `evaluate_task_quality`. `verbosity` and `score_clustering`
  append a sentence to the judge **system** message (the no-mitigation path is
  byte-identical to before E-18, pinned by a test so E-02 goldens never drift);
  `self_preference` flags when the judge model is the same model/family as the agent
  model (`task.model_used`) via the name-prefix heuristic in
  `app/quality/model_identity.py` (no `family` column exists), recording the verdict
  under `quality_profile.bias_mitigation`; `position` is a reserved no-op until pairwise
  judging (E-21).
- **Bias report = controlled A/B re-judge.** `run_bias_report` takes the calibration
  population (`collect_judge_human_pairs`, shared with E-17), and for each task re-scores
  every human-rated judge dimension TWICE — mitigations OFF then ON — against an identical
  `_result_context`. The two passes are emitted as `collect_judge_human_pairs`-shaped rows
  and run through the **same** `_compute_report`, so before/after agreement-with-human is
  computed identically to E-17. On top it derives diagnostics with no extra LLM call:
  `verbosity` (length↔score Pearson off/on vs the human baseline), `score_clustering`
  (score spread + 7-8 share off/on), `self_preference` (judge==agent count + warning),
  `position_bias` (`status:"n/a"`). The gate is recomputed per pass over the rated judge
  dims so the overall verdict-agreement differs before/after. This is the **only**
  LLM-spending part of E-18 (`2 × judge-dims-with-feedback` calls): owner/admin, on-demand,
  dims-within-task concurrent / tasks sequential for rate-limit safety.
- Persisted append-only in **`bias_reports`** (mirrors `judge_calibrations`), versioned per
  `(workspace, judge model)`. `POST /api/quality/bias-report/run` (**owner/admin**),
  `GET …/bias-report` (+`?history`); CLI (`python -m app.cli.bias_report run|show`); a
  "Bias Mitigation" panel on Analytics (before/after κ table + diagnostics) and four toggles
  in Settings. No scheduler job.

### Aggregation Engine — Bradley-Terry / Elo (E-19)

Pointwise scoring (E-02) gives one number per task; the more robust way to **rank**
competitors is pairwise — many "A vs B" matches aggregated into a global rating with a
confidence interval. E-19 is that engine.

- **Pure core** (`app/quality/aggregation.py`, no DB / no LLM, the literal
  `rank(pairwise_results, method='bt'|'elo')` acceptance). A **match** is
  `{player_a, player_b, outcome:"a"|"b"|"tie", weight}`. `bradley_terry` fits the MLE
  strengths via the classic MM iteration with a small Bayesian **prior** (a virtual win+loss
  vs a phantom average opponent) so an undefeated player or a disconnected comparison graph
  still converges; `elo` averages sequential updates over several seeded-shuffled passes to
  remove order-dependence. Both map onto a shared **Elo scale** (centred on 1500). CIs come
  from a seeded bootstrap (resample matches, re-fit, take the central percentile band). All
  randomness is via `random.Random(seed)` → fully deterministic, pinned by tests.
- **Match source.** E-19's natural feed is the pairwise framework (**E-21**, now built — see
  below), which supplies **explicit** matches from real A/B verdicts. Independently,
  `app/quality/ranking.py::derive_matches_from_records` still bridges from pointwise scores when no
  pairwise data exists: within one `benchmark_case_id`, the model/template with the higher mean
  `quality_profile.weighted_score` "beats" the other (gap ≤ `ranking_tie_epsilon` → tie). The pure
  pairing (`build_matches`) is unit-tested separately. Both paths call the same `run_ranking`;
  `metrics.source` (`explicit` vs `derived`) records which fed a given leaderboard.
- **Subject** is selectable: rank `model` (`quality_record.model_used`) or `template`
  (`template_name`). Persisted append-only in **`ranking_reports`** (mirrors
  `judge_calibrations`), versioned per `(workspace, ranking_key)` where `ranking_key =
  "{subject}:{method}"`. `POST /api/quality/ranking/run` (**owner/admin**), `GET …/ranking`
  (+`?history`), `GET …/ranking/badge`; CLI (`python -m app.cli.ranking run|show`); a
  "Leaderboard" panel on Analytics (rating + 95% CI bar + W/L/T, with model/template and
  BT/Elo selectors). No LLM call, no scheduler job.

### Reproducibility Snapshot (E-20)

Models drift and LLMs are stochastic, so an experiment is only meaningful if the exact state
that produced it is recorded (§7.4). E-20 (`app/quality/reproducibility.py`) captures an
**experiment_snapshot** for every quality record, diffs two snapshots, and replays a run from
its snapshot.

- **Capture is automatic and free** (no LLM call). The snapshot inputs are already gathered at
  spawn time into the `agent_spawned` event and surfaced as the data lake `blob["execution"]`
  (soul_md / tools / mcp_servers / model_api_name / memory_context / flat_memory / template_*).
  `build_quality_record` (terminal-state) materializes them into the **reserved**
  `quality_records.reproducibility` JSONB slot via a best-effort `_safe_snapshot` hook — so it's
  **per-record, no new table** (unlike the E-17/18/19 reports). Backfill of older records is free:
  the `quality_record_backfill` job re-runs the builder.
- **Honest about gaps.** `assemble_snapshot` (pure) hashes large text into the fingerprinted
  `determinism` block and keeps it raw-capped under `content`; the `manifest` lists `captured`
  vs. `missing` with `notes`. The runtime does not expose `temperature` or tool **versions**, and
  point-in-time RAG vectors aren't captured (the `memory_context` string is); `seed` is present
  only for benchmark-materialized runs — all of these are marked missing rather than faked, so
  reproducibility is never overstated.
- **Fingerprint** (`snapshot_fingerprint`) is a SHA-256 over the canonicalized `determinism` block
  only (sorted keys, `tools`/`mcp_servers` sorted, volatile `captured_at`/raw `content` excluded),
  so equal runs ⇒ equal fingerprint. `diff_snapshots` (pure) reports added/removed/changed by
  dotted path with a human summary.
- **Replay** reuses the existing re-run primitive (`clone_task_for_rerun`, the U-03 seam):
  `replay_from_snapshot` derives a `run_config` from the snapshot (pins `template_id`, passes
  `soul_md`/`seed`/`temperature` where captured) and clones the task, linked via
  `replay_of_task_id`. Determinism is honestly bounded to "same template + prompt (+ seed/temp
  where available)". `GET …/records/{id}/reproducibility`, `POST …/records/{id}/capture-reproducibility`
  (**owner/admin**), `GET …/reproducibility/diff`, `POST …/records/{id}/replay` (**owner/admin**);
  CLI (`python -m app.cli.reproducibility show|diff|replay`); a "Reproducibility" panel on Analytics
  (snapshot inspector with captured/missing chips + diff viewer + replay).

### Pairwise Comparison Framework (E-21)

Pointwise judging (E-02) clusters everything into 7-8 (§7.2); the more reliable, human-natural
signal is **pairwise** — "which is better, A or B?". E-21 (`app/quality/comparison.py` + table
`pairwise_comparisons`) builds the A/B pipeline and finally feeds E-19 *real* matches instead of
pointwise-derived ones, turning E-18's deferred `position` no-op into a working mitigation.

- **Two candidate sources.** **Direct**: compare two finished tasks by id (`status="ready"`).
  **Generated**: candidate B is produced on the fly by re-running `source_task_id` with a
  `b_run_config` override (`model_id`/`template_id`/`soul_md`) via `clone_task_for_rerun` — the
  same U-03 seam variance/perturbation use — then advanced by the `pairwise_run_tick` scheduler
  job (`generating → ready → judged`, terminalizing to `failed` if B fails; no setting gate, cost
  only for user-created comparisons).
- **Position-bias mitigation (the E-18 deliverable).** `judge_pair_llm` judges the same pair in
  **both orders** — `(A,B)` and `(B,A)` — with a forced `choose_winner` tool-call (judge model via
  `_resolve_judge_model`, as E-02) and reconciles: agree → that winner; disagree → `tie` and
  `position_bias_detected=true`. Both per-order verdicts are stored in `judge_detail`. Two LLM
  calls per judged pair; `mitigate_position=False` (one call) is exposed to *measure* raw bias. A
  failed judge stores the error and leaves the comparison `ready` (retryable) — it never 500s.
- **Human mode + E-17 linkage.** `judge_mode="human"` skips the auto-judge; `record_human_verdict`
  records an `a`/`b`/`tie` winner on the **same row** as the judge verdict, so judge↔human
  `judge_agreement` is row-local (the calibration signal E-17 consumes).
- **ELO via E-19 — the designed hand-off.** `comparisons_to_matches` turns judged verdicts into
  real `{player_a, player_b, outcome, weight}` matches (self/incomplete/unverdicted dropped) and
  `run_pairwise_leaderboard` feeds them to `run_ranking(matches=…, method="elo")` → a versioned
  `ranking_report` (`source="explicit"`) shown in the **existing** Leaderboard tab. It writes the
  same `ranking_key="{subject}:{method}"` as E-19's derived path — intentional; `metrics.source`
  (`explicit` vs `derived`) distinguishes them, no new key. Players resolve on the `subject` axis
  (`model`→`model_used`, `template`→template name, `prompt`→a `soul_md` label); prompt comparisons
  still produce verdicts but aren't an E-19 axis, so they don't get an ELO board.
- **Surface.** `POST /api/quality/comparison` (direct/generated, **owner/admin**), `GET …/comparison`
  (+agreement), `GET …/comparison/{id}` (+`side_by_side`), `POST …/comparison/{id}/judge`,
  `PUT …/comparison/{id}/human-verdict`, `POST …/comparison/leaderboard` (all writes owner/admin);
  CLI (`python -m app.cli.comparison create|generate|show|list|judge|leaderboard`); a "Pairwise"
  panel on Analytics (side-by-side answers, LLM-judge with position-bias readout, human winner
  picker, "Push to ELO leaderboard").

### Tool & MCP Registry (SPA-41)

Tools and MCP servers used to be configured inline on every template (`Template.tools` /
`Template.mcp_servers`), which duplicated config, scattered credentials, and made A/B benchmark runs
error-prone (forget a tool on one configuration → unfair comparison). SPA-41 makes them a
**workspace-level registry** (`registry_entries` + `app/registry/`) that templates and experiments
reference by id.

- **Big-bang migration** (`a5b6c7d8e9f0`): a pure `dedupe_for_migration` collapses every template's
  inline builtins (by name) and MCP servers (by canonical config; name collisions suffixed) into
  registry rows, rewrites each template to a `tool_ids` reference list, and drops the inline columns.
- **Resolution at spawn is the only hot-path change.** `resolve_template_tools(template, run_config)`
  loads the referenced entries (skipping disabled/missing), applies the task-level override
  `run_config.tools_override = {enable, disable}` (finest-restriction-wins: `disable` beats `enable`),
  and **materializes** them into the exact shapes the agent already consumes — a builtin tool-name
  list (`AGENT_TOOLS`) and MCP dicts `{name, command, args, env}` (`MCP_SERVERS`). The wiring is
  favorable: `engine.py` feeds the resolved set into the `AgentSpec`, the Docker runtime rebuilds its
  container env from the spec, so `docker_manager.py` needs no change. The resolved set is also written
  into the `agent_spawned` snapshot, keeping E-20 reproducibility honest.
- **Credentials**: `secrets` are stored plain (like `Provider.api_key`) and **masked on every API
  read** — only the resolver reveals them into container env. The `secrets` slot is the seam for a
  future S-06 Vault/encryption follow-up. **Connection test** is best-effort: builtin → ok, http MCP →
  a reachability probe, stdio MCP → shape validation (the live handshake runs in the agent sandbox).
- **Surface**: `/api/registry/tools` CRUD (+`/{id}/test`, writes owner/admin, delete guarded by a 409
  unless `force`), CLI `python -m app.cli.registry`, a "Tool & MCP Registry" section in Settings, and a
  registry multiselect in the template editor. New-workspace seeding copies the default workspace's
  registry and remaps the seeded templates' `tool_ids` so references are never cross-tenant.

### Benchmark Case Store (pre-E-23)

The eval engines need a **store of reusable task definitions** (with gold signals),
not just the data lake of *results* (E-01). This is the mirror image of the
`quality_records` result slots: a **case** carries `input` + a pluggable `gold`
envelope (`reference_answer`, `rubric`, `canonical_trajectory`, `capability_spec`, …),
and each future eval task plugs in by reading its gold key — no schema migration, same
pattern as the result slots. Almost every methodology/comparison task (E-13B, E-15,
E-16, E-17, E-21, E-23, U-02, V-22) consumes it.

- **Layer 1 — format (source of truth):** versioned YAML/JSON files under
  `backend/benchmarks/<suite>/`. Git is the store and its history; the same files are
  what E-23 will index in a table and publish (so no rework). See [`benchmarks.md`](benchmarks.md).
- **Layer 2 — loader + linkage** (`app/quality/benchmark.py`): `load_cases(suite)`
  parses + validates (pydantic); `materialize` turns a case into one or more runnable
  READY task instances — gold → the task's `reference_answer`/`canonical_trajectory`/
  `capability_spec`, an optional pinned template (engine fast path) + `run_config.model_id`
  override — each tagged with `benchmark_case_id`/`benchmark_suite`. Those are
  denormalized onto the `quality_record` (`build_quality_record`), so eval aggregation
  (e.g. `aggregate_capability(suite=…)`) can scope and group by suite × case × model.
- **Run / compare:** the orchestrator loop drains the READY instances on the pinned
  template/model; loading the same suite once per model gives the `by_model` comparison.
  CLI `python -m app.cli.benchmark suites|load|status|evaluate|aggregate`.
- **Out of scope here (→ E-23):** the registry table, catalogue API/UI, public
  publication, leaderboards. Layers 1+2 only (KISS).

### Experiment Runner / Benchmark execution path (SPA-40)

The eval bricks (E-01…E-21) become one user-facing instrument: a first-class
**Experiment** = frozen dataset × configuration matrix × `n_runs_per_cell`,
with evaluation always on and a statistical report at the end. Answers "model A
vs model B", "does the v2 prompt help", "which toolset / does orchestration
help" as one operation.

- **Benchmark execution path.** Experiment children are plain tasks carrying
  `run_config.benchmark_mode` + `origin='experiment'` + `max_retries=0`. The
  webhook takes `completed` straight to DONE (no inline LLM review, no
  approval status, no rejection retries — those would distort N-run semantics)
  and `failed` straight to FAILED; the quality record is still built on DONE.
  The board hides `origin='experiment'` tasks by default. No human in the loop.
- **`orchestrator: on|off` is a matrix axis.** `off` → the child pins
  `template_id`, so the engine fast path spawns it directly (no decomposition,
  no template selection — the orchestration layer adds a measurable variable,
  which is noise when benchmarking a configuration). `on` → no pin: full
  orchestration runs, decomposition children inherit the benchmark run_config
  (minus template-relative keys) + origin + `max_retries=0`, and the
  decomposed root gets a result-summary rollup so E-02 can judge it
  end-to-end. The report's orchestrator view compares the two sides.
- **Axes per configuration**: `template_id`, `model_id`, `temperature`/`seed`
  (passed to the agent as `LLM_TEMPERATURE`/`LLM_SEED`), `soul_md` (inline
  override), `tools_override` (SPA-41 registry refs — one toolset for all
  cells unless explicitly overridden = fair A/B), `memory_mode`
  (`off|flat|structured`). Matrix composition: explicit `configurations` list
  AND/OR cartesian `axes` product; deduped by canonical fingerprint; keyed
  `cfg-01…`.
- **Dataset freezing** (`app/quality/experiments.py`): all three sources
  (benchmark suite / existing tasks / uploaded JSONL cases) are normalized at
  create time into `experiments.dataset_cases` — reproducible even if suite
  files or source tasks change later. Children are tagged
  `benchmark_case_id=case_key`, `benchmark_suite="exp:<id>"`, so the whole
  E-01 plumbing (denormalization, suite filters) works unchanged.
- **Per-case rubric.** A frozen case may carry an inline `rubric`
  (`{name?, dimensions: [...]}`, validated on upload); at settle time it
  overrides the template/workspace rubric for E-02 scoring of that case's
  runs — mixed datasets (math + writing in one experiment) are judged with
  the right dimensions per case instead of one template-wide rubric.
- **Poll-driven runner** (same pattern as E-11/E-12): all cells are
  pre-created as `pending` `experiment_runs` rows at start; the
  `experiment_run_tick` scheduler job (20 s) settles finished runs (record +
  E-02 + optional E-07/E-14; E-20 auto-captured), denormalizes
  scores/cost/duration onto the run row, claims the next pending cells up to
  `min(max_parallel, max_concurrent_agents)`, enforces `budget_limit_usd`
  (hit → remaining cells `skipped`, status `capped`, partial results kept) and
  finalizes. Pause/resume/cancel are status flips the tick respects.
- **Report** (`app/quality/experiment_report.py`, cached on the experiment
  once terminal): per-config summary, heatmap (configs × rubric dimensions),
  Pareto frontier (quality ↑ × cost ↓ × time ↓), outcome × trajectory scatter,
  pairwise leaderboard (pointwise scores case-paired via E-19 `build_matches`
  + `rank`, Bradley-Terry/Elo + bootstrap CI), statistical significance per
  config pair × metric (Welch t-test with an exact pure-python t-CDF as the
  primary marker, Mann-Whitney U normal-approximation as the non-parametric
  check; ★ p<0.05), failure-mode breakdown, orchestrator on/off comparison.
- **Repro**: clone-with-changes (frozen dataset copied verbatim), re-run =
  clone + run; every run's E-20 snapshot lands in the quality record; CSV/JSON
  export is flat per-run rows. CLI:
  `python -m app.cli.experiment list|create|run|status|report`.

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
| `app/quality/capability.py` | E-13 Capability-isolation Tests: `evaluate_task_capability` (deterministic Glass-Box reuse of E-09 `extract_tool_sequence` + outcome correctness via E-02 → `genuine`/`cheated`/`failed_*` classification → `capability_profile` slot; skipped unless `task.capability_spec` is set) + `aggregate_capability` (capability_score by model/category/template/suite) |
| `app/quality/hallucination.py` | E-15 Hallucination Detection: `evaluate_task_hallucinations` (deterministic in-trace check of URLs + code-fence API symbols, one LLM call for numbers/claims/unconfirmed APIs reusing the E-02/E-07 judge → 4-category `{checked,hallucinated,items[]}` + `hallucination_rate` → `hallucination_profile` slot; skipped without judge/deliverable/trace) + `aggregate_hallucinations` (per-category rate by model/category/template/suite) |
| `app/quality/calibration.py` | E-16 Confidence Calibration: `evaluate_task_calibration` (one post-hoc self-probe on the doer model — `_resolve_doer_model` by `model_used`, judge fallback — reads task + answer + E-06 trace WITHOUT the verdict → `P(correct)`; paired with E-02 correctness via `_outcome_from_profile` → `(predicted_confidence, actual_correct, brier_term)` in `calibration_profile` slot; skipped without model/deliverable/correctness signal) + `aggregate_calibration` (ECE/Brier/reliability diagram + per-model recommendation by model/template/suite) |
| `app/quality/benchmark.py` | Benchmark Case Store (pre-E-23): `load_cases`/`materialize` — parse versioned case files (`backend/benchmarks/<suite>/`) and turn them into runnable READY task instances tagged with `benchmark_case_id`/`benchmark_suite` (gold → reference_answer/canonical_trajectory/capability_spec; pinned template + model override). Format + loader + linkage only; registry/API/UI/publication are E-23 |
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
