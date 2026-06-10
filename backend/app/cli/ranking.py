"""CLI for the Aggregation Engine (E-19).

Thin wrapper over ``app.quality.ranking`` for the "rank(pairwise_results, method)
→ ranked list with CI" acceptance. Run inside the api container:

    docker compose exec api python -m app.cli.ranking run
    docker compose exec api python -m app.cli.ranking run --method elo
    docker compose exec api python -m app.cli.ranking run --subject template --suite my_suite
    docker compose exec api python -m app.cli.ranking show
    docker compose exec api python -m app.cli.ranking show --history

``run`` derives head-to-head matches from the stored pointwise scores (until the
pairwise framework / E-21 supplies real ones), aggregates them with Bradley-Terry
or Elo, and persists the next versioned leaderboard. No LLM calls. ``show`` prints
the latest leaderboard (or the version history with ``--history``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.ranking import get_ranking, list_rankings, run_ranking


def _workspace(args: argparse.Namespace) -> uuid.UUID:
    return uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID


async def _run(args: argparse.Namespace) -> None:
    async with async_session() as db:
        report = await run_ranking(
            db,
            workspace_id=_workspace(args),
            subject=args.subject,
            method=args.method,
            suite=args.suite,
            created_by=args.created_by,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))


async def _show(args: argparse.Namespace) -> None:
    async with async_session() as db:
        if args.history:
            out = await list_rankings(
                db, workspace_id=_workspace(args), ranking_key=args.key
            )
        else:
            out = await get_ranking(
                db, workspace_id=_workspace(args), ranking_key=args.key
            )
    print(json.dumps(out, indent=2, ensure_ascii=False))


def main() -> None:
    p = argparse.ArgumentParser(description="Aggregation Engine — Bradley-Terry / Elo (E-19)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="aggregate matches + persist a new leaderboard")
    r.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    r.add_argument(
        "--subject",
        choices=["model", "template"],
        default="model",
        help="rank models (default) or templates",
    )
    r.add_argument(
        "--method", choices=["bt", "elo"], default="bt", help="Bradley-Terry (default) or Elo"
    )
    r.add_argument("--suite", default=None, help="filter by benchmark suite")
    r.add_argument("--created-by", default="cli", help="attribution label")
    r.set_defaults(func=_run)

    s = sub.add_parser("show", help="print the latest leaderboard or version history")
    s.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    s.add_argument("--key", default=None, help="filter by ranking_key (e.g. model:bt)")
    s.add_argument("--history", action="store_true", help="print full version history")
    s.set_defaults(func=_show)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
