"""Native lifecycle retains private traces and regrades without an agent or user."""

import asyncio
import base64
import io
import json
import os
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest
from evalhive_benchmark import ProtocolError, validate, validate_environment
from evalhive_tau2 import stage
from evalhive_tau2.dataset import build_receipt
from evalhive_tau2.installation import declarations, install
from evalhive_tau2.native import load_domain
from evalhive_tau2.resources import public_surface
from evalhive_tau2.worker import Tau2Connector
from test_agent import Harness

IMAGE = "sha256:" + "a" * 64
SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = os.environ.get("EVALHIVE_NATIVE_TAU2_DATA")
    if not root:
        pytest.skip("Requires the pinned native tau2 wheel and private baseline data")
    load_domain(root, "airline")
    from tau2.data_model.tasks import Task

    native = Task(
        id="engineering-calculator",
        user_scenario={"instructions": "private-user-scenario"},
        evaluation_criteria={"communicate_info": ["Done"], "reward_basis": ["DB", "COMMUNICATE"]},
    )
    receipt = build_receipt(root, "native-base", IMAGE, domain="airline")
    case = deepcopy(receipt["cases"][0])
    surface = public_surface(root, "airline")

    class Dataset:
        def __init__(self):
            self.root, self.receipt = Path(root), receipt

        def resolve(self, selected):
            assert selected == case
            return native.model_copy(deep=True)

    dataset = Dataset()
    resources, runtime = declarations(receipt, surface, IMAGE, IMAGE)
    worker = Tau2Connector(dataset=dataset, bindings=runtime)
    spec = {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": case,
        "config": {},
        "credential_refs": {"user-connection": "bound-user-reference"},
        "models": {
            "user": {
                "name": "fixture-user",
                "endpoint": "http://bound-role.invalid/v1",
                "credential_ref": "user-connection",
            }
        },
        "limits": {"wall_time_seconds": 30, "memory_mb": 2048, "cpu_cores": 2, "disk_mb": 2048},
    }
    monkeypatch.setattr(stage, "ROOT", tmp_path / "records")
    monkeypatch.setattr(stage, "RETAINED", tmp_path / "retained")
    return worker, spec, resources, surface, dataset


