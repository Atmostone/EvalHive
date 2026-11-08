"""Run installed native datasets and inspect historical file-store results.

``load`` requires explicit workspace benchmark, dataset and harness references.
It freezes and starts a native matrix through the shared Benchmark SDK service.
``suites``, ``status``, ``evaluate`` and ``aggregate`` retain historical readers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections import Counter

from sqlalchemy import select

from app.database import async_session
from app.models.benchmark import BenchmarkDatasetRevision
from app.models.task import Task
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.benchmark import list_suites
from app.quality.capability import aggregate_capability, evaluate_task_capability

_TERMINAL = ("done", "awaiting_approval", "failed")


async def _suites(_args) -> None:
    for s in list_suites():
        print(s)


async def _load(args) -> None:
    from app.quality.experiments import create_experiment, start_experiment

    if args.template or args.model:
        raise ValueError("Choose a harness profile; legacy template/model execution overrides are retired")
    if not all(getattr(args, name, None) for name in ("benchmark_profile", "dataset_revision", "harness_profile")):
        raise ValueError("Native loading requires --benchmark-profile, --dataset-revision and --harness-profile")
    workspace_id = uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID
    async with async_session() as db:
        dataset = await db.scalar(select(BenchmarkDatasetRevision).where(
            BenchmarkDatasetRevision.id == uuid.UUID(str(args.dataset_revision)),
            BenchmarkDatasetRevision.workspace_id == workspace_id,
        ))
        if dataset is None or args.suite not in {dataset.dataset_id, dataset.provenance.get("suite")}:
            raise ValueError("Suite must identify the selected registered workspace dataset")
        experiment = await create_experiment(db, workspace_id=workspace_id, payload={
            "name": getattr(args, "name", None) or f"Native {args.suite} {uuid.uuid4().hex[:8]}",
            "dataset": {"source": "benchmark_sdk", "profile_id": str(args.benchmark_profile), "dataset_revision_id": str(args.dataset_revision)},
            "configurations": [{"harness_profile_id": str(args.harness_profile)}],
            "n_runs_per_cell": args.repeat, "max_parallel": 1,
        }, created_by="benchmark-cli")
        await start_experiment(db, experiment)
        print(json.dumps({"experiment_id": str(experiment.id), "status": experiment.status,
                          "dataset_id": dataset.dataset_id, "cases": len(experiment.dataset_cases),
                          "repetitions": experiment.n_runs_per_cell}))


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
    p = argparse.ArgumentParser(description="Native Benchmark SDK and historical Case Store")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("suites", help="list available suites").set_defaults(func=_suites)

    ld = sub.add_parser("load", help="freeze and start an installed native dataset matrix")
    ld.add_argument("--suite", required=True)
    ld.add_argument("--repeat", type=int, default=1, help="instances per case (samples)")
    ld.add_argument("--template", default=None, help="retired; use a harness profile")
    ld.add_argument("--model", default=None, help="retired; configure the harness profile model")
    ld.add_argument("--benchmark-profile", type=uuid.UUID, required=True)
    ld.add_argument("--dataset-revision", type=uuid.UUID, required=True)
    ld.add_argument("--harness-profile", type=uuid.UUID, required=True)
    ld.add_argument("--name", default=None, help="matrix name")
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
