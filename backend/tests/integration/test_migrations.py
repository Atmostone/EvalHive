"""Round-trip migrations: every revision must `upgrade` then `downgrade -1` cleanly."""

import asyncio
import os

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def _alembic_cfg() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    url = os.environ["TEST_DATABASE_URL"].replace("+asyncpg", "")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migrations_round_trip(db_session):
    # Request the database fixture so this historical-schema test does not
    # inherit application rows from whichever API test ran immediately before it.
    cfg = _alembic_cfg()
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    revisions.reverse()  # oldest → newest

    # Start from base, then ratchet through every revision and confirm
    # downgrade -1 + upgrade <rev> works at each step.
    await asyncio.to_thread(command.downgrade, cfg, "base")
    for rev in revisions:
        await asyncio.to_thread(command.upgrade, cfg, rev.revision)
        # If this is the very first revision there's no -1 to step back to.
        if rev.down_revision:
            await asyncio.to_thread(command.downgrade, cfg, "-1")
            await asyncio.to_thread(command.upgrade, cfg, rev.revision)

    # Leave the schema at head so subsequent tests run against the latest schema.
    await asyncio.to_thread(command.upgrade, cfg, "head")
