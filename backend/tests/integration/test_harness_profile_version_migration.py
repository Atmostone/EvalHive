"""Built-in profile upgrades retain settings and immutable historical conditions."""

import asyncio
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.models.execution_attempt import ExecutionAttempt
from app.models.harness_profile import HarnessProfile
from app.models.provider import LLMModel, Provider
from app.models.task import Task
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL


def configuration():
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("+asyncpg", ""))
    return cfg


async def test_profile_pin_upgrade_preserves_settings_references_and_attempts(
    auth_client, db_session
):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    cfg = configuration()
    await asyncio.to_thread(command.downgrade, cfg, "bg15")
    provider = Provider(
        workspace_id=workspace,
        name="Migration fixture",
        api_key="fixture-only",
        endpoint="http://fixture.invalid/v1",
    )
    db_session.add(provider)
    await db_session.flush()
    model = LLMModel(provider_id=provider.id, display_name="Fixture", api_name="fixture")
    db_session.add(model)
    await db_session.flush()
    pi = HarnessProfile(
        workspace_id=workspace,
        name="Old Pi",
        connector_id="pi",
        connector_version="0.1.0",
        config={"thinkingLevel": "high", "temperature": 0},
        secret_refs={"fixture": str(uuid.uuid4())},
        model_id=model.id,
    )
    oc = HarnessProfile(
        workspace_id=workspace,
        name="Old OpenCode",
        connector_id="opencode",
        connector_version="0.2.1",
        config={"apiTimeoutSeconds": 120},
    )
    db_session.add_all([pi, oc])
    await db_session.flush()
    task = Task(
        workspace_id=workspace,
        title="Historical execution",
        status="done",
        harness_profile_id=pi.id,
        run_config={"connector_version": "0.1.0"},
    )
    db_session.add(task)
    await db_session.flush()
    attempt = ExecutionAttempt(
        task_id=task.id,
        workspace_id=workspace,
        number=1,
        profile_id=pi.id,
        connector_id="pi",
        connector_version="0.1.0",
        protocol_version="1.0",
        manifest={"historical": True},
        run_spec={"config": pi.config},
        status="success",
        result={"historical": True},
    )
    db_session.add(attempt)
    await db_session.commit()
    original = (pi.config.copy(), pi.secret_refs.copy(), pi.model_id, pi.updated_at)
    try:
        await asyncio.to_thread(command.upgrade, cfg, "head")
        for row in (pi, oc, task, attempt):
            await db_session.refresh(row)
        assert (pi.connector_version, oc.connector_version) == ("0.2.1", "0.2.2")
        assert (pi.config, pi.secret_refs, pi.model_id, pi.updated_at) == original
        assert task.run_config == {"connector_version": "0.1.0"}
        assert attempt.connector_version == "0.1.0" and attempt.manifest == {"historical": True}
        assert attempt.run_spec == {"config": original[0]} and attempt.result == {
            "historical": True
        }
        audit = (
            await db_session.execute(
                text(
                    "SELECT source_version, target_version, workspace_id FROM harness_profile_version_upgrades WHERE profile_id=:id"
                ),
                {"id": pi.id},
            )
        ).one()
        assert tuple(audit) == ("0.1.0", "0.2.1", workspace)
        await db_session.commit()
        await asyncio.to_thread(command.downgrade, cfg, "bg15")
        await db_session.refresh(pi)
        await db_session.refresh(oc)
        assert (pi.connector_version, oc.connector_version) == ("0.1.0", "0.2.1")
        assert (pi.config, pi.secret_refs, pi.model_id, pi.updated_at) == original
        await db_session.commit()
    finally:
        await db_session.rollback()
        await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.mark.parametrize(
    "version,settings", [("0.1.0", {"unsupported": "private-fixture"}), ("9.0.0", {})]
)
async def test_unsupported_profile_conversion_is_atomic_and_redacted(
    auth_client, db_session, version, settings
):
    cfg = configuration()
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await asyncio.to_thread(command.downgrade, cfg, "bg15")
    profile = HarnessProfile(
        workspace_id=workspace,
        name="Requires conversion",
        connector_id="pi",
        connector_version=version,
        config=settings,
    )
    db_session.add(profile)
    await db_session.commit()
    try:
        with pytest.raises(RuntimeError, match="explicit version/settings conversion") as error:
            await asyncio.to_thread(command.upgrade, cfg, "head")
        assert "private-fixture" not in str(error.value)
        await db_session.refresh(profile)
        assert profile.connector_version == version and profile.config == settings
        assert await db_session.scalar(text("SELECT version_num FROM alembic_version")) == "bg15"
        assert (
            await db_session.scalar(text("SELECT to_regclass('harness_profile_version_upgrades')"))
            is None
        )
    finally:
        await db_session.rollback()
        profile.connector_version, profile.config = "0.1.0", {}
        await db_session.commit()
        await asyncio.to_thread(command.upgrade, cfg, "head")


async def test_edited_profile_requires_backup_for_downgrade(auth_client, db_session):
    cfg = configuration()
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await asyncio.to_thread(command.downgrade, cfg, "bg15")
    profile = HarnessProfile(
        workspace_id=workspace,
        name="Editable upgraded profile",
        connector_id="pi",
        connector_version="0.1.0",
        config={},
    )
    db_session.add(profile)
    await db_session.commit()
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await db_session.refresh(profile)
    original_updated_at = profile.updated_at
    profile_id = profile.id
    profile.config = {"temperature": 0.5}
    await db_session.commit()
    try:
        with pytest.raises(RuntimeError, match="paired database backup"):
            await asyncio.to_thread(command.downgrade, cfg, "bg15")
        await db_session.refresh(profile)
        assert profile.connector_version == "0.2.1" and profile.config == {"temperature": 0.5}
    finally:
        await db_session.rollback()
        await db_session.execute(
            text(
                "UPDATE harness_profiles SET config='{}'::jsonb, updated_at=:updated WHERE id=:id"
            ),
            {"id": profile_id, "updated": original_updated_at},
        )
        await db_session.commit()
        await asyncio.to_thread(command.downgrade, cfg, "bg15")
        await asyncio.to_thread(command.upgrade, cfg, "head")
