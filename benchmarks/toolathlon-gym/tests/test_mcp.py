"""Native MCP translation does not require backend registries or harness imports."""

import os
from pathlib import Path

import pytest
from evalhive_toolathlon.commands import NativeConfigurationError
from evalhive_toolathlon.mcp import load_resources, resource


def test_native_mcp_preserves_argv_empty_arguments_cwd_and_isolates_fixture_database():
    result = resource(
        {
            "type": "stdio",
            "name": "snowflake",
            "params": {
                "command": "uv",
                "args": [
                    "--directory",
                    "${local_servers_paths}/mcp-snowflake-server",
                    "run",
                    "server",
                    "--private_key_path",
                    "",
                ],
                "cwd": "${agent_workspace}",
                "env": {"PG_HOST": "another-db", "PG_USER": "postgres"},
            },
            "client_session_timeout_seconds": 120,
        }
    )
    assert result["config"]["args"][-1] == ""
    assert result["config"]["cwd"] == "/workspace"
    assert result["config"]["env"]["PG_HOST"] == "toolathlon_pg"
    assert result["config"]["env"]["PGDATABASE"] == "toolathlon_gym"
    assert result["config"]["env"]["UV_OFFLINE"] == "1"
    assert result["config"]["alias_prefix"] == "snowflake_"


def test_native_long_timeout_is_preserved_instead_of_silently_clamped():
    value = resource(
        {"name": "howtocook", "params": {"command": "node"}, "client_session_timeout_seconds": 1000}
    )
    assert value["config"]["timeout_seconds"] == 1000


def test_native_browser_bindings_survive_an_isolated_harness_home():
    value = resource({"name": "playwright_with_chunk", "params": {"command": "node"}})
    assert value["config"]["env"] == {
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/playwright-browsers",
        "PLAYWRIGHT_MCP_SANDBOX": "false",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"command": "sh -c"},
        {"args": ["${unregistered}"]},
        {"env": {"PORT": 1}},
        {"cwd": "${private_dir}"},
        {"executable_override": "host"},
    ],
)
def test_unsupported_native_settings_are_not_silently_ignored(mutation):
    with pytest.raises(NativeConfigurationError):
        resource({"name": "fixture", "params": {"command": "python", **mutation}})


def test_all_pinned_native_server_declarations_translate_without_backend(tmp_path):
    if "TOOLATHLON_SOURCE_PATH" not in os.environ:
        pytest.skip("Set TOOLATHLON_SOURCE_PATH to verify the pinned native corpus")
    source = Path(os.environ["TOOLATHLON_SOURCE_PATH"])
    names = [
        "fetch",
        "terminal",
        "snowflake",
        "notion",
        "memory",
        "rail_12306",
        "playwright_with_chunk",
    ]
    mapped = load_resources(source, names)
    assert len(mapped) == len(names)
    assert [value["id"] for value in mapped] == ["toolathlon-" + name for name in names]
    assert all(value["config"]["transport"] == "stdio" for value in mapped)
    assert len(list((source / "configs/mcp_servers").glob("*.yaml"))) == 25
