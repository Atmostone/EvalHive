# SpawnHive — Overview

## What it is

A self-hosted platform for orchestrating specialised AI agents. It takes a task → picks an agent template → spawns an isolated Docker container → the agent solves the task using its built-in tools and MCP servers → the result goes to review → the user approves/rejects.

## Who it's for

- Developers and researchers who need several narrowly-specialised agents for different tasks (research, coding, writing, devops…) under a single control plane.
- Teams who want self-hosting (privacy, control over models, customisation).
- People who want a visual orchestration layer on top of litellm/MCP without vendor lock-in.

## What makes it different

- **Templates as agent roles**: each template is `(model, soul_md, tools, mcp_servers, limits)`. The LLM provider can be overridden per template.
- **Structured Memory**: automatic extraction of entities/relations from task results, embedding-based dedup, relevant sub-graph injected into the agent.
- **Bidirectional control**: the orchestrator can send feedback / abort / switch_model into a live agent container.
- **MCP-first**: custom MCP servers plug in per template without code changes.
- **Local-first**: everything runs in Docker Compose; nothing leaves the host.

## Tech stack

| Layer | What |
|-------|------|
| Backend API | FastAPI + SQLAlchemy async + Alembic |
| Storage | PostgreSQL 16 |
| Vector | Qdrant |
| Object storage | MinIO (S3-compatible) |
| LLM abstraction | litellm |
| Embeddings | fastembed (local) or an OpenAI-compatible API |
| Scheduler | APScheduler |
| Agent runtime | Docker (via docker-py + socket mount) |
| Frontend | React 18 + Vite + TypeScript + TanStack Query |
| Graph viz | reactflow |
| LLM | Any OpenAI-compatible endpoint (configured via UI → Settings → Providers & Models, no hardcoded default) |

## Status

