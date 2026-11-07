"""The official user simulator generates through the credential-free host API."""

import asyncio
import os
import time
from types import SimpleNamespace

import pytest
from evalhive_tau2.models import BoundModel
from evalhive_tau2.native import load_domain


@pytest.mark.asyncio
async def test_native_user_simulator_uses_only_its_host_bound_role():
    root = os.environ.get("EVALHIVE_NATIVE_TAU2_DATA")
    if not root:
        pytest.skip("Requires the pinned native tau2 wheel and private baseline data")
    load_domain(root, "airline")
    from tau2.data_model.message import AssistantMessage
    from tau2.user.user_simulator import UserSimulator

    calls = []

    async def model(scope, role, body, **options):
        calls.append((scope, role, body, options))
        assert role == "user" and body["model"] == "fixture-user"
        assert body["messages"] and "private-user-scenario" in str(body["messages"])
        assert "api_key" not in body and "client" not in body
        return {
            "status_code": 200,
            "body": {
                "id": "native-fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "fixture-user",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "A native user response"},
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            },
        }

    context = SimpleNamespace(model=model, cancelled=asyncio.Event())
    bound = BoundModel(
        context,
        {
            "scope": {"trial_id": "trial"},
            "models": {
                "user": {
                    "name": "fixture-user",
                    "endpoint": "http://owner-provider.invalid/v1",
                    "credential_ref": "owner-user-reference",
                }
            },
        },
        "user",
        asyncio.get_running_loop(),
        time.monotonic() + 60,
    )
    try:
        user = UserSimulator(
            instructions="private-user-scenario", llm=bound.name, llm_args=bound.arguments
        )
        with bound.native_transport(root, "airline"):
            output, state = await asyncio.to_thread(
                user.generate_next_message,
                AssistantMessage(role="assistant", content="Hello"),
                user.get_init_state(),
            )
        assert output.role == "user" and output.content == "A native user response"
        assert output.usage["prompt_tokens"] == 7
        assert len(calls) == 1
        assert bound.arguments["num_retries"] == 0 and state.messages
    finally:
        bound.close()
