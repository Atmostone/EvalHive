"""Native lifecycle preserves reset/time/health and never infers a verdict from agent status."""

import asyncio
import json
import shutil
import sys
from copy import deepcopy

import pytest
from evalhive_benchmark import ConnectorClient, ProtocolError, validate, validate_environment
from evalhive_toolathlon.dataset import InstalledToolathlon, install
from evalhive_toolathlon.worker import ToolathlonConnector
from test_source import source

SCOPE = {"workspace_id": "workspace-one", "trial_id": "trial-one", "generation": 1}
TIME = "2026-11-06 10:15:30 Friday"
OUTCOME = {"attempt_id": "agent-attempt", "evidence_digest": "a" * 64}


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    path = source(tmp_path)

    def export(repository, destination):
        from evalhive_toolathlon.source import file_digest

        shutil.copyfile(path, destination)
        return file_digest(destination)

    monkeypatch.setattr("evalhive_toolathlon.dataset.export_source", export)
    output = tmp_path / "installed"
    install(
        tmp_path,
        output,
        revision="fixture-1",
        worker_image="fixture-only/metadata@sha256:" + "1" * 64,
    )
    return InstalledToolathlon(output)


def spec(dataset, **config):
    return {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": dataset.receipt["cases"][0],
        "config": {"launch_time": TIME, **config},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 600, "memory_mb": 3072, "cpu_cores": 1.5, "disk_mb": 2048},
    }


class Context:
    def __init__(self, *, fallback=False, verdict=0, snapshot=True):
        self.cancelled = asyncio.Event()
        self.calls, self.fallback, self.verdict, self.snapshot = [], fallback, verdict, snapshot
        self.ready_checks, self.agent_calls = 0, 0

    async def agent(self, scope, operation, payload, **options):
        self.agent_calls += 1
        return {"session_id": "session-one", "status": "failed", "outcome": OUTCOME}

    async def broker(self, scope, provider, operation, payload, **options):
        self.calls.append((operation, deepcopy(payload), deepcopy(options)))
        assert scope == SCOPE and provider == "evalhive.rootless" and options["durable_host"]
        if operation == "allocate":
            return {"handle": "owned-" + payload["resource_id"]}
        if operation == "inspect":
            return {
                "native": {
                    "status": "exited"
                    if options["handle"] == "owned-initialize-files"
                    else "running",
                    "exit_code": 0,
                }
            }
        if operation == "export":
            identity = payload["output_id"]
            return {
                "artifact": {
                    "id": identity,
                    "uri": "fixture://" + identity,
                    "digest": "sha256:" + "b" * 64,
                    "size_bytes": 12,
                    "visibility": {
                        "workspace": "user",
                        "checkpoint": "private",
                        "native-report": "evaluator",
                    }[identity],
                }
            }
        argv = payload["argv"]
        result = {"exit_code": 0, "stdout": "", "stderr": ""}
        if argv[1].endswith("environment.py"):
            name = argv[2]
            if name == "evaluate":
                result["exit_code"] = self.verdict
                return result
            if name in {"database_ready", "portals"}:
                self.ready_checks += 1
                data = {"ready": self.ready_checks > 1}
            elif name == "snapshot":
                data = {"checkpoint_complete": self.snapshot}
            else:
                data = {{"initialize": "initialized", "restore": "restored"}.get(name, name): True}
            result["stdout"] = json.dumps(data)
        elif (
            "/preprocess/" in argv[1]
            and self.fallback
            and not options["request_id"].endswith("short")
        ):
            result.update(exit_code=1, stderr="ValueError: unconverted data remains: Friday")
        elif "/evaluation/" in argv[1]:
            result["exit_code"] = self.verdict
        return result


async def prepared(dataset, context, **config):
    worker = ToolathlonConnector(dataset=dataset)
    run_spec = spec(dataset, **config)
    checked = await worker.handle("validate", {"spec": run_spec}, context)
    run_spec["config"] = checked["effective_config"]
    plan = await worker.handle("plan", {"spec": run_spec}, context)
    validate_environment(run_spec, plan)
    result = await worker.handle("prepare", {"spec": run_spec, "plan": plan}, context)
    validate_environment(run_spec, result, kind="prepared")
    return worker, run_spec, plan, result


def test_fallback_reset_readiness_and_private_checkpoint_are_native(dataset):
    async def scenario():
        context = Context(fallback=True)
        worker, run_spec, plan, result = await prepared(dataset, context)
        assert result["native_state"]["active_launch_time"] == "2026-11-06 10:15:30"
        assert result["agent"]["environment_handle"] == "owned-task"
        assert result["agent"]["instructions"] == ["Workspace: /workspace; time: " + TIME]
        assert result["agent"]["resources"][0]["config"]["names"] == ["mcp__toolathlon-terminal__*"]
        assert "private-native-gold" not in json.dumps(result["agent"])
        assert any(resource["kind"] == "volume" for resource in plan["resources"])
        assert (await worker.handle("drive", {"scope": SCOPE}, context)) == {
            "agent_outcome": OUTCOME
        }
        capture = await worker.handle("capture", {"scope": SCOPE}, context)
        validate("regrade_checkpoint", capture["checkpoint"])
        evaluated = await worker.handle("evaluate", {"scope": SCOPE}, context)
        validate("evaluation_result", evaluated)
        assert evaluated["success"] is True  # Independent of the failed agent outcome above.
        phases = [
            payload["argv"]
            for operation, payload, _ in context.calls
            if operation == "exec" and "/preprocess/" in payload["argv"][1]
        ]
        assert [argv[argv.index("--launch_time") + 1] for argv in phases] == [
            TIME,
            "2026-11-06 10:15:30",
        ]
        assert evaluated["native"]["active_launch_time"] == "2026-11-06 10:15:30"
        assert [reference["visibility"] for reference in capture["checkpoint"]["artifacts"]] == [
            "private"
        ]
        assert run_spec["config"]["launch_time"] == TIME

    asyncio.run(scenario())


