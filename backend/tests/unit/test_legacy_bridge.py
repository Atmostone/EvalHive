"""Transition conversion keeps native credentials out of the public run snapshot."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.config import get_settings
from app.harnesses import legacy_bridge
from app.harnesses.legacy_runtime import AgentSpec
from evalhive_harness import validate_run


def fixture_spec(**extra):
    task = SimpleNamespace(id=uuid.uuid4(), workspace_id=uuid.uuid4(), title="Legacy task")
    values = {
        "task_id": str(task.id),
        "workspace_id": str(task.workspace_id),
        "task_description": "Task",
        "template_name": "Role",
        "template_id": str(uuid.uuid4()),
        "soul_md": "Role instructions",
        "tools": [],
        "mcp_servers": [
            {"name": "private", "command": "node", "env": {"API_KEY": "mcp-private-value"}}
        ],
        "env": {
            "OPENAI_API_KEY": "model-private-value",
            "OPENAI_BASE_URL": "http://fake/v1",
            "LLM_MODEL": "fake-model",
        },
        "resource_limits": {"max_ram": "1g", "max_cpu": 50000},
        "agent_token": "knowledge-private-value",
        **extra,
    }
    return task, AgentSpec(**values)


def test_conversion_separates_credentials_and_keeps_explicit_settings():
    task, spec = fixture_spec(extra_env={"LLM_TEMPERATURE": "0.25", "AGENT_MAX_ITERATIONS": "30"})
    manifest, run, private = legacy_bridge.convert_spec(task, spec, timeout_seconds=60)
    validate_run(manifest, run)
    assert manifest["id"] == "legacy" and run["config"]["tools"] == []
    assert run["limits"] == {
        "wall_time_seconds": 60,
        "memory_mb": 1024,
        "cpu_cores": 0.5,
        "max_turns": 30,
    }
    assert run["config"]["temperature"] == 0.25
    snapshot = json.dumps(run)
    assert all(
        value not in snapshot
        for value in ("model-private-value", "mcp-private-value", "knowledge-private-value")
    )
    assert private["model_api_key"] == "model-private-value"
    assert json.loads(private["legacy_mcp_0"])["env"]["API_KEY"] == "mcp-private-value"


@pytest.mark.parametrize(
    "extra",
    [
        {"extra_env": {"UNSUPPORTED_NATIVE_OPTION": "1"}},
        {"image": "evalhive-agent-unknown:latest"},
        {"resource_limits": {"max_ram": "bad"}},
    ],
)
def test_undeclared_options_are_rejected(extra):
    task, spec = fixture_spec(**extra)
    with pytest.raises(ValueError):
        legacy_bridge.convert_spec(task, spec, timeout_seconds=60)


async def test_bridge_uses_common_runner_and_preserves_environment_network(tmp_path, monkeypatch):
    task, spec = fixture_spec(
        image="evalhive-agent-toolathlon:latest",
        network_mode="container:tlpre-owned",
        memory_mode="off",
        extra_env={"AGENT_BENCHMARK_MODE": "1"},
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "host_data_dir", str(tmp_path))
    shared = tmp_path / "shared" / str(task.workspace_id)
    shared.mkdir(parents=True)
    (shared / "rules.md").write_text("Rules")
    (shared / "memory.md").write_text("Memory must stay off")
    launch = AsyncMock(return_value="attempt")
    monkeypatch.setattr(legacy_bridge, "launch_attempt", launch)
    db = object()
    assert await legacy_bridge.launch_legacy(db, task, spec, timeout_seconds=60) == "attempt"
    assert launch.call_args.args == (db,)
    options = launch.call_args.kwargs
    assert options["manifest"]["id"] == "legacy-toolathlon"
    assert options["network"].container == "tlpre-owned"
    assert [mount.target for mount in options["extra_mounts"]] == ["/data/rules.md"]
    assert options["run_spec"]["environment"]["artifact_policy"]["root"] == "."
