"""Registry service — CRUD, secret masking, connection test, migration dedup (SPA-41)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry_entry import RegistryEntry
from app.models.template import Template

logger = logging.getLogger(__name__)

KINDS = ("builtin", "mcp")
# Non-secret mcp config keys carried in `config`; everything credential-y is `secrets`.
_MCP_CONFIG_KEYS = ("command", "args", "url")


class RegistryConflict(Exception):
    """Raised when deleting an entry still referenced by templates (without force)."""

    def __init__(self, referencing: list[str]):
        self.referencing = referencing
        super().__init__(f"referenced by templates: {', '.join(referencing)}")


# --------------------------------------------------------------------------- #
# Secret masking (mirror Provider.api_key handling: stored plain, masked on read)
# --------------------------------------------------------------------------- #
def _mask_secret(value) -> str:
    s = "" if value is None else str(value)
    return f"***{s[-4:]}" if len(s) > 4 else "***"


def mask_secrets(secrets: Optional[dict]) -> dict:
    return {k: _mask_secret(v) for k, v in (secrets or {}).items()}


def serialize(entry: RegistryEntry, *, reveal: bool = False) -> dict:
    return {
        "id": str(entry.id),
        "workspace_id": str(entry.workspace_id),
        "name": entry.name,
        "kind": entry.kind,
        "config": entry.config or {},
        "secrets": (entry.secrets or {}) if reveal else mask_secrets(entry.secrets),
        "secret_keys": sorted((entry.secrets or {}).keys()),
        "enabled": entry.enabled,
        "description": entry.description,
        "created_by": entry.created_by,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def _validate(kind: str, config: dict) -> None:
    if kind not in KINDS:
        raise ValueError("kind must be 'builtin' or 'mcp'")
    if kind == "mcp" and not (config.get("command") or config.get("url")):
        raise ValueError("an mcp entry needs a 'command' (stdio) or a 'url' (http) in config")


async def create_entry(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    name: str,
    kind: str = "builtin",
    config: Optional[dict] = None,
    secrets: Optional[dict] = None,
    enabled: bool = True,
    description: Optional[str] = None,
    created_by: str = "user",
) -> RegistryEntry:
    config = dict(config or {})
    _validate(kind, config)
    entry = RegistryEntry(
        workspace_id=workspace_id,
        name=name.strip(),
        kind=kind,
        config=config,
        secrets=dict(secrets or {}),
        enabled=bool(enabled),
        description=description,
        created_by=created_by,
    )
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError(f"a registry entry named '{name}' already exists in this workspace")
    await db.refresh(entry)
    return entry


async def update_entry(
    db: AsyncSession, entry_id: uuid.UUID, *, workspace_id: uuid.UUID, **fields
) -> RegistryEntry:
    entry = await db.get(RegistryEntry, entry_id)
    if entry is None or entry.workspace_id != workspace_id:
        raise ValueError("registry entry not found")
    if "name" in fields and fields["name"] is not None:
        entry.name = str(fields["name"]).strip()
    if "config" in fields and fields["config"] is not None:
        entry.config = dict(fields["config"])
    if "secrets" in fields and fields["secrets"] is not None:
        entry.secrets = dict(fields["secrets"])
    if "enabled" in fields and fields["enabled"] is not None:
        entry.enabled = bool(fields["enabled"])
    if "description" in fields:
        entry.description = fields["description"]
    _validate(entry.kind, entry.config or {})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ValueError(f"a registry entry named '{entry.name}' already exists in this workspace")
    await db.refresh(entry)
    return entry


async def _referencing_templates(
    db: AsyncSession, entry_id: uuid.UUID, workspace_id: uuid.UUID
) -> list[Template]:
    rows = (
        await db.execute(
            select(Template).where(
                Template.workspace_id == workspace_id,
                Template.tool_ids.contains([str(entry_id)]),
            )
        )
    ).scalars().all()
    return list(rows)


async def delete_entry(
    db: AsyncSession, entry_id: uuid.UUID, *, workspace_id: uuid.UUID, force: bool = False
) -> None:
    entry = await db.get(RegistryEntry, entry_id)
    if entry is None or entry.workspace_id != workspace_id:
        raise ValueError("registry entry not found")
    refs = await _referencing_templates(db, entry_id, workspace_id)
    if refs and not force:
        raise RegistryConflict([t.name for t in refs])
    sid = str(entry_id)
    for t in refs:
        t.tool_ids = [i for i in (t.tool_ids or []) if i != sid]
    await db.delete(entry)
    await db.commit()


async def get_entry(
    db: AsyncSession, entry_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> Optional[RegistryEntry]:
    entry = await db.get(RegistryEntry, entry_id)
    if entry is None or entry.workspace_id != workspace_id:
        return None
    return entry


async def list_entries(
    db: AsyncSession, *, workspace_id: uuid.UUID, kind: Optional[str] = None
) -> list[RegistryEntry]:
    q = select(RegistryEntry).where(RegistryEntry.workspace_id == workspace_id)
    if kind:
        q = q.where(RegistryEntry.kind == kind)
    q = q.order_by(RegistryEntry.kind, RegistryEntry.name)
    return list((await db.execute(q)).scalars().all())


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #
async def test_entry(entry: RegistryEntry) -> dict:
    """Best-effort connectivity/validity check. Never raises.

    builtin → ok (a declared capability); mcp http (``config.url``) → a short
    reachability probe; mcp stdio (``config.command``) → shape validation with an
    honest note that the real handshake runs inside the agent sandbox."""
    if entry.kind == "builtin":
        return {"ok": True, "detail": "builtin capability (no live connection)"}

    config = entry.config or {}
    url = config.get("url")
    if url:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
            return {"ok": resp.status_code < 500, "detail": f"HTTP {resp.status_code} from {url}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"unreachable: {str(e)[:200]}"}

    if config.get("command"):
        return {
            "ok": True,
            "detail": "stdio config valid; the live MCP handshake runs in the agent sandbox at spawn",
        }
    return {"ok": False, "detail": "mcp entry has neither a command nor a url"}


# --------------------------------------------------------------------------- #
# Workspace seeding (copy registry between workspaces)
# --------------------------------------------------------------------------- #
async def copy_registry_to_workspace(
    db: AsyncSession, src_workspace_id: uuid.UUID, dst_workspace_id: uuid.UUID
) -> dict:
    """Copy every registry entry from ``src`` into ``dst`` and return a
    ``{src_entry_id_str: dst_entry_id_str}`` map for remapping template tool_ids.
    Flushes (no commit) so it composes inside the caller's transaction."""
    src = (
        await db.execute(
            select(RegistryEntry).where(RegistryEntry.workspace_id == src_workspace_id)
        )
    ).scalars().all()
    id_map: dict = {}
    for e in src:
        clone = RegistryEntry(
            workspace_id=dst_workspace_id,
            name=e.name,
            kind=e.kind,
            config=dict(e.config or {}),
            secrets=dict(e.secrets or {}),
            enabled=e.enabled,
            description=e.description,
            created_by=e.created_by,
        )
        db.add(clone)
        await db.flush()
        id_map[str(e.id)] = str(clone.id)
    return id_map


