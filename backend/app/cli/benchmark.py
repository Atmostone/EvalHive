"""CLI for the Benchmark Case Store (pre-E-23).

Materialize versioned case files into runnable task instances, watch them, evaluate
them, and aggregate by suite × model. Run inside the api container:

    docker compose exec api python -m app.cli.benchmark suites
    docker compose exec api python -m app.cli.benchmark load --suite capability-isolation --template <uuid> [--model <uuid>] [--repeat 1]
    docker compose exec api python -m app.cli.benchmark status --suite capability-isolation
    docker compose exec api python -m app.cli.benchmark evaluate --suite capability-isolation
    docker compose exec api python -m app.cli.benchmark aggregate --suite capability-isolation [--model <name>] [--category <c>]

`load` creates READY tasks (the orchestrator loop drains them); `evaluate` runs the
capability harness (E-13) on terminal instances; `aggregate` prints capability_score.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter

from sqlalchemy import select

from app.database import async_session
from app.models.task import Task
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.benchmark import list_suites, load_cases, materialize
from app.quality.capability import aggregate_capability, evaluate_task_capability

_TERMINAL = ("done", "awaiting_approval", "failed")


async def _suites(_args) -> None:
    for s in list_suites():
        print(s)


async def _load(args) -> None:
    workspace_id = uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID
    template_id = uuid.UUID(args.template) if args.template else None
    model_id = uuid.UUID(args.model) if args.model else None
    cases = load_cases(args.suite)
    total = 0
    async with async_session() as db:
        for case in cases:
            tasks = await materialize(
                db, case, workspace_id=workspace_id, repeat=args.repeat,
                template_id=template_id, model_id=model_id,
            )
            total += len(tasks)
            print(f"  {case.id}: {len(tasks)} task(s) → {[str(t.id) for t in tasks]}")
    print(f"materialized {len(cases)} case(s) × {args.repeat} = {total} task instance(s) "
          f"in suite '{args.suite}' (READY; the orchestrator will run them)")


async def _status(args) -> None:
    async with async_session() as db:
        rows = (
            await db.execute(select(Task).where(Task.benchmark_suite == args.suite))
        ).scalars().all()
    counts = Counter(t.status for t in rows)
    print(f"suite '{args.suite}': {len(rows)} instance(s)")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")


async def _evaluate(args) -> None:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Task).where(
                    Task.benchmark_suite == args.suite,
                    Task.status.in_(_TERMINAL),
                )
            )
        ).scalars().all()
        classes = Counter()
        for t in rows:
            prof = await evaluate_task_capability(db, t, commit=True)
            if prof and prof.get("status") == "scored":
                classes[prof.get("classification")] += 1
    print(f"evaluated {len(rows)} terminal instance(s) in suite '{args.suite}':")
    for cls, n in sorted(classes.items()):
        print(f"  {cls}: {n}")


async def _aggregate(args) -> None:
    workspace_id = uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID
    async with async_session() as db:
        agg = await aggregate_capability(
            db, workspace_id=workspace_id, suite=args.suite,
            category=args.category, model_used=args.model,
        )
    print(json.dumps(agg, indent=2, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Benchmark Case Store (pre-E-23)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("suites", help="list available suites").set_defaults(func=_suites)

    ld = sub.add_parser("load", help="materialize a suite into READY task instances")
    ld.add_argument("--suite", required=True)
    ld.add_argument("--repeat", type=int, default=1, help="instances per case (samples)")
    ld.add_argument("--template", default=None, help="pin a template (recommended for determinism)")
    ld.add_argument("--model", default=None, help="override the agent model (run_config.model_id)")
    ld.add_argument("--workspace-id", default=None)
    ld.set_defaults(func=_load)

    st = sub.add_parser("status", help="instance counts by status")
    st.add_argument("--suite", required=True)
    st.set_defaults(func=_status)

    ev = sub.add_parser("evaluate", help="run the capability harness on terminal instances")
    ev.add_argument("--suite", required=True)
    ev.set_defaults(func=_evaluate)

    ag = sub.add_parser("aggregate", help="capability_score by model/category for a suite")
    ag.add_argument("--suite", required=True)
    ag.add_argument("--category", default=None)
    ag.add_argument("--model", default=None, help="filter by model_used")
    ag.add_argument("--workspace-id", default=None)
    ag.set_defaults(func=_aggregate)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
