"""CLI for the Pairwise Comparison Framework (E-21).

Thin wrapper over ``app.quality.comparison`` for the "side-by-side A/B → judge →
ELO leaderboard" pipeline. Run inside the api container:

    # Direct: compare two finished tasks, LLM-judge immediately
    docker compose exec api python -m app.cli.comparison create --task-a <A> --task-b <B>
    docker compose exec api python -m app.cli.comparison create --task-a <A> --task-b <B> --human

    # Generated: candidate B is a rerun of the source with an override (judged on the tick)
    docker compose exec api python -m app.cli.comparison generate --source <T> --model-id <M>
    docker compose exec api python -m app.cli.comparison generate --source <T> --soul-md "You are…"

    docker compose exec api python -m app.cli.comparison show --id <C>
    docker compose exec api python -m app.cli.comparison list
    docker compose exec api python -m app.cli.comparison judge --id <C>
    docker compose exec api python -m app.cli.comparison leaderboard --subject model --method elo

The LLM judge is position-bias mitigated (the same pair judged in both orders;
agree → winner, disagree → tie). ``leaderboard`` feeds judged verdicts as real
matches to the E-19 ranking engine and persists the next versioned leaderboard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.comparison import (
    _serialize,
    create_comparison,
    get_comparison,
    judge_comparison_by_id,
    list_comparisons,
    run_pairwise_leaderboard,
)


def _workspace(args: argparse.Namespace) -> uuid.UUID:
    return uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


async def _create(args: argparse.Namespace) -> None:
    async with async_session() as db:
        comp = await create_comparison(
            db,
            workspace_id=_workspace(args),
            subject=args.subject,
            task_a_id=uuid.UUID(args.task_a),
            task_b_id=uuid.UUID(args.task_b),
            judge_mode="human" if args.human else "llm",
            created_by=args.created_by,
        )
        _dump(_serialize(comp))


async def _generate(args: argparse.Namespace) -> None:
    b_run_config: dict = {}
    if args.model_id:
        b_run_config["model_id"] = args.model_id
    if args.template_id:
        b_run_config["template_id"] = args.template_id
    if args.soul_md:
        b_run_config["soul_md"] = args.soul_md
    if not b_run_config:
        raise SystemExit("generate needs at least one override: --model-id / --template-id / --soul-md")
    async with async_session() as db:
        comp = await create_comparison(
            db,
            workspace_id=_workspace(args),
            subject=args.subject,
            task_a_id=uuid.UUID(args.task_a or args.source),
            source_task_id=uuid.UUID(args.source),
            b_run_config=b_run_config,
            judge_mode="human" if args.human else "llm",
            created_by=args.created_by,
        )
        _dump(_serialize(comp))


async def _show(args: argparse.Namespace) -> None:
    async with async_session() as db:
        out = await get_comparison(
            db, uuid.UUID(args.id), workspace_id=_workspace(args), with_sides=True
        )
    if out is None:
        raise SystemExit("comparison not found")
    _dump(out)


async def _list(args: argparse.Namespace) -> None:
    async with async_session() as db:
        out = await list_comparisons(
            db, workspace_id=_workspace(args), subject=args.subject, status=args.status
        )
    _dump(out)


async def _judge(args: argparse.Namespace) -> None:
    async with async_session() as db:
        comp = await judge_comparison_by_id(
            db, uuid.UUID(args.id), workspace_id=_workspace(args)
        )
        _dump(_serialize(comp))


async def _leaderboard(args: argparse.Namespace) -> None:
    async with async_session() as db:
        out = await run_pairwise_leaderboard(
            db,
            workspace_id=_workspace(args),
            subject=args.subject,
            method=args.method,
            source=args.source,
            created_by=args.created_by,
        )
    _dump(out)


def _add_ws(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    p.add_argument("--created-by", default="cli", help="attribution label")


def main() -> None:
    p = argparse.ArgumentParser(description="Pairwise Comparison Framework (E-21)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="compare two existing tasks (direct)")
    _add_ws(c)
    c.add_argument("--task-a", required=True, help="candidate A task id")
    c.add_argument("--task-b", required=True, help="candidate B task id")
    c.add_argument("--subject", choices=["model", "template", "prompt"], default="model")
    c.add_argument("--human", action="store_true", help="human-judge mode (no auto LLM judge)")
    c.set_defaults(func=_create)

    g = sub.add_parser("generate", help="candidate B = rerun of a source with an override")
    _add_ws(g)
    g.add_argument("--source", required=True, help="source task id to rerun into candidate B")
    g.add_argument("--task-a", default=None, help="candidate A task id (defaults to --source)")
    g.add_argument("--model-id", default=None, help="override the model for B")
    g.add_argument("--template-id", default=None, help="override the template for B")
    g.add_argument("--soul-md", default=None, help="override the soul_md / prompt for B")
    g.add_argument("--subject", choices=["model", "template", "prompt"], default="model")
    g.add_argument("--human", action="store_true", help="human-judge mode (no auto LLM judge)")
    g.set_defaults(func=_generate)

    s = sub.add_parser("show", help="print a comparison with its side-by-side payload")
    _add_ws(s)
    s.add_argument("--id", required=True, help="comparison id")
    s.set_defaults(func=_show)

    ls = sub.add_parser("list", help="list comparisons")
    _add_ws(ls)
    ls.add_argument("--subject", choices=["model", "template", "prompt"], default=None)
    ls.add_argument("--status", default=None, help="filter by status")
    ls.set_defaults(func=_list)

    j = sub.add_parser("judge", help="force / redo the LLM judge for a ready comparison")
    _add_ws(j)
    j.add_argument("--id", required=True, help="comparison id")
    j.set_defaults(func=_judge)

    lb = sub.add_parser("leaderboard", help="rank judged comparisons via the E-19 engine")
    _add_ws(lb)
    lb.add_argument("--subject", choices=["model", "template"], default="model")
    lb.add_argument("--method", choices=["bt", "elo"], default="elo")
    lb.add_argument("--source", choices=["judge", "human"], default="judge")
    lb.set_defaults(func=_leaderboard)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
