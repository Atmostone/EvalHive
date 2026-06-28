# SpawnHive

**A self-hosted platform for deep, multi-dimensional evaluation and comparison of AI agents — where the evaluator itself is also measured.**

SpawnHive turns scattered "agent runs" into a reproducible, multi-dimensional, statistically grounded verdict: which agent / model / prompt / configuration is better, by how much, at what cost — **and whether the evaluation itself can be trusted**. It spawns agents in isolated Docker containers (a "hive" where agents are spawned), captures every run as an immutable record, and scores each run along two axes — *outcome* and *process* — while continuously validating its own judges against humans.

> The orchestration loop (task → pick an agent role → spawn an isolated container → the agent solves the task with its tools and MCP servers → review) is the **engine that produces runs to evaluate**, not the headline. The headline is the evaluation pipeline on top of it.

SpawnHive ships in **two modes**, toggled in the UI:
- **Work** — the full agent orchestrator it grew from: a kanban task board, agent selection per task, chat, an agent-communication graph, and structured memory.
- **Experiments** — the evaluation platform: the A/B runner, the ~25 evaluator modules, calibration and reports.

Both are built and working; orchestration is what produces the runs the evaluators score.

---

## Why

Agents are increasingly evaluated in two ways, and both are insufficient:

- **Binary pass/fail** from an executable checker is objective but exists only for verifiable tasks, is brittle, and says nothing about *how* the agent reached its result.
- **A single score from another LLM** ("LLM-as-a-judge") applies everywhere, but the judge is itself an unvalidated model that errs and is systematically biased.

The cost of an evaluator's error equals the cost of every decision made on its metric. SpawnHive is built around that idea: it not only measures agents, it **measures — and, when needed, quarantines — the reliability of the measurement itself**.

---

## Key features

**Two evaluation axes**
- **Outcome** — *what* was produced: a multi-dimensional rubric (accuracy, completeness, structure, clarity, …) instead of a single number, with reference-based scoring (exact / fuzzy / semantic) and deterministic static-analysis probes.
- **Process / trajectory** — *how* the agent worked: six axes (tool selection, parameter quality, error recovery, goal alignment, looping, efficiency), plus an evidence-bank judge that walks the trace step by step, and a deterministic trajectory matcher for tasks with a canonical tool path.

**Three independent oracles** on the same runs — an **executable checker** (deterministic verification of real side effects: DB rows, files, emails), an **LLM judge**, and a **human** — with bias mitigation (position / verbosity / self-preference) on the judge.

**Reliability gate over the judge** — the platform formally measures judge↔human agreement (Cohen's κ) per axis and **quarantines the axes it cannot trust**, so an unvalidated metric can't fake a "win". Measure and quarantine an unreliable judge first; only then trust it to compare and improve agents.

**Diagnostics** — a failure-mode classifier, a hallucination detector over the deliverable (URLs / APIs / numbers / citations), a glass-box capability check ("did it really use the required tool, or answer from memory?"), and model confidence calibration (ECE / Brier).

**Robustness** — variance across repeats and resistance to input perturbation (paraphrase, noise, reordering, prompt injection).

**A/B experiment runner** — a Cartesian matrix of *configurations × cases × repeats* (model, prompt, temperature, seed, toolset, memory mode, orchestrator on/off) over a benchmark dataset, in isolated environments, producing heatmaps, a cost/quality/time Pareto frontier, pairwise leaderboards (Bradley–Terry / Elo) and statistical significance.

**Reproducibility** — every run captures a snapshot (model, prompt, memory, tools, seed) with a deterministic fingerprint, so any run can be diffed and replayed.

---

## Architecture

A modular pipeline of ~25 independent evaluator modules. Each reads a run and appends its own structured profile (a JSONB slot) to the run record, so the evaluator set is easy to extend without touching the rest.

```
Agent in an isolated container
        │  events, tool calls, artifacts, logs
        ▼
Run capture ──►  PostgreSQL (denormalised summary) + MinIO (full immutable blob)
        │
        ▼
Trace cleaning  (deterministic, LLM-free)
        │
   ┌────┴───────────────┬───────────────────────────┐
   ▼                    ▼                            ▼
Executable          Outcome judge            Process judge
checker             (rubrics)                (6 axes + evidence bank)
(side effects)
   └────────────────────┴───────────────────────────┘
        │
        ▼
Human  ──►  Judge calibration (κ)  ──►  reliability gate
        │
        ▼
A/B runner: matrix of configurations × cases × repeats, isolated environments
        │
        ▼
Report: heatmaps · Pareto · significance · leaderboards · quarantine of unreliable axes
```

Pipeline layers: **capture** (durable record of every run) → **pre-processing** (deterministic trace cleaning) → **evaluation** (~20 of the ~25) → **judge validation** (calibration + bias control) → **experiment** (A/B matrix in isolated environments) → **report** (a pure roll-up of the accumulated profiles).

See [`docs/architecture.md`](docs/architecture.md) for the full design.

## Tech stack

| Layer | What |
|-------|------|
| Backend API | FastAPI + SQLAlchemy (async) + Alembic |
| Storage | PostgreSQL |
| Vector | Qdrant |
| Object storage | MinIO (S3-compatible) |
| LLM abstraction | litellm (any OpenAI-compatible endpoint) |
| Scheduler | APScheduler |
| Agent runtime | Docker (isolated container per run) |
| Frontend | React + Vite + TypeScript + TanStack Query + Recharts |
| Deployment | Docker Compose + nginx |

The agent LLM and judge LLM are configured at runtime (UI → Settings → Providers & Models) — no hardcoded provider.

---

## Quick start

Requires Docker and Docker Compose.

```bash
git clone https://github.com/Atmostone/SpawnHive.git
cd SpawnHive

# configure your LLM provider + a JWT secret
cp .env.example .env
#   set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL and JWT_SECRET in .env

docker compose up -d
```

Then open:
- **UI** — http://localhost:3002
- **API + OpenAPI docs** — http://localhost:8002/docs

Full setup, development workflow and test commands are in [`docs/development.md`](docs/development.md).

## Project structure

```
backend/      FastAPI app: API, evaluators (app/quality/), models, migrations, tests, benchmarks
frontend/     React + Vite SPA (experiments, analytics, calibration, reports)
agent-image/  Docker image the agents run inside
nginx/        reverse proxy
docs/         architecture, data model, API map, scheduler, webhooks, development
docker-compose.yml
```

## Documentation

- [Overview](docs/overview.md) — what it is, who it's for, what makes it different
- [Architecture](docs/architecture.md) — components, runtime, data flow
- [Data model](docs/data-model.md) — schema and migrations
- [API](docs/api.md) — endpoint map (source of truth: `/openapi.json`)
- [Benchmarks](docs/benchmarks.md) · [Scheduler](docs/scheduler.md) · [Webhooks](docs/webhooks.md) · [Development](docs/development.md)

## License

**Open, public source** under the [PolyForm Noncommercial License 1.0.0](LICENSE) — any **noncommercial** use is permitted (research, education, personal and evaluation use). Commercial use requires a separate license from the author.