def test_fresh_evaluation_restores_world_without_reset_preprocess_or_agent(dataset):
    async def scenario():
        original = Context(fallback=True)
        worker, run_spec, _, _ = await prepared(dataset, original)
        capture = await worker.handle("capture", {"scope": SCOPE}, original)
        restored = ToolathlonConnector(dataset=dataset)
        restored_context = Context()
        evaluation_spec = {**run_spec, "purpose": "evaluation", "checkpoint": capture["checkpoint"]}
        await restored.handle("validate", {"spec": evaluation_spec}, restored_context)
        plan = await restored.handle("plan", {"spec": evaluation_spec}, restored_context)
        validate_environment(evaluation_spec, plan)
        assert "agent" not in plan and not any(
            resource["id"] == "task" for resource in plan["resources"]
        )
        result = await restored.handle(
            "prepare", {"spec": evaluation_spec, "plan": plan}, restored_context
        )
        validate_environment(evaluation_spec, result, kind="prepared")
        native = [
            payload["argv"]
            for operation, payload, _ in restored_context.calls
            if operation == "exec"
        ]
        assert any("restore" in argv for argv in native)
        assert not any("reset" in argv or "/preprocess/" in argv[1] for argv in native)
        assert restored_context.agent_calls == 0
        assert (await restored.handle("evaluate", {"scope": SCOPE}, restored_context))["native"][
            "active_launch_time"
        ] == "2026-11-06 10:15:30"
        with pytest.raises(ProtocolError) as forbidden:
            await restored.handle("drive", {"scope": SCOPE}, restored_context)
        assert forbidden.value.code == "EVALUATION_ONLY" and restored_context.agent_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("verdict,success", [(0, True), (1, False)])
def test_native_checker_exit_code_owns_the_verdict(dataset, verdict, success):
    async def scenario():
        context = Context(verdict=verdict)
        worker, _, _, _ = await prepared(dataset, context)
        assert (await worker.handle("evaluate", {"scope": SCOPE}, context))["success"] is success

    asyncio.run(scenario())


def test_checker_infrastructure_exit_does_not_become_an_incorrect_answer(dataset):
    async def scenario():
        context = Context(verdict=2)
        worker, _, _, _ = await prepared(dataset, context)
        with pytest.raises(ProtocolError) as error:
            await worker.handle("evaluate", {"scope": SCOPE}, context)
        assert error.value.kind == "grading"

    asyncio.run(scenario())


def test_incomplete_snapshot_retains_public_files_without_false_regrade_availability(dataset):
    async def scenario():
        context = Context(snapshot=False)
        worker, _, _, _ = await prepared(dataset, context)
        capture = await worker.handle("capture", {"scope": SCOPE}, context)
        assert capture["artifacts"][0]["visibility"] == "user"
        assert capture["regrade"] == "unavailable" and "checkpoint" not in capture
        assert capture["regrade_reason"] == "NATIVE_STATE_INCOMPLETE"

    asyncio.run(scenario())


def test_cancelled_readiness_never_opens_agent_access(dataset):
    async def scenario():
        context = Context()
        context.cancelled.set()
        with pytest.raises(ProtocolError) as error:
            await prepared(dataset, context)
        assert error.value.code == "CANCELLED" and context.agent_calls == 0

    asyncio.run(scenario())


def test_real_sdk_worker_process_prepares_drives_captures_and_grades(dataset):
    async def scenario():
        context = Context(fallback=True)

        async def broker(request):
            return await context.broker(
                request["scope"],
                request["provider"],
                request["operation"],
                request["payload"],
                handle=request.get("handle"),
                request_id=request["id"],
                durable_host=True,
            )

        async def agent(request):
            params = request["params"]
            return await context.agent(params["scope"], params["operation"], params["payload"])

        script = "import asyncio,sys;from evalhive_benchmark import serve_stdio;from evalhive_toolathlon.worker import ToolathlonConnector;assert 'app' not in sys.modules;asyncio.run(serve_stdio(ToolathlonConnector(sys.argv[1])))"
        client = await ConnectorClient.launch(
            [sys.executable, "-c", script, str(dataset.native_root)], broker=broker, agent=agent
        )
        try:
            assert (await client.request("describe"))["id"] == "toolathlon-gym"
            run_spec = spec(dataset)
            run_spec["config"] = (await client.request("validate", {"spec": run_spec}))[
                "effective_config"
            ]
            plan = await client.request("plan", {"spec": run_spec})
            prepared = await client.request("prepare", {"spec": run_spec, "plan": plan})
            assert prepared["ready"]
            params = {"scope": SCOPE, "payload": {"session_handle": prepared["session_handle"]}}
            assert (await client.request("drive", params))["agent_outcome"] == OUTCOME
            assert (await client.request("capture", params))["checkpoint"]["state"][
                "active_launch_time"
            ] == "2026-11-06 10:15:30"
            assert (await client.request("evaluate", params))["success"] is True
            assert context.agent_calls == 1
        finally:
            await client.close()

    asyncio.run(scenario())
