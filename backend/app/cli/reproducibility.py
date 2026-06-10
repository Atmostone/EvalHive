"""CLI for Reproducibility Snapshots (E-20).

Inspect, diff and replay the experiment_snapshot captured for a task's eval run.
Run inside the api container:

    docker compose exec api python -m app.cli.reproducibility show --task-id <uuid>
    docker compose exec api python -m app.cli.reproducibility show --task-id <uuid> --capture
    docker compose exec api python -m app.cli.reproducibility diff --task-a <uuid> --task-b <uuid>
    docker compose exec api python -m app.cli.reproducibility replay --task-id <uuid>

``show`` prints the snapshot stored in ``quality_records.reproducibility``
(``--capture`` (re)builds it first). ``diff`` compares two snapshots. ``replay``
clones the task from its snapshot via the existing re-run primitive. No LLM calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.reproducibility import (
    capture_snapshot,
    diff_snapshots,
    replay_from_snapshot,
)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def _load_snapshot(db, task_id: uuid.UUID):
    rec = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task_id))
    ).scalar_one_or_none()
    return rec.reproducibility if rec else None


async def _show(args: argparse.Namespace) -> None:
    tid = uuid.UUID(args.task_id)
    async with async_session() as db:
        if args.capture:
            task = await db.get(Task, tid)
            if task is None:
                _print({"error": "task not found"})
                return
            snapshot = await capture_snapshot(db, task)
        else:
            snapshot = await _load_snapshot(db, tid)
    _print(snapshot)


async def _diff(args: argparse.Namespace) -> None:
    async with async_session() as db:
        a = await _load_snapshot(db, uuid.UUID(args.task_a))
        b = await _load_snapshot(db, uuid.UUID(args.task_b))
    if not a or not b:
        _print({"error": "both tasks must have a reproducibility snapshot"})
        return
    _print(diff_snapshots(a, b))


async def _replay(args: argparse.Namespace) -> None:
    tid = uuid.UUID(args.task_id)
    async with async_session() as db:
        try:
            out = await replay_from_snapshot(db, tid)
        except ValueError as e:
            out = {"error": str(e)}
    _print(out)


def main() -> None:
    p = argparse.ArgumentParser(description="Reproducibility Snapshot (E-20)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show", help="print the experiment_snapshot for a task")
    s.add_argument("--task-id", required=True)
    s.add_argument("--capture", action="store_true", help="(re)capture before printing")
    s.set_defaults(func=_show)

    d = sub.add_parser("diff", help="diff two tasks' snapshots")
    d.add_argument("--task-a", required=True)
    d.add_argument("--task-b", required=True)
    d.set_defaults(func=_diff)

    r = sub.add_parser("replay", help="replay a task from its snapshot")
    r.add_argument("--task-id", required=True)
    r.set_defaults(func=_replay)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
