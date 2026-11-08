"""Clone schema rollback preserves old rows and refuses to erase new ancestry."""

import asyncio
import uuid

import pytest
from alembic import command
from alembic.config import Config
from app.models.experiment import Experiment
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import TEST_DATABASE_URL


def configuration():
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("+asyncpg", ""))
    return cfg


async def test_clone_migration_round_trip_preserves_historical_matrix(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    historical = Experiment(
        workspace_id=workspace,
        name="Historical matrix",
        status="completed",
        dataset={"source": "benchmark_suite", "suite": "toolathlon"},
        dataset_cases=[{"case_key": "old", "title": "Historical case"}],
        configurations=[{"config_key": "cfg-01", "fingerprint": "old-condition"}],
        matrix_spec={},
        report={"historical": "unchanged"},
    )
    db_session.add(historical)
    await db_session.commit()
    identifier = historical.id
    cfg = configuration()
    try:
        await asyncio.to_thread(command.downgrade, cfg, "bg14")
        row = (
            await db_session.execute(
                text("SELECT dataset, configurations, report FROM experiments WHERE id=:id"),
                {"id": identifier},
            )
        ).one()
        assert row.dataset["suite"] == "toolathlon" and row.report == {"historical": "unchanged"}
        assert row.configurations[0]["fingerprint"] == "old-condition"
        await db_session.commit()
    finally:
        await asyncio.to_thread(command.upgrade, cfg, "head")
    await db_session.refresh(historical)
    assert historical.cloned_from_id is None and historical.report == {"historical": "unchanged"}


async def test_downgrade_requires_backup_when_clone_ancestry_exists(auth_client, db_session):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    source = Experiment(
        workspace_id=workspace,
        name="Source matrix",
        dataset={},
        dataset_cases=[],
        configurations=[],
        matrix_spec={},
    )
    db_session.add(source)
    await db_session.flush()
    copied = Experiment(
        workspace_id=workspace,
        name="Copied matrix",
        dataset={},
        dataset_cases=[],
        configurations=[],
        matrix_spec={},
        cloned_from_id=source.id,
        cloned_from_revision=1,
        clone_mode="frozen",
    )
    db_session.add(copied)
    await db_session.commit()
    original_version = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    await db_session.commit()
    with pytest.raises(DBAPIError, match="paired database backup"):
        await asyncio.to_thread(command.downgrade, configuration(), "bg14")
    await db_session.refresh(copied)
    assert copied.cloned_from_id == source.id
    version = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == original_version
