"""World administration cannot be forwarded from the task-facing API surface."""

import asyncio
import json

import pytest
from evalhive_appworld.api_gate import Gate
from evalhive_appworld.api_service import PrivateControls


def request(path, method="POST", client="127.0.0.1", *, gate=None):
    responses = []
    gate = gate or Gate()

    async def app(scope, receive, send):
        assert gate.native == 1
        await send({"type": "http.response.start", "status": 204})

    async def send(value):
        responses.append(value)

    asyncio.run(
        PrivateControls(app, gate)(
            {"type": "http", "path": path, "method": method, "client": (client, 1234)}, None, send
        )
    )
    return responses, gate


def test_private_open_and_seal_do_not_count_themselves_as_pending_native_work():
    opened, gate = request("/.__evalhive/open")
    assert opened[0]["status"] == 200 and gate.accepting
    sealed, gate = request("/.__evalhive/seal", gate=gate)
    assert sealed[0]["status"] == 200 and not gate.accepting
    assert json.loads(sealed[1]["body"]) == {"sealed": True}


@pytest.mark.parametrize(
    "client,method,path",
    [
        ("172.18.0.2", "POST", "/.__evalhive/open"),
        ("127.0.0.1", "GET", "/.__evalhive/open"),
        ("127.0.0.1", "POST", "/.__evalhive/unregistered"),
    ],
)
def test_controls_require_private_loopback_and_an_explicit_method(client, method, path):
    responses, gate = request(path, method, client)
    assert responses[0]["status"] == 403 and not gate.accepting


def test_native_requests_are_measured_by_the_underlying_asgi_application():
    responses, gate = request("/spotify/songs/42", "GET")
    assert responses[0]["status"] == 204 and gate.native == 0
