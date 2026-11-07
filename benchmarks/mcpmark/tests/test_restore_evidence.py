"""Native restore failures retain bounded private diagnostics before grading."""

import json

from evalhive_mcpmark.world_service import NativeService


def test_restore_diagnostics_preserve_exit_codes_redact_secrets_and_bound_output(tmp_path):
    calls = []
    native = {
        "exit_code": 1,
        "stdout": "x" * 40000,
        "stderr": "native failure: fixture-database-password",
    }

    def effect(operation, payload):
        calls.append((operation, payload))
        return native

    service = NativeService(None, records_root=tmp_path, effect=effect)
    service.secret_values = ("fixture-database-password",)
    payload = {"argv": ["pg_restore", "-d", "employees"], "timeout_seconds": 300}
    assert service.restore("postgres.restore", payload) is native
    assert calls == [("postgres.restore", payload)]
    path = tmp_path / "report/postgres-restore.json"
    (record,) = json.loads(path.read_bytes())
    assert record["exit_code"] == 1
    assert record["stderr"] == "native failure: [redacted]"
    assert record["stdout_truncated"] and len(record["stdout"]) == 32768
    assert record["stderr_truncated"] is False
    assert "fixture-database-password" not in path.read_text()
