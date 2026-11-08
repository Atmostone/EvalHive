"""Read historical Toolathlon pilot tasks; executable pilot commands are retired.

Use installed Benchmark SDK profiles and dataset revisions for new executions.
Historical task status remains available without rewriting recorded verdicts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.task import Task


def _fail(msg: str) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    raise SystemExit(1)


async def _create(args) -> None:
    _fail("Historical pilot execution is retired; create a run through Benchmark SDK")


async def _ready(args) -> None:
    _fail("Historical pilot execution is retired; create a run through Benchmark SDK")


async def _status(args) -> None:
    async with async_session() as db:
        task = (
            await db.execute(select(Task).where(Task.id == uuid.UUID(args.task_id)))
        ).scalar_one_or_none()
        if task is None:
            _fail("task not found")
        print(
            json.dumps(
                {
                    "task_id": args.task_id,
                    "status": task.status,
                    "result_summary": (task.result_summary or "")[:300],
                    "result_files": task.result_files or [],
                    "cost_usd": float(task.cost_usd) if task.cost_usd is not None else None,
                }
            )
        )


async def _verdict(args) -> None:
    _fail("Historical pilot execution is retired; create a run through Benchmark SDK")


def main() -> None:
    p = argparse.ArgumentParser(description="Historical Toolathlon pilot status")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="retired; select an installed SDK dataset")
    c.add_argument("--case", required=True)
    c.add_argument("--suite", default="toolathlon")
    c.add_argument("--workspace-id", required=True)
    c.add_argument("--template-id", required=True)
    c.add_argument("--model-id", required=True)
    c.add_argument("--harness-profile", help="Installed workspace harness profile UUID")

    r = sub.add_parser("ready", help="retired; execution belongs to the SDK coordinator")
    r.add_argument("--task-id", required=True)

    s = sub.add_parser("status", help="print task status JSON")
    s.add_argument("--task-id", required=True)

    v = sub.add_parser("verdict", help="retired; SDK evaluations are immutable")
    v.add_argument("--task-id", required=True)
    v.add_argument("--passed", required=True)
    v.add_argument("--log-tail", default="")
    v.add_argument("--launch-time", default=None)

    args = p.parse_args()
    fn = {"create": _create, "ready": _ready, "status": _status, "verdict": _verdict}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
