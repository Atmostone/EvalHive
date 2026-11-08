"""Original PostgreSQL state, native MCP SQL and checkers use the same SDK path."""

import os

import pytest

from tests.integration.test_native_mcpmark_connector import native_engine as native_engine
from tests.integration.test_native_mcpmark_connector import run_native_mcpmark_case

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_MCPMARK_POSTGRES") != "1",
    reason="Requires frozen original backups, pinned PostgreSQL and native MCP client images",
)


@pytest.mark.parametrize("connector_id", ["pi", "opencode"])
async def test_native_postgres_restore_sql_index_verdict_and_cleanup(native_engine, connector_id):
    await run_native_mcpmark_case(native_engine, connector_id, service="postgres")
