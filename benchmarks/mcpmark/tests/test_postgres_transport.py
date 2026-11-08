"""Native restore failures cannot silently produce an empty benchmark baseline."""

import importlib
import os

import pytest
from evalhive_mcpmark.assets import POSTGRES_DATABASES, postgres_asset
from evalhive_mcpmark.native import NativeConfigurationError, native_code
from evalhive_mcpmark.postgres_transport import restore_transport


def test_all_native_backups_contribute_to_the_initial_state_digest(tmp_path):
    for name in POSTGRES_DATABASES:
        (tmp_path / (name + ".backup")).write_bytes(b"PGDMP" + name.encode())
    original = postgres_asset(tmp_path)
    (tmp_path / "lego.backup").write_bytes(b"PGDMPchanged")
    assert postgres_asset(tmp_path)["source_digest"] != original["source_digest"]
    (tmp_path / "sports.backup").unlink()
    with pytest.raises(FileNotFoundError):
        postgres_asset(tmp_path)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_only_pg_restore_is_transported_and_native_warning_does_not_hide_failure(exit_code):
    if not os.getenv("EVALHIVE_MCPMARK_NATIVE_ROOT"):
        pytest.skip("Requires the pinned native wheel and PostgreSQL client dependency")
    native_code()
    module = importlib.import_module("src.mcp_services.postgres.postgres_state_manager")
    original = module.subprocess
    calls = []

    def effect(operation, payload):
        calls.append((operation, payload))
        return {"exit_code": exit_code, "stdout": "", "stderr": "native restore result"}

    def run():
        with restore_transport(effect) as receipts:
            for name in POSTGRES_DATABASES:
                result = module.subprocess.run(
                    ["pg_restore", "-d", name, "/opt/native/postgres_state/" + name + ".backup"],
                    env={"PGPASSWORD": "fixture-only"},
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == exit_code
            assert len(receipts) == 5
            assert module.subprocess.PIPE == original.PIPE

    if exit_code:
        with pytest.raises(NativeConfigurationError, match="incomplete"):
            run()
    else:
        run()
    assert module.subprocess is original and len(calls) == 5
    assert "fixture-only" not in str(calls)
