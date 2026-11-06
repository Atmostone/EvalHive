"""The cost migration preserves history and refuses a lossy downgrade."""

import asyncio
import uuid
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from app.models.task import Task
from sqlalchemy import text

from tests.conftest import TEST_DATABASE_URL


async def test_cost_migration_round_trip_preserves_known_history_and_blocks_unknown(
    auth_client, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(title="Historical cost", workspace_id=workspace_id, cost_usd=Decimal("1.234567"))
    db_session.add(task)
    await db_session.commit()
    task_id = task.id
    config = Config()
    config.set_main_option("script_location", "alembic")
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("+asyncpg", ""))
    await asyncio.to_thread(command.downgrade, config, "ha02")
    await asyncio.to_thread(command.upgrade, config, "head")
    await db_session.refresh(task)
    assert task.cost_usd == Decimal("1.234567")
    task.cost_usd = None
    await db_session.commit()
    with pytest.raises(RuntimeError, match="without data loss"):
        await asyncio.to_thread(command.downgrade, config, "ha02")
    value = await db_session.scalar(
        text("SELECT cost_usd FROM tasks WHERE id = :id"), {"id": task_id}
    )
    assert value is None
    await db_session.rollback()
