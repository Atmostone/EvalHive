"""CLI for the Variance / Robustness Harness (E-11).

Thin wrapper over ``app.quality.variance.run_variance`` for the "CLI/API"
acceptance. Run inside the api container:

    docker compose exec api python -m app.cli.variance --task-id <uuid> --n 10
    docker compose exec api python -m app.cli.variance --title "..." --n 5 --no-parallel
    docker compose exec api python -m app.cli.variance --task-id <uuid> --cost-cap 0.5 --wait

The orchestrator + scheduler workers drain and aggregate the run; ``--wait``
polls the run row until it reaches a terminal status and prints the aggregate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from decimal import Decimal

from app.database import async_session
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.variance import _TERMINAL_RUN, run_variance


async def _run(args: argparse.Namespace) -> None:
    workspace_id = uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID
    source_task_id = uuid.UUID(args.task_id) if args.task_id else None
    template_id = uuid.UUID(args.template_id) if args.template_id else None
    cost_cap = Decimal(str(args.cost_cap)) if args.cost_cap is not None else None
    spec = None
    if args.title:
        spec = {"title": args.title, "description": args.description or "",
                "reference_answer": args.reference_answer}

    async with async_session() as db:
        run = await run_variance(
            db,
            workspace_id=workspace_id,
            source_task_id=source_task_id,
            source_spec=spec,
            n=args.n,
            parallel=not args.no_parallel,
            cost_cap_usd=cost_cap,
            template_id=template_id,
        )
        run_id = run.id
        print(f"variance run created: {run_id} (status={run.status}, n={run.n})")

    if not args.wait:
        print("watch progress: GET /api/quality/variance/" + str(run_id))
        return

    from app.models.variance_run import VarianceRun

    waited = 0
    while waited < args.wait_timeout:
        await asyncio.sleep(args.poll_interval)
        waited += args.poll_interval
        async with async_session() as db:
            run = await db.get(VarianceRun, run_id)
            if run is None:
                print("run disappeared")
                return
            print(f"  [{waited}s] status={run.status} "
                  f"children={len(run.child_task_ids)}/{run.n} "
                  f"cost=${float(run.accumulated_cost_usd or 0):.4f}")
            if run.status in _TERMINAL_RUN:
                print(json.dumps(run.aggregate or {}, indent=2, ensure_ascii=False))
                return
    print("timed out waiting for run to finish")


def main() -> None:
    p = argparse.ArgumentParser(description="Run a variance / robustness harness")
    src = p.add_argument_group("source (exactly one)")
    src.add_argument("--task-id", help="replay an existing finished task N times")
    src.add_argument("--title", help="run a fresh spec N times (with --description)")
    p.add_argument("--description", default=None)
    p.add_argument("--reference-answer", dest="reference_answer", default=None)
    p.add_argument("--n", type=int, default=10, help="number of runs (2..50)")
    p.add_argument("--no-parallel", action="store_true",
                   help="run sequentially instead of up to max_concurrent_agents")
    p.add_argument("--cost-cap", type=float, default=None, help="max USD for the run")
    p.add_argument("--template-id", default=None, help="pin a specific template")
    p.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    p.add_argument("--wait", action="store_true", help="poll until the run finishes")
    p.add_argument("--poll-interval", type=int, default=10)
    p.add_argument("--wait-timeout", type=int, default=3600)
    args = p.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
