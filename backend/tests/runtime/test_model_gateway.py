"""Model routes remain scoped while native HTTP/SSE bodies cross the gateway."""

import asyncio
import gzip
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from app.runtime.model_gateway import GatewayRejected, ModelGateway, ModelRoute

TOKEN = "isolated-engineering-role-token"
KEY = "isolated-owner-model-key"
ROUTE = ModelRoute("native-model", "http://provider.invalid/v1", KEY, TOKEN)


@pytest.mark.asyncio
async def test_gateway_preserves_native_requests_and_sse_without_exposing_owner_key():
    observed = []
    payload = b'data: {"choices": [{"delta": {"content": "native answer"}}]}\n\ndata: [DONE]\n\n'

    def provider(request):
        observed.append(request)
        return httpx.Response(
            200,
            content=gzip.compress(payload),
            headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
        )

    guard = AsyncMock(return_value=60)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = ModelGateway({"agent": ROUTE}, guard, client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://socket"
        ) as client:
            response = await client.post(
                "/agent/v1/chat/completions",
                json={
                    "model": "native-model",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Task"}],
                },
                headers={
                    "authorization": "Bearer " + TOKEN,
                    "user-agent": "native-harness",
                    "openai-beta": "native-feature",
                    "x-api-key": TOKEN,
                },
            )
        assert response.status_code == 200 and response.content == payload
        assert "content-encoding" not in response.headers
        assert (
            len(observed) == 1
            and str(observed[0].url) == "http://provider.invalid/v1/chat/completions"
        )
        assert observed[0].headers["authorization"] == "Bearer " + KEY
        assert observed[0].headers["user-agent"] == "native-harness"
        assert observed[0].headers["openai-beta"] == "native-feature"
        assert "x-api-key" not in observed[0].headers
        assert KEY not in response.text and TOKEN not in repr(ROUTE) and KEY not in repr(ROUTE)
        assert all(call.args == ("agent",) for call in guard.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,headers,body,status",
    [
        (
            "/other/v1/chat/completions",
            {"authorization": "Bearer " + TOKEN},
            {"model": "native-model"},
            401,
        ),
        (
            "/agent/v1/chat/completions",
            {"authorization": "Bearer wrong"},
            {"model": "native-model"},
            401,
        ),
        ("/agent/v1/chat/completions", {}, {"model": "native-model"}, 401),
        (
            "/agent/v1/chat/completions",
            {"authorization": "Bearer " + TOKEN},
            {"model": "another-model"},
            400,
        ),
        ("/agent/v1/models", {"authorization": "Bearer " + TOKEN}, {}, 404),
        (
            "/agent/v1/chat/completions?url=http://another.invalid",
            {"authorization": "Bearer " + TOKEN},
            {},
            404,
        ),
    ],
)
async def test_caller_cannot_change_role_model_auth_or_upstream_route(path, headers, body, status):
    observed = []

    def provider(request):
        observed.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = ModelGateway({"agent": ROUTE}, AsyncMock(return_value=60), client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://socket"
        ) as client:
            response = await client.post(path, headers=headers, json=body)
        assert response.status_code == status and observed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_provider_errors_cannot_echo_credentials_or_redirect_the_client(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                json={"error": "Private provider diagnostic " + KEY},
                headers={"retry-after": "3", "location": "http://another.invalid"},
            )
        )
    ) as upstream:
        app = ModelGateway({"agent": ROUTE}, AsyncMock(return_value=60), client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://socket"
        ) as client:
            response = await client.post(
                "/agent/v1/responses",
                json={"model": "native-model"},
                headers={"authorization": "Bearer " + TOKEN},
            )
        assert response.status_code == (502 if status == 302 else status)
        assert KEY not in response.text and "location" not in response.headers
        assert response.headers["retry-after"] == "3"


@pytest.mark.asyncio
async def test_expired_ownership_and_bounded_request_fail_before_any_provider_request():
    observed = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: observed.append(request))
    ) as upstream:
        for guard, maximum, expected in [
            (AsyncMock(return_value=0), 1024, 403),
            (AsyncMock(return_value=60), 8, 413),
            (AsyncMock(side_effect=GatewayRejected("STALE_GENERATION")), 1024, 403),
        ]:
            app = ModelGateway({"agent": ROUTE}, guard, client=upstream, max_request_bytes=maximum)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://socket"
            ) as client:
                response = await client.post(
                    "/agent/v1/chat/completions",
                    json={"model": "native-model"},
                    headers={"authorization": "Bearer " + TOKEN},
                )
            assert response.status_code == expected
        assert observed == []


@pytest.mark.asyncio
async def test_closed_gateway_rejects_reuse_without_forwarding():
    guard = AsyncMock(return_value=60)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as upstream:
        app = ModelGateway({"agent": ROUTE}, guard, client=upstream)
        await app.close()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://socket"
        ) as client:
            response = await client.post(
                "/agent/v1/chat/completions",
                content=json.dumps({"model": "native-model"}),
                headers={"authorization": "Bearer " + TOKEN},
            )
        assert response.status_code == 403
        guard.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["headers", "stream"])
async def test_revoked_owner_stops_a_silent_provider_and_closes_its_stream(stage):
    entered, revoked, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SilentStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"delta":"first"}\n\n'
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def provider(request):
        if stage == "headers":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        return httpx.Response(200, stream=SilentStream())

    async def guard(role):
        if revoked.is_set():
            raise GatewayRejected("STALE_GENERATION")
        return 60

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        app = ModelGateway({"agent": ROUTE}, guard, client=upstream)
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b'{"model":"native-model"}'}

        request = asyncio.create_task(app({
            "type": "http", "asgi": {"spec_version": "2.4"}, "method": "POST",
            "path": "/agent/v1/chat/completions", "query_string": b"",
            "headers": [(b"authorization", ("Bearer " + TOKEN).encode())],
        }, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            revoked.set()
            await asyncio.wait_for(request, 2)
            assert closed.is_set()
            if stage == "headers":
                assert sent[0]["status"] == 403
            else:
                assert sent[0]["status"] == 200
                assert not any(
                    item["type"] == "http.response.body" and not item.get("more_body")
                    for item in sent
                )  # A fenced stream must not masquerade as a complete response.
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


@pytest.mark.asyncio
async def test_gateway_accepts_canonical_named_model_roles():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    ) as upstream:
        app = ModelGateway({"grader.v1:judge-main": ROUTE}, AsyncMock(return_value=60), client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://socket"
        ) as client:
            response = await client.post(
                "/grader.v1:judge-main/v1/responses", json={"model": ROUTE.model},
                headers={"authorization": "Bearer " + TOKEN},
            )
        assert response.status_code == 200
