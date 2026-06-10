"""CLI for the Tool & MCP Registry (SPA-41).

Thin wrapper over ``app.registry.service``. Run inside the api container:

    docker compose exec api python -m app.cli.registry list
    docker compose exec api python -m app.cli.registry add-builtin --name bash
    docker compose exec api python -m app.cli.registry add-mcp --name github \
        --command npx --arg -y --arg @modelcontextprotocol/server-github --env GITHUB_TOKEN=ghp_xxx
    docker compose exec api python -m app.cli.registry show --id <ID>
    docker compose exec api python -m app.cli.registry test --id <ID>

Secrets are masked in ``list``/``show`` output (only the spawn-time resolver reveals
them). ``test`` is a best-effort connectivity/validity check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from app.database import async_session
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.registry import service


def _workspace(args: argparse.Namespace) -> uuid.UUID:
    return uuid.UUID(args.workspace_id) if args.workspace_id else DEFAULT_WORKSPACE_ID


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


async def _list(args: argparse.Namespace) -> None:
    async with async_session() as db:
        entries = await service.list_entries(db, workspace_id=_workspace(args), kind=args.kind)
        _dump([service.serialize(e) for e in entries])


async def _add_builtin(args: argparse.Namespace) -> None:
    config = json.loads(args.config) if args.config else {}
    async with async_session() as db:
        entry = await service.create_entry(
            db,
            workspace_id=_workspace(args),
            name=args.name,
            kind="builtin",
            config=config,
            created_by="cli",
        )
        _dump(service.serialize(entry))


async def _add_mcp(args: argparse.Namespace) -> None:
    config: dict = {}
    if args.command:
        config["command"] = args.command
    if args.arg:
        config["args"] = list(args.arg)
    if args.url:
        config["url"] = args.url
    secrets = dict(kv.split("=", 1) for kv in (args.env or []))
    async with async_session() as db:
        entry = await service.create_entry(
            db,
            workspace_id=_workspace(args),
            name=args.name,
            kind="mcp",
            config=config,
            secrets=secrets,
            created_by="cli",
        )
        _dump(service.serialize(entry))


async def _show(args: argparse.Namespace) -> None:
    async with async_session() as db:
        entry = await service.get_entry(db, uuid.UUID(args.id), workspace_id=_workspace(args))
    if entry is None:
        raise SystemExit("registry entry not found")
    _dump(service.serialize(entry))


async def _test(args: argparse.Namespace) -> None:
    async with async_session() as db:
        entry = await service.get_entry(db, uuid.UUID(args.id), workspace_id=_workspace(args))
        if entry is None:
            raise SystemExit("registry entry not found")
        _dump(await service.test_entry(entry))


def _add_ws(p: argparse.ArgumentParser) -> None:
    p.add_argument("--workspace-id", default=None, help="defaults to the default workspace")


def main() -> None:
    p = argparse.ArgumentParser(description="Tool & MCP Registry (SPA-41)")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="list registry entries (secrets masked)")
    _add_ws(ls)
    ls.add_argument("--kind", choices=["builtin", "mcp"], default=None)
    ls.set_defaults(func=_list)

    ab = sub.add_parser("add-builtin", help="register a builtin tool")
    _add_ws(ab)
    ab.add_argument("--name", required=True)
    ab.add_argument("--config", default=None, help="JSON config object")
    ab.set_defaults(func=_add_builtin)

    am = sub.add_parser("add-mcp", help="register an MCP server")
    _add_ws(am)
    am.add_argument("--name", required=True)
    am.add_argument("--command", default=None, help="stdio command")
    am.add_argument("--arg", action="append", help="command arg (repeatable)")
    am.add_argument("--url", default=None, help="http MCP url")
    am.add_argument("--env", action="append", help="secret KEY=VALUE (repeatable)")
    am.set_defaults(func=_add_mcp)

    sh = sub.add_parser("show", help="print one entry (secrets masked)")
    _add_ws(sh)
    sh.add_argument("--id", required=True)
    sh.set_defaults(func=_show)

    ts = sub.add_parser("test", help="best-effort connection/validity check")
    _add_ws(ts)
    ts.add_argument("--id", required=True)
    ts.set_defaults(func=_test)

    args = p.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
