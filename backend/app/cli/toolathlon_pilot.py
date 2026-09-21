"""CLI: Toolathlon pilot task management (SPA-45).

Creates benchmark-path tasks for Toolathlon cases, flips them READY after the
host-side runner has seeded the workspace and run preprocess, and records the
external evaluation verdict. The host orchestration (preprocess/eval containers,
launch_time bookkeeping) lives in research/scripts/toolathlon_pilot.py — this CLI
is only the DB-touching half, run inside the api container:

    python -m app.cli.toolathlon_pilot create --case <case_id> \\
        --workspace-id <uuid> --template-id <uuid> --model-id <uuid> \\
        [--agent-image evalhive-agent-toolathlon:latest] [--suite toolathlon]
    python -m app.cli.toolathlon_pilot ready --task-id <uuid>
    python -m app.cli.toolathlon_pilot status --task-id <uuid>
    python -m app.cli.toolathlon_pilot verdict --task-id <uuid> --passed {true,false} \\
        [--log-tail <text>] [--launch-time <str>]

``create`` prints a JSON object {task_id, case_id, task_path, mcp_entries}; the
task starts in BACKLOG so the orchestrator cannot spawn it before the workspace
is seeded — ``ready`` performs the flip. ``verdict`` logs an
``external_eval_verdict`` AgentEvent tied to the task (the durable record the
pilot agreement analysis joins against quality profiles).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.registry_entry import RegistryEntry
from app.models.task import Task, TaskStatus
from app.quality.benchmark import load_cases
from app.utils.events import log_event


def _fail(msg: str) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    raise SystemExit(1)


async def _create(args) -> None:
    cases = {c.id: c for c in load_cases(args.suite)}
    case = cases.get(args.case)
    if case is None:
        _fail(f"case {args.case!r} not found in suite {args.suite!r} ({len(cases)} cases)")

    ws_id = uuid.UUID(args.workspace_id)
    servers = list((case.environment.mcp_servers if case.environment else None) or [])
    async with async_session() as db:
        names = [f"toolathlon-{s}" for s in servers]
        rows = (
            await db.execute(
                select(RegistryEntry).where(
                    RegistryEntry.workspace_id == ws_id,
                    RegistryEntry.name.in_(names),
                )
            )
        ).scalars().all()
        found = {r.name: r for r in rows}
        missing = [n for n in names if n not in found]
        if missing:
            _fail(f"registry entries missing (run toolathlon_import first): {missing}")

        task = Task(
            title=case.input.title[:500],
            description=case.input.description,
            status=TaskStatus.BACKLOG.value,
            workspace_id=ws_id,
            origin="experiment",
            template_id=uuid.UUID(args.template_id),
            run_config={
                "benchmark_mode": True,
                "template_id": args.template_id,
                "model_id": args.model_id,
                "agent_image": args.agent_image,
                "tools_override": {"enable": [str(found[n].id) for n in names]},
                "max_iterations": 100,
                "toolathlon_pilot": True,
            },
            max_retries=0,
            capability_spec=(
                case.gold.capability_spec if case.gold and case.gold.capability_spec else None
            ),
            benchmark_case_id=case.id,
            benchmark_suite="toolathlon-pilot",
        )
        db.add(task)
        await db.flush()
        out = {
            "task_id": str(task.id),
            "case_id": case.id,
            "task_path": (case.meta or {}).get("task_path"),
            "mcp_entries": names,
        }
        await db.commit()
    print(json.dumps(out))


async def _ready(args) -> None:
    async with async_session() as db:
        task = (
            await db.execute(select(Task).where(Task.id == uuid.UUID(args.task_id)))
        ).scalar_one_or_none()
        if task is None:
            _fail("task not found")
        if task.status != TaskStatus.BACKLOG.value:
            _fail(f"task is {task.status}, expected backlog")
        task.status = TaskStatus.READY.value
        await db.commit()
    print(json.dumps({"task_id": args.task_id, "status": "ready"}))


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
                    "cost_usd": float(task.cost_usd or 0),
                }
            )
        )


async def _verdict(args) -> None:
    passed = args.passed.lower() in ("true", "1", "yes", "pass")
    async with async_session() as db:
        task = (
            await db.execute(select(Task).where(Task.id == uuid.UUID(args.task_id)))
        ).scalar_one_or_none()
        if task is None:
            _fail("task not found")
        await log_event(
            db,
            "external_eval_verdict",
            "system",
            {
                "passed": passed,
                "benchmark_case_id": task.benchmark_case_id,
                "benchmark_suite": task.benchmark_suite,
                "launch_time": args.launch_time,
                "log_tail": (args.log_tail or "")[-2000:],
            },
            task_id=task.id,
            workspace_id=task.workspace_id,
            commit=True,
        )
    print(json.dumps({"task_id": args.task_id, "passed": passed, "recorded": True}))


def main() -> None:
    p = argparse.ArgumentParser(description="Toolathlon pilot task management (SPA-45)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a BACKLOG benchmark task for a case")
    c.add_argument("--case", required=True)
    c.add_argument("--suite", default="toolathlon")
    c.add_argument("--workspace-id", required=True)
    c.add_argument("--template-id", required=True)
    c.add_argument("--model-id", required=True)
    c.add_argument("--agent-image", default="evalhive-agent-toolathlon:latest")

    r = sub.add_parser("ready", help="flip a seeded task BACKLOG -> READY")
    r.add_argument("--task-id", required=True)

    s = sub.add_parser("status", help="print task status JSON")
    s.add_argument("--task-id", required=True)

    v = sub.add_parser("verdict", help="record external eval verdict as an AgentEvent")
    v.add_argument("--task-id", required=True)
    v.add_argument("--passed", required=True)
    v.add_argument("--log-tail", default="")
    v.add_argument("--launch-time", default=None)

    args = p.parse_args()
    fn = {"create": _create, "ready": _ready, "status": _status, "verdict": _verdict}[args.cmd]
    asyncio.run(fn(args))


if __name__ == "__main__":
    main()
