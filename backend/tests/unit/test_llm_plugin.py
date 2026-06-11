"""LLMProvider plugin abstraction — verify singleton + override hooks."""

from typing import Any

import pytest

from app.plugins.llm import (
    LLMProvider,
    LiteLLMProvider,
    get_llm_provider,
    set_llm_provider,
)


class _FakeProvider(LLMProvider):
    def __init__(self):
        self.calls: list[dict] = []

    async def acompletion(
        self,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **kwargs: Any,
    ):
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        return {"choices": [{"message": {"content": "ok"}}]}


def test_default_provider_is_litellm_wrapper():
    set_llm_provider(None)
    try:
        prov = get_llm_provider()
        assert isinstance(prov, LiteLLMProvider)
    finally:
        set_llm_provider(None)


@pytest.mark.asyncio
async def test_set_llm_provider_overrides_global():
    set_llm_provider(None)
    fake = _FakeProvider()
    set_llm_provider(fake)
    try:
        result = await get_llm_provider().acompletion(
            "MyModel", [{"role": "user", "content": "hi"}]
        )
        assert fake.calls and fake.calls[0]["model"] == "MyModel"
        assert result["choices"][0]["message"]["content"] == "ok"
    finally:
        set_llm_provider(None)


# ---- SPA-47: transient retry + per-provider concurrency ----------------------

import asyncio

from app.plugins.llm import (
    reset_provider_concurrency,
    set_provider_concurrency,
)


class _TransientBoom(Exception):
    status_code = 429


class _PermanentBoom(Exception):
    status_code = 400


@pytest.fixture()
def fast_retries(monkeypatch):
    monkeypatch.setenv("LLM_TRANSIENT_RETRIES", "2")
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "0")
    reset_provider_concurrency()
    yield
    reset_provider_concurrency()


@pytest.mark.asyncio
async def test_transient_429_retried_then_succeeds(monkeypatch, fast_retries):
    import litellm

    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _TransientBoom("rate limited")
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", fake)
    result = await LiteLLMProvider().acompletion("m", [{"role": "user", "content": "x"}])
    assert result == {"ok": True}
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_non_transient_error_not_retried(monkeypatch, fast_retries):
    import litellm

    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        raise _PermanentBoom("bad request")

    monkeypatch.setattr(litellm, "acompletion", fake)
    with pytest.raises(_PermanentBoom):
        await LiteLLMProvider().acompletion("m", [{"role": "user", "content": "x"}])
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retries_exhausted_reraises(monkeypatch, fast_retries):
    import litellm

    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        raise _TransientBoom("still limited")

    monkeypatch.setattr(litellm, "acompletion", fake)
    with pytest.raises(_TransientBoom):
        await LiteLLMProvider().acompletion("m", [{"role": "user", "content": "x"}])
    assert calls["n"] == 3  # 1 + LLM_TRANSIENT_RETRIES(2)


@pytest.mark.asyncio
async def test_provider_semaphore_caps_concurrency(monkeypatch, fast_retries):
    import litellm

    state = {"current": 0, "max": 0}

    async def fake(**kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", fake)
    set_provider_concurrency("https://api.test", "sk-cp-key12345", 2)
    prov = LiteLLMProvider()
    await asyncio.gather(
        *[
            prov.acompletion(
                "m",
                [{"role": "user", "content": "x"}],
                api_key="sk-cp-key12345",
                api_base="https://api.test",
            )
            for _ in range(6)
        ]
    )
    assert state["max"] <= 2


@pytest.mark.asyncio
async def test_unregistered_provider_is_unbounded(monkeypatch, fast_retries):
    import litellm

    state = {"current": 0, "max": 0}

    async def fake(**kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", fake)
    prov = LiteLLMProvider()
    await asyncio.gather(
        *[
            prov.acompletion(
                "m",
                [{"role": "user", "content": "x"}],
                api_key="sk-other",
                api_base="https://api.other",
            )
            for _ in range(6)
        ]
    )
    assert state["max"] == 6


@pytest.mark.asyncio
async def test_clearing_limit_unbounds_provider(monkeypatch, fast_retries):
    import litellm

    state = {"current": 0, "max": 0}

    async def fake(**kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.01)
        state["current"] -= 1
        return {"ok": True}

    monkeypatch.setattr(litellm, "acompletion", fake)
    set_provider_concurrency("https://api.test", "sk-1", 1)
    set_provider_concurrency("https://api.test", "sk-1", None)
    prov = LiteLLMProvider()
    await asyncio.gather(
        *[
            prov.acompletion(
                "m", [{"role": "user", "content": "x"}],
                api_key="sk-1", api_base="https://api.test",
            )
            for _ in range(4)
        ]
    )
    assert state["max"] == 4
