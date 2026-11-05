"""Template conversion has an audit trail and never rewrites historical evidence."""

import asyncio
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.models.harness_profile import HarnessProfile
from app.models.task import Task
from app.models.template import Template
from app.models.template_version import TemplateVersion
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL


def config():
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("+asyncpg", ""))
    return cfg


async def test_conversion_round_trip_preserves_explicit_connector_and_history(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    cfg = config()
    await asyncio.to_thread(command.downgrade, cfg, "ha08")
    profile = HarnessProfile(workspace_id=workspace, name="Chosen OpenCode", connector_id="opencode", connector_version="0.1.0")
    db_session.add(profile)
    await db_session.flush()
    old = Template(workspace_id=workspace, description="Migration fixture", name="Historical role", soul_md="Original instructions", tool_ids=[])
    explicit = Template(workspace_id=workspace, description="Migration fixture", name="Explicit role", soul_md="Selected connector", tool_ids=[], harness_profile_id=profile.id)
    task = Task(workspace_id=workspace, title="Historical task", status="done", run_config={"legacy_condition": "unchanged"})
    db_session.add_all([old, explicit, task])
    await db_session.flush()
    snapshot = {"name": old.name, "soul_md": old.soul_md, "harness_profile_id": None}
    version = TemplateVersion(workspace_id=workspace, template_id=old.id, version=1, snapshot=snapshot, tool_ids=[])
    db_session.add(version)
    await db_session.commit()
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await db_session.refresh(old)
    await db_session.refresh(explicit)
    await db_session.refresh(task)
    await db_session.refresh(version)
    chosen = await db_session.get(HarnessProfile, old.harness_profile_id)
    assert chosen.connector_id == "pi" and explicit.harness_profile_id == profile.id
    assert version.snapshot == snapshot and task.run_config == {"legacy_condition": "unchanged"}
    audit = (await db_session.execute(text("SELECT source_profile_id, target_profile_id FROM harness_template_conversions WHERE template_id = :id"), {"id": old.id})).one()
    assert audit.source_profile_id is None and audit.target_profile_id == chosen.id
    await db_session.commit()
    await asyncio.to_thread(command.downgrade, cfg, "ha08")
    await db_session.refresh(old)
    assert old.harness_profile_id is None
    await db_session.commit()
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def test_unsupported_legacy_settings_abort_without_silent_conversion(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    cfg = config()
    await asyncio.to_thread(command.downgrade, cfg, "ha08")
    profile = HarnessProfile(workspace_id=workspace, name="Legacy settings", connector_id="legacy", connector_version="0.1.0", config={"tool_injection": "requires review"})
    db_session.add(profile)
    await db_session.flush()
    template = Template(workspace_id=workspace, description="Migration fixture", name="Unconverted", soul_md="Original", tool_ids=[], harness_profile_id=profile.id)
    db_session.add(template)
    await db_session.commit()
    with pytest.raises(RuntimeError, match="explicit settings conversion"):
        await asyncio.to_thread(command.upgrade, cfg, "head")
    await db_session.refresh(template)
    assert template.harness_profile_id == profile.id
    profile.config = {}
    await db_session.commit()
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await db_session.refresh(template)
    assert (await db_session.get(HarnessProfile, template.harness_profile_id)).connector_id == "pi"
