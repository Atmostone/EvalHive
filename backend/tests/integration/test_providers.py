"""Integration tests for /api/providers and /api/models."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.models.provider import LLMModel, Provider
from app.models.template import Template
from app.models.workspace import DEFAULT_WORKSPACE_ID


@pytest.mark.asyncio
async def test_provider_crud_lifecycle(auth_client: AsyncClient):
    # The fixture-bootstrapped workspace may already contain a cloned default provider.
    initial = await auth_client.get("/api/providers")
    assert initial.status_code == 200
    initial_ids = {p["id"] for p in initial.json()}

    # Create
    r = await auth_client.post(
        "/api/providers",
        json={"name": "openai-crud", "api_key": "sk-test-abcd1234", "endpoint": "https://api.openai.com/v1"},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "openai-crud"
    assert created["api_key_masked"] == "***1234"
    assert "api_key" not in created
    pid = created["id"]

    # List shows masked key, has the new provider
    r = await auth_client.get("/api/providers")
    assert r.status_code == 200
    body = r.json()
    by_id = {p["id"]: p for p in body}
    assert pid in by_id and pid not in initial_ids
    assert by_id[pid]["api_key_masked"] == "***1234"

    # Update endpoint, keep api_key
    r = await auth_client.patch(
        f"/api/providers/{pid}", json={"endpoint": "https://api.openai.com/v2"}
    )
    assert r.status_code == 200
    assert r.json()["endpoint"] == "https://api.openai.com/v2"
    assert r.json()["api_key_masked"] == "***1234"

    # Update api_key — mask must change
    r = await auth_client.patch(
        f"/api/providers/{pid}", json={"api_key": "sk-new-xyz9876"}
    )
    assert r.status_code == 200
    assert r.json()["api_key_masked"] == "***9876"

    # Delete
    r = await auth_client.delete(f"/api/providers/{pid}")
    assert r.status_code == 204
    r = await auth_client.get("/api/providers")
    assert all(p["id"] != pid for p in r.json())


@pytest.mark.asyncio
async def test_provider_name_collision_409(auth_client: AsyncClient):
    payload = {"name": "dup", "api_key": "x", "endpoint": "http://a"}
    r1 = await auth_client.post("/api/providers", json=payload)
    assert r1.status_code == 201
    r2 = await auth_client.post("/api/providers", json=payload)
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_model_crud_lifecycle(auth_client: AsyncClient):
    r = await auth_client.post(
        "/api/providers",
        json={"name": "p1", "api_key": "k", "endpoint": "http://e"},
    )
    pid = r.json()["id"]

    # Create model
    r = await auth_client.post(
        f"/api/providers/{pid}/models",
        json={
            "display_name": "GPT-4o",
            "api_name": "gpt-4o",
            "input_price_per_1m_usd": "2.5",
            "output_price_per_1m_usd": "10",
        },
    )
    assert r.status_code == 201, r.text
    model = r.json()
    assert model["display_name"] == "GPT-4o"
    assert model["api_name"] == "gpt-4o"
    assert model["input_price_per_1m_usd"] == 2.5
    assert model["output_price_per_1m_usd"] == 10
    mid = model["id"]

    # List
    r = await auth_client.get(f"/api/providers/{pid}/models")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Update price
    r = await auth_client.patch(
        f"/api/models/{mid}", json={"input_price_per_1m_usd": "3.0"}
    )
    assert r.status_code == 200
    assert r.json()["input_price_per_1m_usd"] == 3.0

    # Delete
    r = await auth_client.delete(f"/api/models/{mid}")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_provider_workspace_isolation(client: AsyncClient):
    """Each user's workspace sees only its own providers."""
    # User A
    rA = await client.post(
        "/api/auth/register",
        json={"email": "a@test.dev", "password": "secret1234", "display_name": "A"},
    )
    tokA = rA.json()["access_token"]
    wsA = rA.json()["default_workspace_id"]
    rB = await client.post(
        "/api/auth/register",
        json={"email": "b@test.dev", "password": "secret1234", "display_name": "B"},
    )
    tokB = rB.json()["access_token"]
    wsB = rB.json()["default_workspace_id"]

    # A creates a provider
    r = await client.post(
        "/api/providers",
        json={"name": "only-A", "api_key": "secret-key", "endpoint": "http://a"},
        headers={"Authorization": f"Bearer {tokA}", "X-Workspace-Id": wsA},
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    # B cannot see it
    r = await client.get(
        "/api/providers",
        headers={"Authorization": f"Bearer {tokB}", "X-Workspace-Id": wsB},
    )
    assert r.status_code == 200
    assert all(p["id"] != pid for p in r.json())

    # B cannot fetch/update/delete it (404)
    r = await client.patch(
        f"/api/providers/{pid}",
        json={"name": "stolen"},
        headers={"Authorization": f"Bearer {tokB}", "X-Workspace-Id": wsB},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_model_sets_template_model_id_null(
    auth_client: AsyncClient, db_session
):
    """Cascade behaviour: deleting a model leaves template.model_id NULL via SET NULL."""
    r = await auth_client.post(
        "/api/providers",
        json={"name": "p", "api_key": "k", "endpoint": "http://e"},
    )
    pid = r.json()["id"]
    r = await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M", "api_name": "m"},
    )
    mid = r.json()["id"]

    # Create a template referencing this model
    tpl = Template(
        name="tpl", description="d", soul_md="s",
        model_id=mid, tool_ids=[], tags=[],
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(tpl)
    await db_session.commit()
    await db_session.refresh(tpl)
    assert tpl.model_id is not None

    # Delete the model — template.model_id should go NULL.
    r = await auth_client.delete(f"/api/models/{mid}")
    assert r.status_code == 204
    await db_session.refresh(tpl)
    assert tpl.model_id is None


@pytest.mark.asyncio
async def test_delete_provider_cascades_to_models(auth_client: AsyncClient, db_session):
    r = await auth_client.post(
        "/api/providers", json={"name": "p", "api_key": "k", "endpoint": "http://e"}
    )
    pid = r.json()["id"]
    await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M1", "api_name": "m1"},
    )
    await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M2", "api_name": "m2"},
    )

    r = await auth_client.delete(f"/api/providers/{pid}")
    assert r.status_code == 204

    # No models for that provider remain
    from sqlalchemy import select
    rows = (await db_session.execute(select(LLMModel))).scalars().all()
    rows = [m for m in rows if str(m.provider_id) == pid]
    assert rows == []


