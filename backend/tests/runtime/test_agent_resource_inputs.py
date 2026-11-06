"""Native runtime values use the existing host-only Harness input grant."""

import json
from copy import deepcopy

import pytest
from app.benchmarks.agent_resources import compile_resources, private_inputs
from app.benchmarks.harness_execution import HarnessExecution
from app.harnesses.catalog import get_manifest
from evalhive_benchmark import ProtocolError


def resource():
    return {
        "id": "native-tools",
        "kind": "mcp",
        "config": {
            "transport": "stdio",
            "command": "native-tool",
            "args": [],
            "env": {"ALLOWED_DIR": "/workspace", "UV_OFFLINE": "1", "EMPTY": ""},
        },
    }


def test_native_settings_keep_empty_values_without_public_inline_environment_or_mutation():
    original = [resource()]
    before = deepcopy(original)
    compiled, values = compile_resources(original)
    assert original == before and "env" not in compiled[0]["config"]
    refs = compiled[0]["credential_refs"]
    assert {key: values[ref] for key, ref in refs.items()} == {
        "env.ALLOWED_DIR": "/workspace",
        "env.UV_OFFLINE": "1",
        "env.EMPTY": "",
    }
    inputs = {"agent-input": b'{"model_api_key":"fixture-model-secret"}'}
    lifted = private_inputs(inputs, "agent-input", values)
    assert inputs["agent-input"] == b'{"model_api_key":"fixture-model-secret"}'
    assert json.loads(lifted["agent-input"])["model_api_key"] == "fixture-model-secret"
    assert set(json.loads(lifted["agent-input"])) == {"model_api_key", *values}


def test_conflicting_roles_ambiguous_resources_and_missing_host_slots_are_rejected():
    selected = resource()
    selected["credential_refs"] = {"env.ALLOWED_DIR": "already-private"}
    with pytest.raises(ProtocolError):
        compile_resources([selected])
    with pytest.raises(ProtocolError):
        compile_resources([resource(), resource()])
    _, values = compile_resources([resource()])
    with pytest.raises(ProtocolError):
        private_inputs({}, "missing", values)
    with pytest.raises(ProtocolError):
        private_inputs({"agent-input": json.dumps(values).encode()}, "agent-input", values)


def test_prepared_agent_access_uses_the_frozen_host_input_grant_and_preserves_real_redaction(
    tmp_path,
):
    execution = HarnessExecution(
        None,
        get_manifest("pi"),
        {
            "protocol_version": "1.0",
            "workspace_id": "workspace",
            "task_id": "task",
            "attempt_id": "attempt",
            "task": {"prompt": "Native task"},
            "cwd": "/workspace",
            "config": {},
        },
        None,
        tmp_path / "unused.sqlite",
        credential_ref="agent-input",
        private_values=["fixture-model-secret"],
    )
    prepared = {
        "ready": True,
        "session_handle": "world",
        "native_state": {},
        "artifacts": [],
        "agent": {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": "task",
            "cwd": "/workspace",
            "prompt": "Native task",
            "instructions": [],
            "resources": [resource()],
        },
    }
    lifted = execution.plan_private_inputs(
        {"agent": prepared["agent"]}, {"agent-input": b'{"model_api_key":"fixture-model-secret"}'}
    )
    execution.configure_access(prepared)
    selected = execution.spec["environment"]["resources"][0]
    assert "env" not in selected["config"]
    assert json.loads(lifted["agent-input"])[selected["credential_refs"]["env.EMPTY"]] == ""
    assert execution.redactor.text("/workspace 1 fixture-model-secret") == "/workspace 1 [REDACTED]"


def test_prepared_runtime_values_cannot_change_after_resource_admission(tmp_path):
    execution = HarnessExecution(
        None,
        get_manifest("pi"),
        {
            "protocol_version": "1.0",
            "workspace_id": "workspace",
            "task_id": "task",
            "attempt_id": "attempt",
            "task": {"prompt": "Native task"},
            "cwd": "/workspace",
            "config": {},
        },
        None,
        tmp_path / "unused.sqlite",
        credential_ref="agent-input",
    )
    original = resource()
    execution.plan_private_inputs({"agent": {"resources": [original]}}, {"agent-input": b"{}"})
    changed = deepcopy(original)
    changed["config"]["env"]["ALLOWED_DIR"] = "/other"
    with pytest.raises(ProtocolError) as conflict:
        execution.configure_access(
            {
                "ready": True,
                "session_handle": "world",
                "native_state": {},
                "artifacts": [],
                "agent": {
                    "mode": "in_environment",
                    "interaction": "autonomous",
                    "environment_handle": "task",
                    "cwd": "/workspace",
                    "prompt": "Native task",
                    "instructions": [],
                    "resources": [changed],
                },
            }
        )
    assert conflict.value.code == "AGENT_RESOURCE_CONFLICT"
