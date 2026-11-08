"""Real SQL model receipts, role fences and unknown charges without paid models."""

# ruff: noqa: F811

import asyncio
from copy import deepcopy

import httpx
import pytest
from app.benchmarks.model_calls import AuxiliaryModels, ModelBinding
from app.benchmarks.usage import trial_usage
from app.models.benchmark_command import BenchmarkCommand
from app.runtime.model_gateway import ModelRoute
from evalhive_benchmark import ProtocolError, command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_trials import advance_to_driving, fixture  # noqa: F401
from tests.runtime.broker_worker_fixture import CASE

KEY = "fixture-owner-model-key-never-public"
TOKEN = "fixture-private-role-token-never-public"


async def service_for(fixture, handler, *, maximum=2, phases=("driving",)):
    store, owner, _, actor, _, _, _ = fixture
    await store.register_stream(owner, actor)
    await advance_to_driving(store, owner)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = AuxiliaryModels(
        store,
        owner,
        actor,
        {
            "user": ModelBinding(
                "aux-user",
                ModelRoute("fixture-model", "http://provider.invalid/v1", KEY, TOKEN),
                phases,
            ),
        },
        client=client,
    )
    service.configure(
        {
            "protocol_version": "1.0",
            "scope": scope(owner),
            "case": deepcopy(CASE),
            "config": {},
            "credential_refs": {"aux-user": "fixture-user-reference"},
            "models": {
                "user": {
                    "name": "fixture-model",
                    "endpoint": "http://provider.invalid/v1",
                    "credential_ref": "aux-user",
                }
            },
            "limits": {
                "wall_time_seconds": 60,
                "memory_mb": 1024,
                "cpu_cores": 1,
                "disk_mb": 1024,
                "model_requests": maximum,
            },
        }
    )
    return service, client


def scope(owner):
    return {
        "workspace_id": str(owner.workspace_id),
        "trial_id": str(owner.trial_id),
        "generation": owner.generation,
    }


def frame(service, identity="model-request", *, role="user"):
    return command(
        identity,
        "model.call",
        {
            "scope": scope(service.owner),
            "role": role,
            "operation": "chat/completions",
            "body": {"model": "fixture-model", "messages": []},
        },
    )


async def test_model_receipt_replay_and_usage_are_atomic_and_do_not_disclose_keys(fixture):
    requests = []

    async def provider(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert str(request.url) == "http://provider.invalid/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": KEY}}],
                "usage": {
                    "prompt_tokens": 13,
                    "completion_tokens": 4,
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            },
        )

    service, client = await service_for(fixture, provider)
    try:
        request = frame(service)
        first = await service.handle(request)
        assert first["body"]["choices"][0]["message"]["content"] == "[REDACTED]"
        assert await service.handle(request) == await service.replay(request) == first
        assert len(requests) == 1
        usage = await trial_usage(service.store, service.owner.workspace_id, service.owner.trial_id)
        (group,) = usage["groups"]
        assert group["role"] == "user" and group["calls"] == 1
        assert group["tokens"]["input_tokens"]["value"] == 13
        assert group["tokens"]["output_tokens"]["value"] == 4
        assert group["cost"]["amount"] is None and group["cost"]["unknown_calls"] == 1
        async with AsyncSession(service.store.engine) as db:
            row = await db.scalar(
                select(BenchmarkCommand)
                .where(BenchmarkCommand.method == "model.call")
                .options(
                    undefer(BenchmarkCommand.private_request),
                    undefer(BenchmarkCommand.private_response),
                )
            )
            assert row.state == "completed"
            assert (
                KEY.encode() not in row.private_response
                and TOKEN.encode() not in row.private_request
            )
    finally:
        await service.close()
        await client.aclose()


async def test_wrong_role_scope_and_phase_cannot_make_a_model_request(fixture):
    requests = []

    async def provider(request):
        requests.append(request)
        return httpx.Response(200, json={})

    service, client = await service_for(fixture, provider, phases=("evaluating",))
    try:
        with pytest.raises(ProtocolError):
            await service.handle(frame(service, role="agent"))
        request = frame(service)
        request["params"]["scope"]["generation"] += 1
        with pytest.raises(ProtocolError) as foreign:
            await service.handle(request)
        assert foreign.value.code == "FOREIGN_MODEL_CALL"
        with pytest.raises(ProtocolError) as phase:
            await service.handle(frame(service))
        assert phase.value.code == "MODEL_ROLE_INACTIVE" and requests == []
    finally:
        await service.close()
        await client.aclose()


async def test_concurrent_calls_cannot_exceed_the_saved_model_request_budget(fixture):
    entered, finish = asyncio.Event(), asyncio.Event()
    requests = []

    async def provider(request):
        requests.append(request)
        entered.set()
        await finish.wait()
        return httpx.Response(200, json={"choices": []})

    service, client = await service_for(fixture, provider, maximum=1)
    first = asyncio.create_task(service.handle(frame(service, "first")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(ProtocolError) as exhausted:
            await service.handle(frame(service, "second"))
        assert exhausted.value.code == "MODEL_REQUEST_LIMIT"
        finish.set()
        await first
        assert len(requests) == 1
    finally:
        finish.set()
        await asyncio.gather(first, return_exceptions=True)
        await service.close()
        await client.aclose()


async def test_interrupted_request_stays_unknown_and_is_never_repeated(fixture):
    entered = asyncio.Event()
    requests = []

    async def provider(request):
        requests.append(request)
        entered.set()
        await asyncio.Event().wait()

    service, client = await service_for(fixture, provider)
    task = asyncio.create_task(service.handle(frame(service)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        with pytest.raises(ProtocolError) as uncertain:
            await service.handle(frame(service))
        assert uncertain.value.code == "COMMAND_UNCERTAIN" and len(requests) == 1
        usage = await trial_usage(service.store, service.owner.workspace_id, service.owner.trial_id)
        (group,) = usage["groups"]
        assert group["calls"] == 1 and group["tokens"]["input_tokens"]["value"] is None
        assert group["cost"]["amount"] is None
        with pytest.raises(ProtocolError) as absent:
            await service.replay(frame(service, "never-started"))
        assert absent.value.code == "RECEIPT_UNAVAILABLE" and len(requests) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()
        await client.aclose()
