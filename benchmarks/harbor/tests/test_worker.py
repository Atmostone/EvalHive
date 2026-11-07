"""Native lifecycle plans remain declarative and keep replay separate from agents."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evalhive_benchmark import ProtocolError, validate, validate_environment
from evalhive_harbor.dataset import InstalledHarbor, build_receipt
from evalhive_harbor.worker import HarborConnector
from test_native import task

IMAGE = "sha256:" + "a" * 64
SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}
ARTIFACT = {
    "id": "owned-artifact",
    "visibility": "user",
    "uri": "evidence://owned",
    "size_bytes": 12,
    "digest": "sha256:" + "b" * 64,
}


def installed(tmp_path, *, separate=False, extra="", verifier_timeout=None):
    verifier = '[verifier]\nenvironment_mode="separate"\n' if separate else ""
    if verifier_timeout is not None:
        verifier += "timeout_sec=" + str(verifier_timeout) + "\n"
    task(tmp_path, extra + verifier)
    receipt = build_receipt(tmp_path, "fixture-native", IMAGE)
    dataset = InstalledHarbor(tmp_path, receipt)
    case = receipt["cases"][0]
    binding = {
        "source_digest": case["source_digest"],
        "task": {"asset_id": "harbor.fixture.task", "cwd": "/native"},
        "verifier": {"asset_id": "harbor.fixture.verifier", "cwd": "/native"},
    }
    worker = HarborConnector(
        dataset=dataset,
        bindings={
            "format_version": "1.0",
            "source_digest": receipt["source_digest"],
            "cases": {case["ref"]["case_id"]: binding},
        },
    )
    spec = {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": case,
        "config": {},
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 1024, "cpu_cores": 1, "disk_mb": 1024},
    }
    return worker, spec


class Context:
    def __init__(self):
        self.calls = []
        self.cancelled = asyncio.Event()
        self.agent = AsyncMock(
            return_value={"outcome": {"attempt_id": "attempt", "evidence_digest": "a" * 64}}
        )

    async def broker(self, scope, provider, operation, payload, **options):
        assert scope == SCOPE and provider == "evalhive.rootless" and options["durable_host"]
        self.calls.append((operation, payload, options))
        if operation == "allocate":
            return {"handle": "owned-" + payload["resource_id"]}
        if operation == "inspect":
            return {"native": {"status": "running"}}
        if operation == "export":
            visibility = (
                "evaluator"
                if payload["output_id"] == "native-report"
                else "private"
                if payload["output_id"] == "checkpoint"
                else "user"
            )
            return {"artifact": {**ARTIFACT, "id": payload["output_id"], "visibility": visibility}}
        if operation == "stop":
            return {"stopped": True}
        assert operation == "exec"
        return {"exit_code": 0, "stdout": '{"initialized":true,"restored":true}', "stderr": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("separate", [False, True])
async def test_native_plan_has_only_registered_assets_and_private_checker_is_not_agent_input(
    tmp_path, separate
):
    worker, spec = installed(tmp_path, separate=separate)
    checked = await worker.handle("validate", {"spec": spec}, Context())
    spec["config"] = checked["effective_config"]
    plan = await worker.handle("plan", {"spec": spec}, Context())
    validate_environment(spec, plan)
    assert plan["agent"]["prompt"] == "Public instruction\n"
    assert plan["agent"]["process_policy"] == "retain_until_cleanup"
    assert "wall_time_seconds" not in plan["agent"]
    assert {item["id"] for item in plan["resources"] if item["kind"] == "container"} == {
        "world",
        "task",
        *(["verifier"] if separate else []),
    }
    assert all(
        "asset_id" in item["config"] and "image" not in item["config"] for item in plan["resources"]
    )
    context = Context()
    prepared = await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    validate_environment(spec, prepared, kind="prepared")
    assert prepared["agent"]["environment_handle"] == "owned-task"
    assert context.agent.await_count == 0
    driven = await worker.handle("drive", {"scope": SCOPE}, context)
    assert driven["agent_outcome"]["attempt_id"] == "attempt"
    await worker.close(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "binding_source", "task_asset", "cwd", "config"])
async def test_invalid_installation_or_settings_fail_before_resource_calls(tmp_path, change):
    worker, spec = installed(tmp_path)
    if change == "source":
        (tmp_path / "native-task/tests/test.sh").write_text("changed")
    elif change == "binding_source":
        worker.bindings["source_digest"] = "sha256:" + "c" * 64
    elif change == "task_asset":
        worker.bindings["cases"]["native-task"]["task"]["asset_id"] = "../foreign"
    elif change == "cwd":
        worker.bindings["cases"]["native-task"]["task"]["cwd"] = "/native/../foreign"
    else:
        spec["config"] = {"unknown_setting": True}
    context = Context()
    with pytest.raises(ProtocolError):
        await worker.handle("validate", {"spec": spec}, context)
    assert context.calls == []
    await worker.close(context)


@pytest.mark.asyncio
async def test_native_agent_healthcheck_runs_in_the_bound_task_before_ready(tmp_path):
    worker, spec = installed(
        tmp_path, extra='[environment.healthcheck]\ncommand="printf ready"\ntimeout_sec=2\n'
    )
    context = Context()
    plan = await worker.handle("plan", {"spec": spec}, context)
    await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    health = [
        call
        for call in context.calls
        if call[0] == "exec" and call[1]["argv"] == ["bash", "-c", "printf ready"]
    ]
    assert len(health) == 1 and health[0][2]["handle"] == "owned-task"
    assert health[0][1]["cwd"] == "/native"
    await worker.close(context)


@pytest.mark.asyncio
async def test_shared_capture_does_not_claim_a_replayable_live_environment(tmp_path):
    worker, spec = installed(tmp_path)
    context = Context()
    plan = await worker.handle("plan", {"spec": spec}, context)
    await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    captured = await worker.handle("capture", {"scope": SCOPE}, context)
    assert captured["regrade"] == "unavailable" and "checkpoint" not in captured
    assert captured["artifacts"][0]["id"] == "task-artifacts"
    assert not any(call[0] == "stop" for call in context.calls)
    await worker.close(context)


@pytest.mark.asyncio
async def test_separate_capture_records_native_inputs_before_stopping_task_and_replay_never_runs_agent(
    tmp_path,
):
    worker, spec = installed(tmp_path, separate=True)
    context = Context()
    plan = await worker.handle("plan", {"spec": spec}, context)
    await worker.handle("prepare", {"spec": spec, "plan": plan}, context)
    worker._collect = AsyncMock(
        return_value={"complete": True, "manifest": [], "collect_hooks": []}
    )
    worker._environment(context, "world").transfers.publish = AsyncMock()
    captured = await worker.handle("capture", {"scope": SCOPE}, context)
    validate("regrade_checkpoint", captured["checkpoint"])
    assert captured["regrade"] == "available"
    assert context.calls[-1][0] == "stop" and context.calls[-1][2]["handle"] == "owned-task"
    replay = HarborConnector(dataset=worker.dataset, bindings=deepcopy(worker.bindings))
    replay_spec = {**deepcopy(spec), "purpose": "evaluation", "checkpoint": captured["checkpoint"]}
    replay_plan = await replay.handle("plan", {"spec": replay_spec}, Context())
    validate_environment(replay_spec, replay_plan)
    assert "agent" not in replay_plan
    assert "task" not in {item["id"] for item in replay_plan["resources"]}
    assert next(item for item in replay_plan["resources"] if item["id"] == "world")["config"][
        "retained_inputs"
    ] == ["checkpoint"]
    replay_context = Context()
    replay.handles["world"] = "owned-world"
    replay.operation_deadline = __import__("time").monotonic() + 60
    replay._environment(replay_context, "world").transfers.download = AsyncMock()
    prepared = await replay.handle(
        "prepare", {"spec": replay_spec, "plan": replay_plan}, replay_context
    )
    validate_environment(replay_spec, prepared, kind="prepared")
    with pytest.raises(ProtocolError):
        await replay.handle("drive", {"scope": SCOPE}, replay_context)
    replay_context.agent.assert_not_awaited()
    await replay.close(replay_context)
    await worker.close(context)


@pytest.mark.asyncio
async def test_native_aggregation_uses_harbor_mean_and_preserves_unscored_count(tmp_path):
    worker, _ = installed(tmp_path)
    scored = {
        "status": "completed",
        "success": None,
        "metrics": [],
        "native": {"rewards": {"quality": 0.5}},
        "artifacts": [],
        "completeness": {},
    }
    unscored = {
        "status": "unavailable",
        "success": None,
        "metrics": [],
        "native": {},
        "artifacts": [],
        "completeness": {},
    }
    result = await worker.handle(
        "aggregate", {"payload": {"results": [scored, unscored], "expected": 3}}, Context()
    )
    validate("aggregate_result", result)
    assert result["native"]["mean"] == {"mean": 0.25}
    assert result["native"]["unscored"] == 1
    assert result["expected"] == 3 and result["observed"] == 2
    await worker.close(None)


@pytest.mark.asyncio
async def test_separate_artifact_restoration_does_not_consume_the_native_verifier_budget(
    tmp_path, monkeypatch
):
    worker, spec = installed(tmp_path, separate=True, verifier_timeout=10)
    worker._load(spec)
    worker.ready = True
    clock = [100]
    monkeypatch.setattr("evalhive_harbor.worker.time", SimpleNamespace(monotonic=lambda: clock[0]))
    environment = SimpleNamespace(
        empty_dirs=AsyncMock(),
        recording_complete=True,
        deadline=220,
        transfers=SimpleNamespace(publish=AsyncMock()),
    )
    monkeypatch.setattr(worker, "_environment", lambda context, identity: environment)
    monkeypatch.setattr(worker, "_export", AsyncMock(return_value=ARTIFACT))

    async def restore(native, selected, paths):
        assert worker.operation_deadline == 220
        clock[0] += 30

    async def verify(native, selected, paths):
        assert worker.operation_deadline == environment.deadline == 140
        assert native.task.config.verifier.timeout_sec == 10
        return SimpleNamespace(rewards={"quality": 0.625})

    monkeypatch.setattr("evalhive_harbor.worker.restore_artifacts", restore)
    monkeypatch.setattr("evalhive_harbor.worker.run_verifier", verify)
    result = await worker._evaluate(Context())
    assert result["status"] == "completed" and result["native"]["rewards"] == {"quality": 0.625}
    await worker.close(None)


@pytest.mark.asyncio
async def test_native_hardware_cannot_fall_back_to_an_unrequested_cpu_run(tmp_path):
    worker, spec = installed(tmp_path, extra="[environment]\ngpus=1\n")
    context = Context()
    with pytest.raises(ProtocolError) as error:
        await worker.handle("validate", {"spec": spec}, context)
    assert error.value.code == "NATIVE_HARDWARE_UNAVAILABLE"
    assert error.value.kind == "unsupported"
    assert context.calls == []
    await worker.close(context)