class Context(Harness):
    def __init__(self, surface):
        super().__init__()
        self.cancelled = asyncio.Event()
        self.mapping = [
            {"native_name": name, "exposed_name": "chosen_" + name, "schema_sha256": digest}
            for name, digest in surface["schema_hashes"].items()
        ]
        self.initial_history = [{"role": "assistant", "content": "Hi! How can I help you today?"}]
        self.broker_calls, self.models, self.artifacts = [], [], {}

    async def agent(self, scope, operation, payload, **options):
        assert scope == SCOPE
        return await super().agent({"trial_id": "trial"}, operation, payload, **options)

    async def model(self, scope, role, body, **options):
        self.models.append(role)
        assert scope == SCOPE and role == "user"
        content = "Calculate" if len(self.models) == 1 else "###STOP###"
        return {
            "status_code": 200,
            "body": {
                "id": "native-user",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-user",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            },
        }

    async def broker(self, scope, provider, operation, payload, **options):
        assert scope == SCOPE and provider == "evalhive.rootless" and options["durable_host"]
        self.broker_calls.append((operation, deepcopy(payload)))
        if operation == "allocate":
            return {"handle": "owned-" + payload["resource_id"]}
        if operation == "inspect":
            return {"native": {"status": "running"}}
        if operation == "exec":
            assert payload["process_policy"] == "stop_after_command"
            method, encoded = payload["argv"][-2:]
            result = stage.execute(method, json.loads(base64.b64decode(encoded)))
            return {"exit_code": 0, "stdout": json.dumps(result), "stderr": ""}
        assert operation == "export"
        name = payload["output_id"]
        self.artifacts[name] = stage.read_file(stage.path(name))
        return {
            "artifact": {
                "id": name,
                "uri": "evidence://" + name,
                "visibility": "private" if name == "checkpoint" else "evaluator",
                "size_bytes": len(self.artifacts[name]),
                "digest": "sha256:" + "b" * 64,
            }
        }


@pytest.mark.asyncio
async def test_full_native_lifecycle_and_cold_grade_have_same_reward_without_reopening_agent(
    setup, tmp_path, monkeypatch
):
    worker, spec, resources, surface, dataset = setup
    context = Context(surface)
    checked = await worker.handle("validate", {"spec": spec}, context)
    assert checked["effective_config"]["max_steps"] == 200
    spec["config"] = checked["effective_config"]
    plan = await worker.handle("plan", {"spec": spec}, context)
    validate_environment(spec, plan)
    assert plan["agent"]["interaction"] == "external_steps"
    assert "private-user-scenario" not in str(plan)
    task_mounts = resources["assets"]["tau2.task"]["mounts"]
    assert "worker-data" not in str(task_mounts)
    assert resources["assets"]["tau2.recorder"]["mounts"] == [
        {"file_id": "harness-runtime", "target": "/evalhive-runtime", "read_only": True}
    ]
    prepared = await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    validate_environment(spec, prepared, kind="prepared")
    driven = await worker.handle("drive", {"scope": SCOPE}, context)
    assert (
        driven["native_termination"] == "user_stop"
        and driven["agent_outcome"]["attempt_id"] == "session"
    )
    captured = await worker.handle("capture", {"scope": SCOPE}, context)
    validate("regrade_checkpoint", captured["checkpoint"])
    assert captured["regrade"] == "available"
    result = await worker.handle("evaluate", {"scope": SCOPE}, context)
    validate("evaluation_result", result)
    assert result["native"]["reward"] == 1.0 and context.models == ["user", "user"]
    assert all(artifact["visibility"] != "user" for artifact in captured["artifacts"])

    retained = tmp_path / "retained" / "checkpoint"
    retained.mkdir(parents=True)
    checkpoint = context.artifacts["checkpoint"]
    with tarfile.open(retained / "payload", "w") as archive:
        member = tarfile.TarInfo("record.json")
        member.size = len(checkpoint)
        archive.addfile(member, io.BytesIO(checkpoint))
    monkeypatch.setattr(stage, "ROOT", tmp_path / "cold-records")
    cold = Tau2Connector(dataset=dataset, bindings=worker.bindings)
    cold_spec = {
        **deepcopy(spec),
        "purpose": "evaluation",
        "checkpoint": captured["checkpoint"],
        "models": {},
        "credential_refs": {},
    }
    cold_context = Context(surface)
    cold_plan = await cold.handle("plan", {"spec": cold_spec}, cold_context)
    validate_environment(cold_spec, cold_plan)
    assert "agent" not in cold_plan and [item["id"] for item in cold_plan["resources"]] == [
        "recorder"
    ]
    await cold.handle("prepare", {"spec": cold_spec, "plan": cold_plan}, cold_context)
    actual = await cold.handle("evaluate", {"scope": SCOPE}, cold_context)
    assert (
        actual["metrics"] == result["metrics"] and not cold_context.ids and not cold_context.models
    )
    assert context.artifacts["checkpoint"] == checkpoint


@pytest.mark.asyncio
async def test_invalid_native_role_or_changed_surface_fails_before_allocating(setup):
    worker, spec, _, surface, _ = setup
    context = Context(surface)
    spec["models"] = {}
    with pytest.raises(ProtocolError) as error:
        await worker.handle("validate", {"spec": spec}, context)
    assert error.value.code == "NATIVE_CONFIGURATION_INVALID" and not context.broker_calls


@pytest.mark.asyncio
async def test_native_limit_before_agent_turn_still_confirms_an_idle_harness_attempt(setup):
    worker, spec, _, surface, _ = setup
    context = Context(surface)
    spec["config"] = {"max_steps": 1}
    plan = await worker.handle("plan", {"spec": spec}, context)
    await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    driven = await worker.handle("drive", {"scope": SCOPE}, context)
    assert (
        driven["native_termination"] == "max_steps"
        and driven["agent_outcome"]["attempt_id"] == "session"
    )
    assert not context.commands and context.models == ["user"]
    result = await worker.handle("evaluate", {"scope": SCOPE}, context)
    assert result["native"]["reward"] == 0.0


def test_installation_snapshots_only_selected_private_domain_and_public_sdk(setup, tmp_path):
    _, _, _, surface, dataset = setup
    output = tmp_path / "installed"
    receipt = install(
        dataset.root,
        output,
        domain="airline",
        revision="native-base",
        worker_image=IMAGE,
        task_image=IMAGE,
    )
    assert len(receipt["cases"]) == 50
    assert not (output / "worker-data/native/tau2/domains/retail").exists()
    assert json.loads((output / "public-surface/tools.json").read_text()) == surface["catalog"]
    public = "".join(path.read_text() for path in (output / "public-surface").iterdir())
    assert "user_scenario" not in public and "evaluation_criteria" not in public
    manifest = json.loads((output / "manifest.json").read_text())
    validate("manifest", manifest)
    assert manifest["image"] == IMAGE
    assert (output / "public-sdk/evalhive_benchmark/tool_catalog.py").is_file()
    files = json.loads((output / "files.json").read_text())["files"]
    assert files["public-surface"]["source_digest"] == "sha256:" + surface["fingerprint"]
    assert (output / "public-sdk/evalhive_benchmark").stat().st_mode & 0o777 == 0o755
    with pytest.raises(ValueError, match="already exists"):
        install(
            dataset.root,
            output,
            domain="airline",
            revision="native-base",
            worker_image=IMAGE,
            task_image=IMAGE,
        )
