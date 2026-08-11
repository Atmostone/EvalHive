# SpawnHive — Documentation

This folder is the single source of truth for the project. Any code change that touches architecture, API, DB schema, configuration, or processes — must be accompanied by an update to the relevant file here.

## Rules for working with docs/

1. **Every feature/refactor → docs update in the same PR**. If you change a model field, update `data-model.md`. If you add an endpoint, update `api.md`.
2. **Don't multiply files without need**. One thorough section beats ten mini-pages.
3. **Don't duplicate docstrings / CLAUDE.md**. `docs/` answers "why and how it's built"; CLAUDE.md holds working rules for the AI assistant; docstrings cover individual functions.
4. **Write dated entries**. Every change — add a line to `CHANGELOG.md` (create one if it doesn't exist yet).

## Structure

| File | What's inside |
|------|---------------|
| [`overview.md`](overview.md) | What SpawnHive is, who it's for, the value proposition. |
| [`architecture.md`](architecture.md) | Components, data flows, diagrams. |
| [`data-model.md`](data-model.md) | Tables, indexes, invariants. |
| [`api.md`](api.md) | Endpoint list, contracts. Auto-generation from OpenAPI is desirable. |
| [`webhooks.md`](webhooks.md) | The agent → orchestrator contract, Pydantic schemas. |
| [`memory.md`](memory.md) | P0: structured memory, extraction, dedup. |
| [`scheduler.md`](scheduler.md) | P8: APScheduler, scheduled_jobs, built-in jobs. |
| [`development.md`](development.md) | How to run locally, migrations, tests. |
| [`benchmarks.md`](benchmarks.md) | File-first store of reusable benchmark task definitions (pre-E-23), aggregated by suite × case × model. |
| [`toolathlon.md`](toolathlon.md) | SPA-42: running the Toolathlon-GYM environment next to our stack (mock PostgreSQL profile + derived agent image). |
| [`workarounds.md`](workarounds.md) | Known shortcuts and the reasons behind them. (replaces the root WORKAROUNDS.md) |
| [`research-toolathlon-gym.md`](research-toolathlon-gym.md) | SPA-37 spike: Toolathlon-GYM as an external benchmark source for E-23 (verdict, license, integration plan). |
| [`CHANGELOG.md`](CHANGELOG.md) | Dated log of changes — one line per change, linked to the plan block / PR. |

## Where to start

If you just joined the project, read in this order:

1. `overview.md` — why
2. `architecture.md` — how
3. `development.md` — how to spin it up
4. `CHANGELOG.md` — what has changed recently
