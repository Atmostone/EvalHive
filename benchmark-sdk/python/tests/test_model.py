"""Both SDKs delegate auxiliary roles with durable replay and no credentials."""

import asyncio
from copy import deepcopy

import pytest
from evalhive_benchmark import ConnectorClient, ProtocolError
from test_agent import FIXTURE, argv


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_auxiliary_model_call_and_retired_receipt_use_host_binding(language):
    async def scenario():
        spec = deepcopy(FIXTURE["spec"])
        spec["case"]["requirements"]["capabilities"].append("host.model_api_v1")
        spec["models"]["user"] = {
            "name": "fixture-model",
            "endpoint": "http://fixture.invalid/v1",
            "credential_ref": "private-user-model",
        }
        spec["credential_refs"]["private-user-model"] = "runtime-bound-user"
        calls, replays = [], []
        answer = {"status_code": 200, "body": {"choices": [], "usage": {"prompt_tokens": 3}}}

        async def model(request):
            calls.append(request)
            assert request["params"] == {
                "scope": spec["scope"],
                "role": "user",
                "operation": "chat/completions",
                "body": {"model": "fixture-model"},
            }
            return answer

        async def replay(request):
            replays.append(request)
            return answer

        async def broker(request):
            return {"handle": "environment"}

        client = await ConnectorClient.launch(
            argv(language), model=model, model_replay=replay, broker=broker
        )
        try:
            await client.request("prepare", {"spec": spec, "plan": FIXTURE["plan"]})
            result = await client.request(
                "drive",
                {"scope": spec["scope"], "payload": {"model_role": "user", "repeat_model": True}},
            )
            assert result == {"model": answer}
            assert len(calls) == len(replays) == 1
            for role in ("agent", "judge"):
                with pytest.raises(ProtocolError) as rejected:
                    await client.request(
                        "drive", {"scope": spec["scope"], "payload": {"model_role": role}}
                    )
                assert rejected.value.code == "MODEL_ROLE_UNAVAILABLE"
            assert len(calls) == 1
        finally:
            await client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["python", "typescript"])
def test_evaluation_worker_can_use_declared_judge_without_agent_api(language):
    async def scenario():
        spec = deepcopy(FIXTURE["evaluation_spec"])
        spec["case"]["evaluation_requirements"] = {"capabilities": ["host.model_api_v1"]}
        spec["models"]["judge"] = {
            "name": "fixture-model",
            "endpoint": "http://fixture.invalid/v1",
            "credential_ref": "private-judge-model",
        }
        spec["checkpoint"]["evaluator_roles"] = ["judge"]
        spec["credential_refs"]["private-judge-model"] = "runtime-bound-judge"
        calls = []

        async def model(request):
            calls.append(request)
            return {"status_code": 200, "body": {"choices": []}}

        async def broker(request):
            return {"handle": "environment"}

        client = await ConnectorClient.launch(argv(language), model=model, broker=broker)
        try:
            plan = await client.request("plan", {"spec": spec})
            await client.request("prepare", {"spec": spec, "plan": plan})
            await client.request(
                "evaluate", {"scope": spec["scope"], "payload": {"model_role": "judge"}}
            )
            assert len(calls) == 1 and calls[0]["params"]["role"] == "judge"
        finally:
            await client.close()

    asyncio.run(scenario())