- ✅ Core MVP loop: kanban → orchestrator → agent → review → approve.
- ✅ 11 default agent templates.
- ✅ RAG (PDF/DOCX/MD/TXT), MCP servers, kill switch, kanban, chat WebSocket.
- ✅ Pre-backlog (P0–P14): structured memory, bidirectional channel, periodic progress, Pydantic webhook schemas, per-template model routing, cost calculation, analytics + reasoning trail, priority in polling, APScheduler, depends_on in decomposition, audit log, workspace_id labels (stub), per-agent WS, slash commands, versioned templates.
- ✅ Eval Phase 0 — **E-01 Quality Data Lake**: immutable, versioned per-task execution snapshots (Postgres summary + MinIO blob), with `/api/data-lake` query + parquet/JSON export, retention + backfill jobs, and nullable slots for downstream eval features (E-02/E-05/E-07/E-20/E-22).
- ✅ Eval Phase 0 — **E-02 Multi-dimensional Quality Rubric Engine**: per-task rubrics scoring results into a quality profile (vector of 0–10) via LLM-as-judge, with 5 built-in rubrics, custom rubric editor, radar-chart UI, soft threshold gating, and on-demand + async evaluation (fills the `quality_profile` slot).
- ✅ Eval Phase 1 — **E-03 Reference-based Judge**: a `reference` rubric dimension scores the result against a task's optional gold `reference_answer` and folds one 0–10 score into the E-02 profile. Modes: pointwise (LLM), exact, fuzzy (difflib), semantic (embeddings). Pairwise (A vs B) deferred — needs a second candidate (E-11/E-21).
- ✅ Eval Phase 1 — **E-04 Behavioral Testing Layer** (POC): an `objective` rubric dimension runs a deterministic static-analysis probe over the task's Python artifacts and folds one 0–10 measurement into the E-02 profile. Probes: `lint` (ruff), `types` (mypy), run in-process, memoised by artifact hash. POC scope is Python static analysis only; executing code (pytest), web/text/data probes and the docker-isolated plugin format are deferred.
- ✅ Eval Phase 1 — **E-05 Human Feedback Collection**: an optional, non-blocking form rates each E-02 axis 1–10 (banded incorrect 1-3 / improve 4-7 / correct 8-10) with per-dimension and overall comments and an approve/reject verdict, stored as a parallel signal in `quality_records.human_feedback`. The judge score is shown alongside each slider (one-click agree); a `/api/quality/calibration` export pairs judge↔human scores for E-17. Pairwise comparison deferred to E-21; configurable bands and feedback→agent re-run deferred to E-26.
- ✅ Eval Phase 2 — **E-06 Trace Cleaner**: a deterministic, LLM-free pre-processor that turns a raw agent trajectory (events + log chunks) into a compact, judge-ready `CleanedTrace` — keeping the task, per-step reasoning and tool calls, dropping the `agent_spawned` system snapshot and noise events, and truncating long tool outputs (with a `keep_tail_on_error` debug option). Reports token savings via tiktoken; previewed read-only at `GET /api/quality/records/{task_id}/trace`. Input for the trajectory judge (E-07) — it scores nothing and persists nothing.
- ✅ Eval Phase 2 — **E-07 6-axis Trajectory Judge**: the process counterpart of the outcome judge — an LLM scores the cleaned trace (E-06) on six axes in a single call (efficiency, tool_selection, parameter_quality, error_recovery, goal_alignment, loop_detection), each 0–10 with a reason, plus an overall score, derived `loop_detected` flag and summary, written to the `trajectory_profile` slot next to E-02's `quality_profile`. Reuses the E-02 judge-model resolver; cost is bounded by a configurable input-token cap; on-demand `POST …/evaluate-trajectory` + read `…/trajectory`, visualised as a radar panel in TaskDetail. Optional batch job off by default.
- ✅ Eval Phase 2 — **E-08 TRACE Evidence Bank Judge**: the contextual counterpart of E-07 — instead of judging the whole trajectory in one holistic call, it walks the cleaned trace step by step accumulating an **evidence bank** (facts established so far) that is threaded into each step's prompt, then produces the same evidence-aware 6-axis profile plus a `groundedness` signal that flags "🤷 lucky" cases (a correct answer not supported by the gathered evidence). `N+1` calls, bounded by `trace_evidence_max_steps`/`…_max_input_tokens`; reuses E-07's axes/tool/resolver; written to a new `trajectory_evidence_profile` slot (coexists with E-07 for comparison); on-demand `POST …/evaluate-trajectory-evidence` + read `…/trajectory-evidence`, shown as an evidence-bank panel in TaskDetail. Optional batch job off by default. The formal E-07-vs-E-08 comparison is deferred to the public benchmark (E-23).
- ✅ Eval Phase 2 — **E-09 Trajectory Matching (T3)**: a **deterministic, LLM-free** matcher for the narrow class of tasks with a *canonical* trajectory (a single valid tool-call path). It compares the agent's actual tool sequence (from the E-06 cleaned trace) against a reference stored on the task (`tasks.canonical_trajectory` — a list of tool names or a `{nodes, edges}` DAG) on three metrics: `exact` (sequence equality), `edit` (`difflib` ratio) and `dag` (the run is a valid topological order of the canonical DAG). The headline follows the configured `match_mode` (default `edit`); written to the new `trajectory_match_profile` slot. Only runs when a canonical trajectory is set (else skipped); on-demand `POST …/evaluate-trajectory-match` + read `…/trajectory-match`, shown as a match panel in TaskDetail. No batch job (instant, free, rare task class). Canonical trajectories normally come from the benchmark dataset (E-23); settable now via the task field.
- ✅ Eval Phase 3 — **E-11 Variance / Robustness Harness (R1)**: runs one scenario N times and measures the **dispersion** of the result instead of a single point estimate — an agent that is sometimes brilliant and sometimes fails is worse than a stably-mediocre one. Children are created via a small **re-run core** (`clone_task_for_rerun` + a `tasks.run_config`/`replay_of_task_id` seam and an engine pinned-template fast path that skips decomposition/selection) and drained by the existing orchestrator loop under `max_concurrent_agents`; a `variance_run_tick` scheduler job creates the next children under a **cost cap**, judges finished ones (E-02/E-07 when configured) and aggregates. Reports distributions (mean/std/p25-p50-p75-p95) of outcome-score, trajectory length and trajectory score, plus success rate and tool-selection stability. `POST/GET /api/quality/variance` + `python -m app.cli.variance`, visualised as box-plots in TaskDetail. The same re-run core is the seam for E-21/E-24/E-26 and a future full U-03 replay UX.
- ✅ Eval Phase 3 — **E-12 Adversarial / Perturbation Judge (R2)**: the complement of E-11 — instead of robustness to model stochasticity on a fixed input, it probes robustness to **input variation**. Replays a finished scenario through four pluggable transforms — `paraphrase` (LLM rewrite, reusing the E-02 judge model), `noise` and `reorder` (deterministic, no LLM), and `inject` (a prompt-injection payload appended to the first **tool response** at runtime via `run_config.tool_injection` → `AGENT_TOOL_INJECTION`) — plus `base_n` clean baseline runs. Compares each perturbed outcome profile against the baseline → per-transform and **overall robustness score** (1.0 = no degradation), with a deterministic, canary-based **safety flag** for whether the agent followed the injection (overlaps the security pillar's S-02). Reuses the E-11 poll-driven machinery (re-run core, orchestrator loop, cost cap, `runs_common` helpers) via a `perturbation_run_tick` job. `POST/GET /api/quality/perturbation`, with robustness bars + an injection safety badge in TaskDetail.

## What's next

See [`production-readiness-tz.md`](production-readiness-tz.md) — work to be done **before** the main `BACKLOG.md` starts. After that — backlog features (visual A2A graph, benchmarks, replay, explainability, etc.).
