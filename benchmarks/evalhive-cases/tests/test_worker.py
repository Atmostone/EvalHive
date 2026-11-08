"""Native case lifecycle, restore and public SDK process integration."""

import asyncio
import copy
import json
import sys

import pytest
from evalhive_benchmark import ConnectorClient, ProtocolError, validate, validate_environment
from evalhive_cases.dataset import InstalledCases, build_receipt
from evalhive_cases.format import CaseFormatError
from evalhive_cases.worker import CasesConnector, unavailable
from test_format import DOCUMENT

IMAGE = "example/cases@sha256:" + "1" * 64
OUTCOME = {"attempt_id": "attempt-one", "evidence_digest": "a" * 64}
SCOPE = {"workspace_id": "workspace-one", "trial_id": "trial-one", "generation": 1}
ARTIFACT = {
    "id": "workspace-one",
    "visibility": "user",
    "uri": "evidence://workspace.tar",
    "size_bytes": 12,
    "digest": "sha256:" + "b" * 64,
}


def installation(tmp_path, *, document=None):
    document = copy.deepcopy(DOCUMENT if document is None else document)
    document["environment"] = document.get("environment", {})
    if document["environment"] is not None:
        document["environment"]["mcp_servers"] = []
    directory = tmp_path / "example"
    directory.mkdir(exist_ok=True)
    (directory / "case.json").write_text(json.dumps(document))
    receipt = build_receipt(tmp_path, "example", "rev-one", IMAGE)
    (tmp_path / "installation.json").write_text(json.dumps(receipt))
    return InstalledCases(tmp_path, receipt)


def spec(dataset, **config):
    return {
        "protocol_version": "1.0",
        "scope": SCOPE,
        "case": dataset.receipt["cases"][0],
        "config": config,
        "credential_refs": {},
        "models": {},
        "limits": {"wall_time_seconds": 60, "memory_mb": 512, "cpu_cores": 1, "disk_mb": 64},
    }


def event(content, **fields):
    return {"event": "message.end", "data": {"role": "assistant", "content": content, **fields}}


class Context:
    def __init__(self, events=None):
        self.events = events or [event("private-reference")]
        self.calls = []

    async def broker(self, scope, provider, operation, payload, **options):
        self.calls.append((operation, payload))
        assert scope == SCOPE and provider == "evalhive.rootless"
        assert options["durable_host"]
        return {
            "allocate": {"handle": "owned-task"},
            "inspect": {"state": "allocated", "native": {"status": "running"}},
            "export": {"artifact": ARTIFACT},
        }[operation]

    async def agent(self, scope, operation, payload, **options):
        self.calls.append((operation, payload))
        if operation == "run":
            return {"session_id": "agent-one", "status": "success", "outcome": OUTCOME}
        assert options["session_id"] == "agent-one"
        cursor = payload["cursor"]
        return {
            "session_id": "agent-one",
            "events": self.events[cursor:],
            "cursor": len(self.events),
            "closed": True,
        }


async def lifecycle(worker, run_spec, context):
    checked = await worker.handle("validate", {"spec": run_spec}, context)
    run_spec = {**run_spec, "config": checked["effective_config"]}
    plan = await worker.handle("plan", {"spec": run_spec}, context)
    validate_environment(run_spec, plan)
    assert "private-reference" not in json.dumps(plan)
    prepared = await worker.handle("prepare", {"spec": run_spec, "plan": plan}, context)
    validate_environment(run_spec, prepared, kind="prepared")
    params = {"scope": SCOPE, "payload": {"session_handle": prepared["session_handle"]}}
    if run_spec.get("purpose") != "evaluation":
        assert await worker.handle("drive", params, context) == {"agent_outcome": OUTCOME}
    capture = await worker.handle("capture", params, context)
    evaluated = await worker.handle("evaluate", params, context)
    validate("evaluation_result", evaluated)
    if "checkpoint" in capture:
        validate("regrade_checkpoint", capture["checkpoint"])
    return capture, evaluated


