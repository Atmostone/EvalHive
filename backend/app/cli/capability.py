"""CLI for the Capability-isolation Tests harness (E-13, part A).

Thin wrapper over ``app.quality.capability`` for the "compare models by
capability_score" acceptance. Run inside the api container:

    docker compose exec api python -m app.cli.capability evaluate --task <uuid>
    docker compose exec api python -m app.cli.capability aggregate
    docker compose exec api python -m app.cli.capability aggregate --category fresh_data
    docker compose exec api python -m app.cli.capability aggregate --model gpt-4o

``evaluate`` runs the deterministic harness for one task (reusing the configured
judge for outcome correctness). ``aggregate`` prints capability_score broken down
by model / category / template across the workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session
from app.models.task import Task
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.capability import aggregate_capability, evaluate_task_capability


async def _evaluate(args: argparse.Namespace) -> None:
    async with async_session() as db:
        task = await db.get(Task, uuid.UUID(args.task))
        if task is None:
            print("task not found")
            return
        profile = await evaluate_task_capability(db, task)
    if profile is None:
        print("skipped — task has no capability_spec (required_tools)")
        return
    print(json.dumps(profile, indent=2, ensure_ascii=False))


async def _aggregate(args: argparse.Namespace) -> None:
    workspace_id = uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID
    template_id = uuid.UUID(args.template) if args.template else None
    async with async_session() as db:
        agg = await aggregate_capability(
            db,
            workspace_id=workspace_id,
            category=args.category,
            model_used=args.model,
            template_id=template_id,
        )
    print(json.dumps(agg, indent=2, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Capability-isolation harness (E-13)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ev = sub.add_parser("evaluate", help="run the harness for one task")
    ev.add_argument("--task", required=True, help="task id to evaluate")
    ev.set_defaults(func=_evaluate)

    ag = sub.add_parser("aggregate", help="capability_score by model/category/template")
    ag.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    ag.add_argument("--category", default=None, help="filter by capability category")
    ag.add_argument("--model", default=None, help="filter by model_used")
    ag.add_argument("--template", default=None, help="filter by template id")
    ag.set_defaults(func=_aggregate)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
