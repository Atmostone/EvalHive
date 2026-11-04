"""Exercise the original agent loop through its temporary SDK boundary."""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest
from app.harnesses.catalog import get_manifest
from evalhive_harness import ProtocolError, validate_run
from evalhive_harness.connector import RunContext


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connector(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[3]
    agent_path = root / "agent-image/agent.py"
    if not agent_path.is_file():
        agent_path = Path("/agent-image/agent.py")
    worker_path = root / "harnesses/legacy/worker.py"
    if not worker_path.is_file():
        worker_path = Path("/harnesses/legacy/worker.py")
    agent = load_module("legacy_agent_under_test", agent_path)
    worker = load_module("legacy_worker_under_test", worker_path)
    control = SimpleNamespace(queue=asyncio.Queue(), state={})
    monkeypatch.setitem(sys.modules, "feedback_server", control)
    # All filesystem and model access is confined to this fake environment.
    environment = dict(os.environ)
    monkeypatch.setattr(worker, "os", SimpleNamespace(environ=environment))
    monkeypatch.setattr(
        agent,
        "os",
        SimpleNamespace(
            environ=environment,
            makedirs=lambda *a, **kw: None,
            path=SimpleNamespace(exists=lambda path: False),
        ),
    )
    monkeypatch.setattr(agent, "build_system_prompt", lambda soul: soul)
    for key in ("AGENT_BENCHMARK_MODE", "LLM_TEMPERATURE", "LLM_SEED", "AGENT_TOOL_INJECTION"):
        environment.pop(key, None)
    return worker.LegacyConnector(
        agent, control, get_manifest("legacy"), {"model_api_key": "fake-model-key"}
    )


def run_spec(config=None):
    return {
        "protocol_version": "1.0",
        "workspace_id": "w",
        "task_id": "t",
        "attempt_id": "a",
        "task": {"prompt": "Write a file"},
        "cwd": "/workspace",
        "instructions": ["Role"],
        "config": config if config is not None else {"tools": ["file_write"]},
        "model": {
            "name": "fake-model",
            "endpoint": "http://fake/v1",
            "credential_ref": "model_api_key",
        },
    }


async def test_legacy_reuses_loop_and_preserves_tool_ids_and_unknown_usage(connector, monkeypatch):
    first = litellm.ModelResponse(
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "file_write",
                                "arguments": json.dumps({"path": "result.txt", "content": "Done"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 2},
    )
    second = litellm.ModelResponse(
        choices=[
            {"message": {"role": "assistant", "content": "Finished"}, "finish_reason": "stop"}
        ],
        usage={"prompt_tokens": 20, "completion_tokens": 3},
    )
    complete = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(connector.agent.litellm, "acompletion", complete)
    tool_calls = []
    monkeypatch.setattr(
        connector.agent,
        "execute_builtin_tool",
        lambda name, args: tool_calls.append((name, args)) or "Written",
    )
    records = []
    context = RunContext(run_spec(), lambda event: collect(records, event))
    await connector.validate(context.spec)
    result = await connector.run(context)
    assert result == {"status": "success", "summary": "Finished"}
    assert len(tool_calls) == 1 and complete.await_count == 2
    calls = [item for item in records if item["event"].startswith("tool.")]
    assert [item["event"] for item in calls] == ["tool.start", "tool.end"]
    assert calls[0]["data"]["call_id"] == calls[1]["data"]["call_id"] == "call-1"
    usage = [item["data"] for item in records if item["event"] == "usage"]
    assert [item["input_tokens"] for item in usage] == [10, 20]
    assert len({item["usage_id"] for item in usage}) == 2
    assert all(item["cache_read_tokens"] is None and item["cost"] is None for item in usage)
    assert all(
        item["data"]["adapter_version"] == "0.1.0" for item in records if item["event"] == "native"
    )


async def collect(records, event):
    records.append(event)


async def test_explicit_empty_tools_remains_empty(connector, monkeypatch):
    complete = AsyncMock(
        return_value=litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "No tools"}}],
        )
    )
    monkeypatch.setattr(connector.agent.litellm, "acompletion", complete)
    await connector.run(RunContext(run_spec({"tools": []}), lambda event: collect([], event)))
    assert complete.call_args.kwargs["tools"] is None


async def test_feedback_and_abort_are_explicit(connector):
    context = RunContext(run_spec(), lambda event: collect([], event))
    await connector.control(context, "feedback", {"text": "Continue"})
    assert await connector.control_state.queue.get() == {"type": "feedback", "message": "Continue"}
    await connector.control(context, "abort", {})
    assert context.cancelled.is_set()
    with pytest.raises(ProtocolError, match="unavailable"):
        await connector.control(context, "switch_model", {})


async def test_model_and_native_settings_are_checked_before_execution(connector):
    spec = run_spec()
    spec["model"] = None
    with pytest.raises(ProtocolError, match="requires a model"):
        await connector.validate(spec)
    with pytest.raises(ProtocolError):
        validate_run(connector.manifest, run_spec({"unrecognized_native_option": True}))