def test_execution_checkpoint_and_fresh_evaluation_do_not_run_the_agent_again(tmp_path):
    async def scenario():
        dataset = installation(tmp_path)
        run_spec = spec(dataset, evaluation_mode="reference_exact")
        context = Context()
        capture, evaluated = await lifecycle(CasesConnector(dataset=dataset), run_spec, context)
        assert evaluated["success"] is True and capture["artifacts"] == [ARTIFACT]
        restored_spec = {**run_spec, "purpose": "evaluation", "checkpoint": capture["checkpoint"]}
        restored_context = Context()
        _, restored = await lifecycle(
            CasesConnector(dataset=dataset), restored_spec, restored_context
        )
        assert restored == evaluated and restored_context.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,answer,expected",
    [
        ("reference_exact", "  PRIVATE-reference\n", True),
        ("reference_exact", "incorrect", False),
        ("reference_fuzzy", "private-referenc", True),
        ("deferred", "private-reference", None),
    ],
)
def test_native_evaluation_is_explicit_and_never_inferred_from_agent_success(
    tmp_path, mode, answer, expected
):
    async def scenario():
        dataset = installation(tmp_path)
        _, evaluated = await lifecycle(
            CasesConnector(dataset=dataset),
            spec(
                dataset,
                evaluation_mode=mode,
                similarity_threshold=0 if mode == "reference_exact" else 0.9,
            ),
            Context([event(answer)]),
        )
        assert evaluated["success"] is expected
        assert (evaluated["status"] == "unavailable") == (expected is None)

    asyncio.run(scenario())


def test_reasoning_and_nested_session_text_do_not_replace_final_answer(tmp_path):
    async def scenario():
        dataset = installation(tmp_path)
        events = [
            event("private-reference"),
            event("thinking-secret", channel="reasoning"),
            {**event("child-output"), "parent_session_id": "root-session"},
        ]
        capture, evaluated = await lifecycle(
            CasesConnector(dataset=dataset),
            spec(dataset, evaluation_mode="reference_exact"),
            Context(events),
        )
        assert (
            evaluated["success"] and capture["checkpoint"]["state"]["answer"] == "private-reference"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("events", [[event("x" * 300000)], [event("\x01" * 50000 + "x")], []])
def test_missing_or_oversized_answers_are_unknown_without_a_false_checkpoint(tmp_path, events):
    async def scenario():
        dataset = installation(tmp_path)
        context = Context()
        context.events = events
        capture, evaluated = await lifecycle(
            CasesConnector(dataset=dataset),
            spec(dataset, evaluation_mode="reference_exact"),
            context,
        )
        assert evaluated["status"] == "unavailable" and evaluated["success"] is None
        assert capture["regrade"] == "unavailable" and "checkpoint" not in capture
        assert capture["artifacts"] == [ARTIFACT]

    asyncio.run(scenario())


def test_dataset_identity_source_and_receipt_are_frozen(tmp_path):
    dataset = installation(tmp_path)
    descriptor = copy.deepcopy(dataset.receipt["cases"][0])
    forged = copy.deepcopy(descriptor)
    forged["title"] = "forged title"
    with pytest.raises(CaseFormatError):
        dataset.resolve(forged)
    path = tmp_path / "example" / "case.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(CaseFormatError):
        dataset.resolve(descriptor)


def test_case_pages_are_byte_bounded_and_do_not_skip_descriptors(tmp_path):
    async def scenario():
        document = copy.deepcopy(DOCUMENT)
        document["input"]["description"] = "x" * 250000
        dataset = installation(tmp_path, document=document)
        original = dataset.receipt["cases"][0]
        dataset.receipt["cases"] = [
            {**copy.deepcopy(original), "ref": {**original["ref"], "case_id": str(index)}}
            for index in range(5)
        ]
        worker = CasesConnector(dataset=dataset)
        first = await worker.handle(
            "list_cases", {"dataset_id": "example", "revision": "rev-one", "limit": 100}, None
        )
        assert len(first["cases"]) == 4 and first["next_cursor"] == "4"
        second = await worker.handle(
            "list_cases",
            {"dataset_id": "example", "revision": "rev-one", "cursor": first["next_cursor"]},
            None,
        )
        assert [case["ref"]["case_id"] for case in first["cases"] + second["cases"]] == [
            str(index) for index in range(5)
        ]
        assert second["next_cursor"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("environment", {"required_services": ["postgres"]}, "NATIVE_SERVICES_REQUIRED"),
        ("input", {"title": "Fixture", "context": ["document-id"]}, "CONTEXT_BINDING_REQUIRED"),
        (
            "gold",
            {
                "external_eval": {
                    "preprocess_command": "python prepare.py",
                    "eval_command": "python checker.py",
                }
            },
            "NATIVE_CONNECTOR_REQUIRED",
        ),
    ],
)
def test_unsupported_legacy_dependencies_fail_before_an_agent_or_resource_call(
    tmp_path, field, value, code
):
    async def scenario():
        document = copy.deepcopy(DOCUMENT)
        document[field] = value
        dataset = installation(tmp_path, document=document)
        context = Context()
        with pytest.raises(ProtocolError) as caught:
            await CasesConnector(dataset=dataset).handle(
                "validate", {"spec": spec(dataset)}, context
            )
        assert caught.value.code == code and context.calls == []

    asyncio.run(scenario())