@pytest.mark.asyncio
async def test_model_update_endpoint_404_for_unknown(auth_client: AsyncClient):
    import uuid
    r = await auth_client.patch(
        f"/api/models/{uuid.uuid4()}", json={"display_name": "x"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_model_update_changes_api_name(auth_client: AsyncClient):
    r = await auth_client.post(
        "/api/providers", json={"name": "p-upd", "api_key": "k", "endpoint": "http://e"}
    )
    pid = r.json()["id"]
    r = await auth_client.post(
        f"/api/providers/{pid}/models", json={"display_name": "Old", "api_name": "old-id"},
    )
    mid = r.json()["id"]

    r = await auth_client.patch(
        f"/api/models/{mid}",
        json={"display_name": "New", "api_name": "new-id"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["display_name"] == "New"
    assert body["api_name"] == "new-id"


@pytest.mark.asyncio
async def test_provider_update_404_for_unknown(auth_client: AsyncClient):
    import uuid
    r = await auth_client.patch(
        f"/api/providers/{uuid.uuid4()}", json={"name": "x"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cross_workspace_model_delete_404(client: AsyncClient):
    """Deleting another workspace's model must return 404, not 204."""
    rA = await client.post(
        "/api/auth/register",
        json={"email": "del-a@test.dev", "password": "secret1234", "display_name": "A"},
    )
    tokA, wsA = rA.json()["access_token"], rA.json()["default_workspace_id"]
    rB = await client.post(
        "/api/auth/register",
        json={"email": "del-b@test.dev", "password": "secret1234", "display_name": "B"},
    )
    tokB, wsB = rB.json()["access_token"], rB.json()["default_workspace_id"]

    r = await client.post(
        "/api/providers",
        json={"name": "p-cross", "api_key": "k", "endpoint": "http://e"},
        headers={"Authorization": f"Bearer {tokA}", "X-Workspace-Id": wsA},
    )
    pid = r.json()["id"]
    r = await client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M", "api_name": "m"},
        headers={"Authorization": f"Bearer {tokA}", "X-Workspace-Id": wsA},
    )
    mid = r.json()["id"]

    # B tries to delete A's model → 404
    r = await client.delete(
        f"/api/models/{mid}",
        headers={"Authorization": f"Bearer {tokB}", "X-Workspace-Id": wsB},
    )
    assert r.status_code == 404

    # B tries to delete A's provider → 404
    r = await client.delete(
        f"/api/providers/{pid}",
        headers={"Authorization": f"Bearer {tokB}", "X-Workspace-Id": wsB},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_provider_with_short_api_key_masking():
    """Short keys (<= 4 chars) are fully masked as '***'."""
    from app.api._resolve_model import mask_api_key
    assert mask_api_key("") == ""
    assert mask_api_key("ab") == "***"
    assert mask_api_key("abcd") == "***"
    assert mask_api_key("abcde") == "***bcde"


@pytest.mark.asyncio
async def test_invalid_uuid_400(auth_client: AsyncClient):
    r = await auth_client.patch("/api/providers/not-a-uuid", json={"name": "x"})
    assert r.status_code == 400
    r = await auth_client.patch("/api/models/not-a-uuid", json={})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_model_for_unknown_provider_404(auth_client: AsyncClient):
    import uuid
    r = await auth_client.get(f"/api/providers/{uuid.uuid4()}/models")
    assert r.status_code == 404
    r = await auth_client.post(
        f"/api/providers/{uuid.uuid4()}/models",
        json={"display_name": "x", "api_name": "y"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_model_api_name_collision_409(auth_client: AsyncClient):
    r = await auth_client.post(
        "/api/providers", json={"name": "p-dup", "api_key": "k", "endpoint": "http://e"}
    )
    pid = r.json()["id"]
    r1 = await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M", "api_name": "same"},
    )
    assert r1.status_code == 201
    r2 = await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M2", "api_name": "same"},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_model_test_endpoint_reports_error_on_exception(
    auth_client: AsyncClient, monkeypatch
):
    """If the LLM call raises, /test returns status=error with the message (no 500)."""
    from unittest.mock import AsyncMock, MagicMock

    r = await auth_client.post(
        "/api/providers", json={"name": "p-err", "api_key": "k", "endpoint": "http://e"}
    )
    pid = r.json()["id"]
    r = await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M", "api_name": "m"},
    )
    mid = r.json()["id"]

    fake_provider = MagicMock()
    fake_provider.acompletion = AsyncMock(side_effect=RuntimeError("connection refused"))

    import app.api.providers as providers_mod
    monkeypatch.setattr(providers_mod, "get_llm_provider", lambda: fake_provider)

    r = await auth_client.post(f"/api/models/{mid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert "connection refused" in body["error"]


@pytest.mark.asyncio
async def test_model_test_endpoint_returns_status(auth_client: AsyncClient, monkeypatch):
    """/api/models/{id}/test wraps the LLM call; we mock the provider to return a fake completion."""
    from unittest.mock import AsyncMock, MagicMock

    r = await auth_client.post(
        "/api/providers", json={"name": "p", "api_key": "k", "endpoint": "http://e"}
    )
    pid = r.json()["id"]
    r = await auth_client.post(
        f"/api/providers/{pid}/models",
        json={"display_name": "M", "api_name": "m"},
    )
    mid = r.json()["id"]

    fake_message = MagicMock()
    fake_message.content = "pong"
    fake_choice = MagicMock(message=fake_message)
    fake_resp = MagicMock(choices=[fake_choice])

    fake_provider = MagicMock()
    fake_provider.acompletion = AsyncMock(return_value=fake_resp)

    import app.api.providers as providers_mod
    monkeypatch.setattr(providers_mod, "get_llm_provider", lambda: fake_provider)

    r = await auth_client.post(f"/api/models/{mid}/test")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "latency_ms" in body
    assert body["sample"] == "pong"
