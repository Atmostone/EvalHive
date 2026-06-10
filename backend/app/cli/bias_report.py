"""CLI for the Bias Mitigation Toolkit (E-18).

Thin wrapper over ``app.quality.bias_mitigation`` for the "bias report: before vs
after mitigation on the calibration set" acceptance. Run inside the api container:

    docker compose exec api python -m app.cli.bias_report run
    docker compose exec api python -m app.cli.bias_report run --suite my_suite
    docker compose exec api python -m app.cli.bias_report run --no-verbosity
    docker compose exec api python -m app.cli.bias_report show
    docker compose exec api python -m app.cli.bias_report show --history

``run`` re-judges every calibration-set task with the prompt-level mitigations OFF
then ON and persists the next versioned before/after report. Unlike judge
calibration (E-17) this SPENDS LLM CALLS. ``show`` prints the latest report (or the
version history with ``--history``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality.bias_mitigation import (
    get_bias_report,
    list_bias_reports,
    run_bias_report,
)


def _workspace(args: argparse.Namespace) -> uuid.UUID:
    return uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID


def _toggles(args: argparse.Namespace) -> dict | None:
    """Build the toggle override dict from --[no-]<toggle> flags, or None when the
    caller left them all unset (so saved settings / a full A/B default apply)."""
    overrides = {}
    for key in ("verbosity", "score_clustering", "self_preference", "position"):
        val = getattr(args, key)
        if val is not None:
            overrides[key] = val
    return overrides or None


async def _run(args: argparse.Namespace) -> None:
    template_id = uuid.UUID(args.template) if args.template else None
    async with async_session() as db:
        report = await run_bias_report(
            db,
            workspace_id=_workspace(args),
            suite=args.suite,
            template_id=template_id,
            toggles=_toggles(args),
            created_by=args.created_by,
        )
    print(json.dumps(report, indent=2, ensure_ascii=False))


async def _show(args: argparse.Namespace) -> None:
    async with async_session() as db:
        if args.history:
            out = await list_bias_reports(
                db, workspace_id=_workspace(args), judge_config_key=args.key
            )
        else:
            out = await get_bias_report(
                db, workspace_id=_workspace(args), judge_config_key=args.key
            )
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _add_toggle(parser: argparse.ArgumentParser, name: str, help_text: str) -> None:
    """A tri-state --<name> / --no-<name> flag defaulting to None (unset)."""
    dest = name.replace("-", "_")
    parser.add_argument(f"--{name}", dest=dest, action="store_true", default=None, help=help_text)
    parser.add_argument(f"--no-{name}", dest=dest, action="store_false", help=argparse.SUPPRESS)


def main() -> None:
    p = argparse.ArgumentParser(description="Bias Mitigation Toolkit (E-18)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="A/B re-judge + persist a new bias report (spends LLM calls)")
    r.add_argument("--workspace-id", default=None, help="defaults to the default workspace")
    r.add_argument("--suite", default=None, help="filter by benchmark suite")
    r.add_argument("--template", default=None, help="filter by template id")
    r.add_argument("--created-by", default="cli", help="attribution label")
    _add_toggle(r, "verbosity", "enable the verbosity mitigation in the 'after' pass")
    _add_toggle(r, "score-clustering", "enable the score-clustering mitigation in the 'after' pass")
    _add_toggle(r, "self-preference", "flag judge==agent self-preference")
    _add_toggle(r, "position", "position-bias toggle (no-op until pairwise / E-21)")
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