def test_null_legacy_environment_and_unknown_population_remain_supported(tmp_path):
    async def scenario():
        dataset = installation(tmp_path, document={**DOCUMENT, "environment": None})
        worker = CasesConnector(dataset=dataset)
        await worker.handle("validate", {"spec": spec(dataset)}, Context())
        result = await worker.handle(
            "aggregate",
            {"payload": {"results": [unavailable("DEFERRED")], "expected": 3}},
            Context(),
        )
        validate("aggregate_result", result)
        assert result["metrics"][0]["value"] is None and result["observed"] == 1
        assert result["native"] == {"scored": 0, "unscored": 1}
        with pytest.raises(ProtocolError):
            await worker.handle(
                "aggregate",
                {"payload": {"results": [unavailable("DEFERRED")], "expected": 0}},
                Context(),
            )

    asyncio.run(scenario())


def test_real_stdio_worker_runs_via_sdk_without_importing_evalhive_backend(tmp_path):
    async def scenario():
        dataset = installation(tmp_path)
        context = Context()

        async def broker(request):
            return await context.broker(
                request["scope"],
                request["provider"],
                request["operation"],
                request["payload"],
                durable_host=True,
            )

        async def agent(request):
            params = request["params"]
            return await context.agent(
                params["scope"],
                params["operation"],
                params["payload"],
                session_id=params.get("session_id"),
            )

        script = "import asyncio,sys; from evalhive_benchmark import serve_stdio; from evalhive_cases.worker import CasesConnector; assert 'app' not in sys.modules; asyncio.run(serve_stdio(CasesConnector(sys.argv[1])))"
        client = await ConnectorClient.launch(
            [sys.executable, "-c", script, str(tmp_path)], broker=broker, agent=agent
        )
        try:
            assert (await client.request("describe"))["id"] == "evalhive-cases"
            page = await client.request(
                "list_cases", {"dataset_id": "example", "revision": "rev-one", "limit": 1}
            )
            assert page["cases"] == dataset.receipt["cases"] and page["next_cursor"] is None
            run_spec = spec(dataset, evaluation_mode="reference_exact")
            checked = await client.request("validate", {"spec": run_spec})
            run_spec["config"] = checked["effective_config"]
            plan = await client.request("plan", {"spec": run_spec})
            prepared = await client.request("prepare", {"spec": run_spec, "plan": plan})
            params = {"scope": SCOPE, "payload": {"session_handle": prepared["session_handle"]}}
            await client.request("drive", params)
            assert (await client.request("capture", params))["regrade"] == "available"
            assert (await client.request("evaluate", params))["success"] is True
        finally:
            await client.close()

    asyncio.run(scenario())
