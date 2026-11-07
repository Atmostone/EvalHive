"""The connector drives native state through the generic broker and agent APIs."""

import asyncio
import base64
import hashlib
import json
import os
from copy import deepcopy

import pytest
from evalhive_benchmark import ProtocolError, validate, validate_environment
from evalhive_mcpmark.assets import filesystem_asset
from evalhive_mcpmark.dataset import InstalledMCPMark, build_receipt
from evalhive_mcpmark.installation import declarations
from evalhive_mcpmark.worker import MCPMarkConnector
from evalhive_mcpmark.world_rpc import World
from evalhive_mcpmark.world_service import NativeService
from evalhive_mcpmark.world_stage import execute

IMAGE = "sha256:" + "a" * 64
SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


@pytest.fixture
def native_connector(tmp_path):
    initial, code = (
        os.getenv("EVALHIVE_MCPMARK_FILESYSTEM_DATA"),
        os.getenv("EVALHIVE_MCPMARK_NATIVE_ROOT"),
    )
    if not initial or not code:
        pytest.skip("Requires the pinned native wheel and original filesystem CDN input")
    receipt = build_receipt(
        code,
        "verified",
        IMAGE,
        service="filesystem",
        suite="easy",
        assets=[filesystem_asset(initial, "file_context")],
    )
    installed = InstalledMCPMark(code, receipt)
    resources, bindings = declarations(receipt, IMAGE, IMAGE)
    connector = MCPMarkConnector(dataset=installed, bindings=bindings)
    case = next(case for case in receipt["cases"] if case["native_ref"]["task_id"] == "uppercase")
    spec = {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": case,
        "config": {},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 30, "memory_mb": 2048, "cpu_cores": 2, "disk_mb": 2048},
    }
    service = NativeService(
        installed,
        initial_root=initial,
        connection_root=tmp_path / "connection",
        records_root=tmp_path / "records",
    )
    world = World(lambda effect: service)
    try:
        yield connector, spec, resources, world, service
    finally:
        service("cleanup", {})
        assert world.close()


class Context:
    def __init__(self, world, service):
        self.world, self.service = world, service
        self.agents, self.exports, self.calls = 0, [], []
        self.agent_allowed = True

    async def broker(self, scope, provider, operation, payload, **options):
        assert scope == SCOPE and provider == "evalhive.rootless" and options["durable_host"]
        self.calls.append((operation, deepcopy(payload)))
        if operation == "allocate":
            return {"handle": "owned-" + payload["resource_id"]}
        if operation == "exec":
            argv = payload["argv"]
            assert payload["process_policy"] == "stop_after_command"
            if argv[2] == "evalhive_mcpmark.world_control":
                result = self.world.request(json.loads(base64.b64decode(argv[3])))
            else:
                assert argv[2] == "evalhive_mcpmark.world_stage"
                result = execute(
                    argv[3],
                    json.loads(base64.b64decode(argv[4])),
                    root=self.service.records_root / "transcript",
                )
            return {"exit_code": 0, "stdout": json.dumps(result), "stderr": ""}
        assert operation == "export"
        name = payload["output_id"]
        self.exports.append(name)
        return {
            "artifact": {
                "id": name,
                "uri": "evidence://" + name,
                "visibility": "user" if name == "working" else "evaluator",
                "size_bytes": 1,
                "digest": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
            }
        }

    async def agent(self, scope, operation, payload, **options):
        assert self.agent_allowed, "Agent APIs must only be used in the driving phase"
        assert scope == SCOPE
        if operation == "run":
            self.agents += 1
            return {
                "session_id": "attempt",
                "outcome": {"attempt_id": "attempt", "evidence_digest": "a" * 64},
                "status": "success",
            }
        assert operation == "events"
        if payload["cursor"] == 0:
            return {
                "events": [
                    {
                        "event": "message.end",
                        "source": "pi",
                        "session_id": "main",
                        "data": {
                            "role": "assistant",
                            "message_id": "final",
                            "content": "No file changes",
                        },
                    }
                ],
                "cursor": 1,
                "closed": True,
            }
        return {"events": [], "cursor": 1, "closed": True}


def test_full_native_lifecycle_keeps_task_mounts_public_and_negative_verdict_complete(
    native_connector,
):
    connector, spec, resources, world, service = native_connector

    async def run():
        context = Context(world, service)
        checked = await connector.handle("validate", {"spec": spec}, context)
        spec["config"] = checked["effective_config"]
        plan = await connector.handle("plan", {"spec": spec}, context)
        validate_environment(spec, plan)
        task = resources["assets"]["mcpmark.task"]
        assert task["storage_slots"]["connection"]["read_only"] is True
        assert "worker-data" not in str(task["mounts"]) and "initial-state" not in str(
            task["mounts"]
        )
        assert "world.sock" not in str(task) and "/control" not in str(task)
        prepared = await connector.handle("prepare", {"spec": spec, "plan": plan}, context)
        validate_environment(spec, prepared, kind="prepared")
        driven = await connector.handle("drive", {"scope": SCOPE}, context)
        context.agent_allowed = False
        assert driven["agent_outcome"]["attempt_id"] == "attempt"
        captured = await connector.handle("capture", {"scope": SCOPE}, context)
        assert captured["regrade"] == "unavailable" and set(context.exports) == {
            "transcript",
            "working",
        }
        result = await connector.handle("evaluate", {"scope": SCOPE}, context)
        validate("evaluation_result", result)
        assert result["status"] == "completed" and result["success"] is False
        assert result["completeness"]["native_verdict"] is True and context.agents == 1
        assert service.captured["agent_success"] is True
        assert await connector.handle("cleanup", {"scope": SCOPE}, context) == {"cleaned": True}

    asyncio.run(run())


def test_uninstalled_initial_category_is_rejected_before_allocating_any_resources(native_connector):
    connector, spec, _, world, service = native_connector
    spec["case"] = next(
        case
        for case in connector.installed().receipt["cases"]
        if case["native_ref"]["category_id"] != "file_context"
    )
    context = Context(world, service)
    with pytest.raises(ProtocolError, match="initial state"):
        asyncio.run(connector.handle("validate", {"spec": spec}, context))
    assert not context.calls and context.agents == 0
