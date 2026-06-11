"""CLI for the Experiment Runner (SPA-40).

Create and drive A/B matrix experiments from a JSON spec. Run inside the api
container:

    docker compose exec api python -m app.cli.experiment list
    docker compose exec api python -m app.cli.experiment create --file spec.json
    docker compose exec api python -m app.cli.experiment run <experiment-id>
    docker compose exec api python -m app.cli.experiment status <experiment-id>
    docker compose exec api python -m app.cli.experiment report <experiment-id>

The spec file is the same envelope as POST /api/experiments: {name, dataset,
configurations | axes, n_runs_per_cell, budget_limit_usd?, max_parallel?}.
`run` claims the first batch immediately; the experiment_run_tick scheduler
job advances the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter

from sqlalchemy import select

from app.database import async_session
from app.models.experiment import Experiment, ExperimentRun
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.experiment_report import compute_report
from app.quality.experiments import (
    TERMINAL_EXPERIMENT,
    advance_experiment,
    create_experiment,
    start_experiment,
)


async def _list(_args) -> None:
    async with async_session() as db:
        rows = (
            await db.execute(select(Experiment).order_by(Experiment.created_at))
        ).scalars().all()
    for e in rows:
        print(f"{e.id}  [{e.status:>9}]  {e.name}")


async def _create(args) -> None:
    workspace_id = (
        uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID
    )
    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)
    async with async_session() as db:
        exp = await create_experiment(
            db, workspace_id=workspace_id, payload=payload, created_by="cli"
        )
    print(f"created experiment {exp.id} '{exp.name}' (draft): "
          f"{len(exp.configurations)} configs × {len(exp.dataset_cases)} cases × "
          f"{exp.n_runs_per_cell} runs")


async def _run(args) -> None:
    async with async_session() as db:
        exp = await db.get(Experiment, uuid.UUID(args.experiment_id))
        if exp is None:
            raise SystemExit("experiment not found")
        await start_experiment(db, exp)
        await advance_experiment(db, exp)
        await db.refresh(exp)
    print(f"experiment {exp.id} is {exp.status}; the scheduler tick drives it from here")


async def _status(args) -> None:
    async with async_session() as db:
        exp = await db.get(Experiment, uuid.UUID(args.experiment_id))
        if exp is None:
            raise SystemExit("experiment not found")
        rows = (
            await db.execute(
                select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id)
            )
        ).scalars().all()
    counts = Counter(r.status for r in rows)
    print(f"{exp.name} [{exp.status}] — spent ${float(exp.accumulated_cost_usd or 0):.4f}")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")


async def _report(args) -> None:
    async with async_session() as db:
        exp = await db.get(Experiment, uuid.UUID(args.experiment_id))
        if exp is None:
            raise SystemExit("experiment not found")
        report = await compute_report(
            db, exp, method=args.method,
            partial=exp.status not in TERMINAL_EXPERIMENT,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


def main() -> None:
    p = argparse.ArgumentParser(description="Experiment Runner (SPA-40)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list experiments").set_defaults(func=_list)

    cr = sub.add_parser("create", help="create a draft experiment from a JSON spec")
    cr.add_argument("--file", required=True, help="spec file (POST /api/experiments envelope)")
    cr.add_argument("--workspace-id", default=None)
    cr.set_defaults(func=_create)

    rn = sub.add_parser("run", help="start a draft experiment")
    rn.add_argument("experiment_id")
    rn.set_defaults(func=_run)

    st = sub.add_parser("status", help="run counts by status")
    st.add_argument("experiment_id")
    st.set_defaults(func=_status)

    rp = sub.add_parser("report", help="print the assembled report as JSON")
    rp.add_argument("experiment_id")
    rp.add_argument("--method", default="bt", choices=["bt", "elo"])
    rp.set_defaults(func=_report)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
