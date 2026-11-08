"""Native failure diagnostics expose exception classes, never exception messages."""

import asyncio
import time

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_mcpmark.worker import MCPMarkConnector


@pytest.mark.parametrize(
    "exception,expected",
    [("OperationalError", "OperationalError"), ("fixture-private-value!", "UnknownError")],
)
def test_native_failure_keeps_only_a_bounded_exception_class(exception, expected):
    connector = MCPMarkConnector()
    connector.deadline = time.monotonic() + 10

    async def control(context, operation, payload):
        if operation == "begin":
            return {"operation_id": "native.prepare"}
        return {
            "state": "failed",
            "effects": [],
            "error": {"exception": exception, "message": "fixture-private-value!"},
        }

    connector.control = control
    with pytest.raises(ProtocolError) as observed:
        asyncio.run(connector.native(None, "prepare"))
    assert observed.value.code == "NATIVE_SERVICE_FAILED"
    assert str(observed.value) == f"Native service prepare failed ({expected})"
    assert "fixture-private-value" not in str(observed.value)
