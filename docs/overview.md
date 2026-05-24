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

## What's next

See [`production-readiness-tz.md`](production-readiness-tz.md) — work to be done **before** the main `BACKLOG.md` starts. After that — backlog features (visual A2A graph, benchmarks, replay, explainability, etc.).
