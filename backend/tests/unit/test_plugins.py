"""Unit tests for the plugin abstractions."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.plugins import embeddings as emb_mod
from app.plugins import notifier as notif_mod
from app.plugins import runtime as rt_mod
from app.plugins import secrets as sec_mod

# --- EmbeddingProvider --------------------------------------------------------

@pytest.mark.asyncio
async def test_fastembed_provider_caches_model_per_name():
    fake_model = MagicMock()
    fake_model.embed.return_value = iter([SimpleNamespace(tolist=lambda: [0.1, 0.2])])

    with patch("fastembed.TextEmbedding", return_value=fake_model) as ctor:
        prov = emb_mod.FastembedProvider("BAAI/bge-small-en-v1.5", dim=384)
        # Reset class-level cache so this test is isolated.
        emb_mod.FastembedProvider._model_cache.pop("BAAI/bge-small-en-v1.5", None)
        out = await prov.embed(["hello"])
    assert prov.dim == 384
    assert out == [[0.1, 0.2]]
    ctor.assert_called_once()


@pytest.mark.asyncio
async def test_openai_embedding_provider_calls_remote():
    prov = emb_mod.OpenAIEmbeddingProvider(
        url="http://x/embeddings", api_key="K", model="emb-m", dim=1536,
    )
    req = httpx.Request("POST", "http://x/embeddings")
    resp = httpx.Response(
        200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]}, request=req,
    )
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=resp)):
        out = await prov.embed(["hi"])
    assert out == [[0.1, 0.2, 0.3]]
    assert prov.dim == 1536


@pytest.mark.asyncio
async def test_openai_embedding_provider_validates_url():
    prov = emb_mod.OpenAIEmbeddingProvider(url="", api_key="", model="")
    with pytest.raises(RuntimeError):
        await prov.embed(["x"])


@pytest.mark.asyncio
async def test_settings_dispatch_routes_to_fastembed_by_default(monkeypatch):
    """When DB setting `embedding_provider` is fastembed, dispatch returns FastembedProvider."""
    async def fake_get_setting(db, key, default=None):
        return {
            "embedding_provider": "fastembed",
            "embedding_model_local": "BAAI/bge-small-en-v1.5",
        }.get(key, default)

    monkeypatch.setattr("app.api.settings.get_setting", fake_get_setting)

    class _NullSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    monkeypatch.setattr("app.database.async_session", lambda: _NullSession())

    prov = emb_mod.SettingsDispatchProvider()
    inner = await prov._resolve()
    assert isinstance(inner, emb_mod.FastembedProvider)


@pytest.mark.asyncio
async def test_settings_dispatch_routes_to_openai_when_api(monkeypatch):
    async def fake_get_setting(db, key, default=None):
        return {
            "embedding_provider": "api",
            "embedding_api_url": "http://emb",
            "embedding_api_key": "k",
            "embedding_model_api": "M",
        }.get(key, default)

    monkeypatch.setattr("app.api.settings.get_setting", fake_get_setting)

    class _NullSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None

    monkeypatch.setattr("app.database.async_session", lambda: _NullSession())

    prov = emb_mod.SettingsDispatchProvider()
    inner = await prov._resolve()
    assert isinstance(inner, emb_mod.OpenAIEmbeddingProvider)
    assert inner.url == "http://emb"


def test_get_embedding_provider_env_dispatch(monkeypatch):
    emb_mod.set_embedding_provider(None)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    prov = emb_mod.get_embedding_provider()
    assert isinstance(prov, emb_mod.FastembedProvider)
    emb_mod.set_embedding_provider(None)


def test_get_embedding_provider_unknown_raises(monkeypatch):
    emb_mod.set_embedding_provider(None)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "nope")
    with pytest.raises(ValueError):
        emb_mod.get_embedding_provider()
    emb_mod.set_embedding_provider(None)


# --- Runtime management --------------------------------------------------------


def test_runtime_management_scopes_inspection_and_stop(monkeypatch):
    process = MagicMock()
    process.list_active.return_value = [{"container_id": "owned", "workspace_id": "workspace"}]
    process.inspect.return_value = {"container_id": "owned", "status": "running"}
    process.stop.return_value = True
    monkeypatch.setattr(rt_mod, "get_process_runtime", lambda: process)
    rt = rt_mod.RuntimeManagement()
    assert rt.stats("owned")["status"] == "running"
    process.inspect.assert_called_once_with("owned", "workspace")
    assert rt.kill("owned", "workspace") is True
    process.stop.assert_called_once_with("owned", "workspace", grace_seconds=0)
    assert rt.kill("unowned") is False
    assert rt.stats("unowned") is None


def test_kill_all_only_stops_listed_runners_in_their_scope(monkeypatch):
    process = MagicMock()
    process.list_active.return_value = [{"container_id": "owned", "workspace_id": "workspace"}]
    process.stop.return_value = True
    monkeypatch.setattr(rt_mod, "get_process_runtime", lambda: process)
    assert rt_mod.RuntimeManagement().kill_all("workspace") == 1
    process.list_active.assert_called_once_with("workspace")
    process.stop.assert_called_once_with("owned", "workspace", grace_seconds=0)


def test_management_facade_can_be_injected_without_loading_a_connector():
    runtime = object()
    try:
        rt_mod.set_agent_runtime(runtime)
        assert rt_mod.get_agent_runtime() is runtime
    finally:
        rt_mod.set_agent_runtime(None)
    assert isinstance(rt_mod.get_agent_runtime(), rt_mod.RuntimeManagement)


# --- Notifier -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_noop_notifier_returns_none():
    n = notif_mod.NoopNotifier()
    assert await n.notify("e", {}, uuid.uuid4()) is None


def test_get_notifier_env_unknown_raises(monkeypatch):
    notif_mod.set_notifier(None)
    monkeypatch.setenv("NOTIFIER", "slack")
    with pytest.raises(ValueError):
        notif_mod.get_notifier()
    notif_mod.set_notifier(None)


def test_get_notifier_default_noop(monkeypatch):
    notif_mod.set_notifier(None)
    monkeypatch.delenv("NOTIFIER", raising=False)
    assert isinstance(notif_mod.get_notifier(), notif_mod.NoopNotifier)
    notif_mod.set_notifier(None)


# --- SecretsProvider ----------------------------------------------------------

@pytest.mark.asyncio
async def test_env_secrets_provider_reads_env(monkeypatch):
    monkeypatch.setenv("MY_KEY", "value-here")
    sec = sec_mod.EnvSecretsProvider()
    assert await sec.get(None, "my_key") == "value-here"
    assert await sec.get(None, "absent", default="d") == "d"


@pytest.mark.asyncio
async def test_env_secrets_provider_set_raises():
    sec = sec_mod.EnvSecretsProvider()
    with pytest.raises(RuntimeError):
        await sec.set(None, "k", "v")


def test_get_secrets_provider_env_dispatch(monkeypatch):
    sec_mod.set_secrets_provider(None)
    monkeypatch.setenv("SECRETS_PROVIDER", "env")
    p = sec_mod.get_secrets_provider()
    assert isinstance(p, sec_mod.EnvSecretsProvider)
    sec_mod.set_secrets_provider(None)


def test_get_secrets_provider_unknown_raises(monkeypatch):
    sec_mod.set_secrets_provider(None)
    monkeypatch.setenv("SECRETS_PROVIDER", "vault")
    with pytest.raises(ValueError):
        sec_mod.get_secrets_provider()
    sec_mod.set_secrets_provider(None)
