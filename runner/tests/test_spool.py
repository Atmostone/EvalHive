import json

import pytest
from evalhive_runner.redaction import Redactor
from evalhive_runner.spool import EventSpool, SpoolFull


def record(seq, name="heartbeat", data=None):
    return {
        "protocol_version": "1.0",
        "type": "event",
        "attempt_id": "a1",
        "event_id": f"event-{seq}",
        "seq": seq,
        "source": "test",
        "event": name,
        "data": data or {},
    }


def test_spool_survives_reopen_and_deletes_only_acknowledged_records(tmp_path):
    path = tmp_path / "private/spool.sqlite"
    with EventSpool(path) as spool:
        spool.append(record(0, "run.started"))
        spool.append(record(1))
        assert spool.next_seq == 2 and spool.pending_count == 2
    with EventSpool(path) as spool:
        assert [event["seq"] for event in spool.batch()] == [0, 1]
        spool.acknowledge(0)
        assert spool.pending_count == 1 and spool.next_seq == 2
    with EventSpool(path) as spool:
        assert spool.batch() == [record(1)]
        spool.acknowledge(1)
        assert spool.next_seq == 2 and spool.pending_bytes == 0
        spool.append(record(2))
        assert spool.batch() == [record(2)]
        with pytest.raises(ValueError):
            spool.acknowledge(3)
        with pytest.raises(ValueError):
            spool.append(record(4))
    assert path.stat().st_mode & 0o777 == 0o600


def test_bounds_reserve_space_for_a_terminal_error(tmp_path):
    with EventSpool(tmp_path / "spool.sqlite", max_pending_bytes=1024 * 1024 + 1024) as spool:
        spool.append(record(0, "run.started"))
        with pytest.raises(SpoolFull):
            spool.append(record(1, "message.end", {"message_id": "m", "content": "x" * 2048}))
        spool.append(
            record(
                1,
                "run.finished",
                {
                    "status": "failed",
                    "summary": "Journal full",
                    "error": {"code": "SPOOL_FULL", "message": "Journal full"},
                },
            )
        )
        assert spool.pending_count == 2


def test_command_receipt_survives_reopen_without_reexecution(tmp_path):
    path = tmp_path / "spool.sqlite"
    with EventSpool(path) as spool:
        spool.command_started("command-1")
        spool.command_finished("command-1", {"ok": True, "result": {"accepted": True}})
    with EventSpool(path) as spool:
        assert spool.command("command-1") == (
            "complete",
            {"ok": True, "result": {"accepted": True}},
        )
        assert len(spool.completed_commands()) == 1
        spool.command_acknowledged("command-1")
        assert spool.command("command-1") is None


def test_redaction_handles_nested_values_json_and_url_encoding():
    secret = "test-key with spaces/and-symbols"
    redactor = Redactor([secret])
    value = {"nested": [secret, "prefix " + secret], "number": 3}
    assert redactor.value(value) == {"nested": ["[REDACTED]", "prefix [REDACTED]"], "number": 3}
    assert secret not in json.dumps(redactor.value(value))
    assert redactor.text("test-key%20with%20spaces%2Fand-symbols") == "[REDACTED]"


def test_structured_credentials_redact_individually_echoed_values():
    redactor = Redactor([json.dumps({"env": {"MCP_PASSWORD": "private-mcp-value"}})])
    assert redactor.text("echo private-mcp-value") == "echo [REDACTED]"


def test_short_credentials_do_not_corrupt_protocol_or_usage_metadata():
    from evalhive_harness import validate

    redactor = Redactor(["1", "success"])
    usage = record(
        0,
        "usage",
        {
            "usage_id": "u1",
            "mode": "cumulative",
            "purpose": "agent",
            "model": "model-1",
            "input_tokens": 1,
            "cost": {"amount": "0.1", "currency": "USD", "source": "provider"},
        },
    )
    cleaned = redactor.event(usage)
    validate("event", cleaned)
    assert cleaned == usage
    terminal = record(1, "run.finished", {"status": "success", "summary": "secret=1"})
    cleaned = redactor.event(terminal)
    validate("event", cleaned)
    assert (
        cleaned["data"]["status"] == "success" and cleaned["data"]["summary"] == "secret=[REDACTED]"
    )
