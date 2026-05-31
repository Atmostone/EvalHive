"""CLI for the Judge Calibration Protocol (E-17).

Thin wrapper over ``app.quality.judge_calibration`` for the "calibrate(judge_config,
dataset_id) → report" acceptance. Run inside the api container:

    docker compose exec api python -m app.cli.judge_calibration run
    docker compose exec api python -m app.cli.judge_calibration run --suite my_suite
    docker compose exec api python -m app.cli.judge_calibration show
    docker compose exec api python -m app.cli.judge_calibration show --history

``run`` computes a fresh per-dimension reliability report (Pearson / Spearman /
Cohen's kappa of judge vs human scores) and persists it as the next version.
``show`` prints the latest report (or the version history with ``--history``). No
LLM call — pure statistics over stored scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.judge_calibration import (
    get_judge_calibration,
    list_judge_calibrations,
    run_judge_calibration,
)


def _workspace(args: argparse.Namespace) -> uuid.UUID:
    return uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID


async def _run(args: argparse.Namespace) -> None:
    template_id = uuid.UUID(args.template) if args.template else None
    async with async_session() as db:
        report = await run_judge_calibration(
            db,
            workspace_id=_workspace(args),
            suite=args.suite,
            template_id=template_id,
            created_by=args.created_by,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))


async def _show(args: argparse.Namespace) -> None:
    async with async_session() as db:
        if args.history:
            out = await list_judge_calibrations(
                db, workspace_id=_workspace(args), judge_config_key=args.key
            )
        else:
            out = await get_judge_calibration(
                db, workspace_id=_workspace(args), judge_config_key=args.key
            )
    print(json.dumps(out, indent=2, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Judge Calibration Protocol (E-17)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="compute + persist a new calibration report")
    r.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    r.add_argument("--suite", default=None, help="filter by benchmark suite")
    r.add_argument("--template", default=None, help="filter by template id")
    r.add_argument("--created-by", default="cli", help="attribution label")
    r.set_defaults(func=_run)

    s = sub.add_parser("show", help="print the latest report or version history")
    s.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    s.add_argument("--key", default=None, help="filter by judge_config_key (judge model)")
    s.add_argument("--history", action="store_true", help="print full version history")
    s.set_defaults(func=_show)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