# --------------------------------------------------------------------------- #
# Migration dedup (pure — imported by the Alembic data migration AND a unit test)
# --------------------------------------------------------------------------- #
def _mcp_split(mcp: dict) -> tuple[dict, dict]:
    """Split an inline mcp dict into (config, secrets)."""
    config = {k: mcp[k] for k in _MCP_CONFIG_KEYS if k in mcp}
    secrets = dict(mcp.get("env") or {})
    return config, secrets


def dedupe_for_migration(templates: list[dict]) -> tuple[list[dict], dict]:
    """Pure: collapse one workspace's inline template tools/MCP into a deduped set
    of registry entries + a per-template ordered reference list.

    ``templates`` is ``[{"id": str, "tools": [str], "mcp_servers": [dict]}]``.
    Returns ``(entries, per_template_keys)`` where ``entries`` is an ordered list of
    ``{"key", "name", "kind", "config", "secrets"}`` (``key`` is a stable local id the
    caller maps to a fresh DB uuid) and ``per_template_keys`` is
    ``{template_id: [key, …]}`` (builtins first in original order, then MCP).

    Builtins dedupe by name. MCP dedupe by canonical config; a name reused with a
    *different* config is suffixed ``-2/-3`` so nothing is silently merged. Fully
    deterministic for a fixed template order."""
    ordered = sorted(templates, key=lambda t: str(t.get("id")))

    entries: list[dict] = []
    builtin_key: dict = {}  # name -> key
    mcp_key: dict = {}  # canonical-json -> key
    used_names: set = set()

    def _unique_name(base: str) -> str:
        base = base or "tool"
        if base not in used_names:
            used_names.add(base)
            return base
        i = 2
        while f"{base}-{i}" in used_names:
            i += 1
        name = f"{base}-{i}"
        used_names.add(name)
        return name

    # Pass 1: builtins across all templates (distinct names, stable order).
    for t in ordered:
        for name in (t.get("tools") or []):
            name = str(name)
            if name in builtin_key:
                continue
            key = f"b:{name}"
            builtin_key[name] = key
            used_names.add(name)
            entries.append({"key": key, "name": name, "kind": "builtin", "config": {}, "secrets": {}})

    # Pass 2: mcp servers (dedupe by canonical config; suffix name collisions).
    for t in ordered:
        for mcp in (t.get("mcp_servers") or []):
            if not isinstance(mcp, dict):
                continue
            config, secrets = _mcp_split(mcp)
            canonical = json.dumps(
                {"config": config, "secrets": secrets}, sort_keys=True, default=str
            )
            if canonical in mcp_key:
                continue
            display = _unique_name(str(mcp.get("name") or "mcp"))
            key = f"m:{len(mcp_key)}"
            mcp_key[canonical] = key
            entries.append({"key": key, "name": display, "kind": "mcp", "config": config, "secrets": secrets})

    # Build per-template ordered key lists (builtins first, then mcp).
    per_template: dict = {}
    for t in ordered:
        keys: list[str] = []
        for name in (t.get("tools") or []):
            k = builtin_key.get(str(name))
            if k:
                keys.append(k)
        for mcp in (t.get("mcp_servers") or []):
            if not isinstance(mcp, dict):
                continue
            config, secrets = _mcp_split(mcp)
            canonical = json.dumps(
                {"config": config, "secrets": secrets}, sort_keys=True, default=str
            )
            k = mcp_key.get(canonical)
            if k:
                keys.append(k)
        per_template[str(t.get("id"))] = keys

    return entries, per_template
