"""Harness declarations are code-free; profiles and credentials stay tenant scoped."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from app.config import get_settings
from app.harnesses.catalog import CatalogError, load_catalog
from app.harnesses.profiles import ProfileError, resolve_secret_refs
from app.models.workspace import WorkspaceMember
from sqlalchemy import select


@pytest.fixture
def installed_fake(tmp_path, monkeypatch):
    root = Path(get_settings().harness_manifest_dir)
    builtin = json.loads((root / "pi/manifest.json").read_text())
    pi_dir = tmp_path / "pi"
    pi_dir.mkdir()
    (pi_dir / "manifest.json").write_text(json.dumps(builtin))
    fake = copy.deepcopy(builtin)
    fake.update(
        id="external.fake",
        display_name="External test connector",
        version="7.3.2",
        entrypoint=["python", "connector.py"],
        image="example/fake@sha256:" + "a" * 64,
    )
    fake["config_schema"] = {
        "type": "object",
        "properties": {"native_prefix": {"type": "string"}},
        "additionalProperties": False,
    }
    fake_dir = tmp_path / "external"
    fake_dir.mkdir()
    (fake_dir / "manifest.json").write_text(json.dumps(fake))
    # Importing this would fail the test. Catalog discovery only reads JSON.
    (fake_dir / "connector.py").write_text("raise RuntimeError('Connector code was imported')\n")
    monkeypatch.setattr(get_settings(), "harness_manifest_dir", str(tmp_path))
    return tmp_path


async def create_fake(client, name="Custom", **extra):
    response = await client.post(
        "/api/harness-profiles",
        json={
            "name": name,
            "connector_id": "external.fake",
            "config": {"native_prefix": "hello"},
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_opencode_registers_through_the_common_profile_api(auth_client):
    catalog = (await auth_client.get("/api/harnesses")).json()
    declaration = next(row for row in catalog if row["id"] == "opencode")
    assert declaration["requirements"]["harness_version"] == "1.19.5"
    assert declaration["capabilities"]["approval"] == "native"
    response = await auth_client.post(
        "/api/harness-profiles",
        json={
            "name": "OpenCode native",
            "connector_id": declaration["id"],
            "config": {"permissionMode": "ask", "maxOutputTokens": 1024},
        },
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["connector_version"] == declaration["version"]
    assert (
        await auth_client.post(f"/api/harness-profiles/{profile['id']}/validate")
    ).json() == {"valid": True}
    rejected = await auth_client.patch(
        f"/api/harness-profiles/{profile['id']}", json={"config": {"plugins": ["untrusted"]}}
    )
    assert rejected.status_code == 422
    unchanged = (await auth_client.get(f"/api/harness-profiles/{profile['id']}")).json()
    assert unchanged["config"] == profile["config"]


async def test_builtin_profile_catalog_and_external_crud(auth_client, installed_fake):
    catalog = await auth_client.get("/api/harnesses")
    assert catalog.status_code == 200
    assert {row["id"] for row in catalog.json()} == {"pi", "external.fake"}
    initial = (await auth_client.get("/api/harness-profiles")).json()
    assert len(initial) == 1 and initial[0]["connector_id"] == "pi" and initial[0]["is_default"]
    profile = await create_fake(auth_client)
    assert profile["connector_version"] == "7.3.2"
    assert profile["config"] == {"native_prefix": "hello"}
    pid = profile["id"]
    response = await auth_client.patch(
        f"/api/harness-profiles/{pid}",
        json={"name": "Renamed", "config": {"native_prefix": "world"}},
    )
    assert response.status_code == 200 and response.json()["name"] == "Renamed"
    assert (await auth_client.get(f"/api/harness-profiles/{pid}")).json()["config"][
        "native_prefix"
    ] == "world"
    assert (await auth_client.post(f"/api/harness-profiles/{pid}/validate")).json() == {
        "valid": True
    }
    assert (await auth_client.delete(f"/api/harness-profiles/{pid}")).status_code == 204
    assert (await auth_client.get(f"/api/harness-profiles/{pid}")).status_code == 404


async def test_configuration_version_and_default_rules(auth_client, installed_fake):
    initial = (await auth_client.get("/api/harness-profiles")).json()[0]
    assert (await auth_client.delete(f"/api/harness-profiles/{initial['id']}")).status_code == 409
    assert (
        await auth_client.patch(
            f"/api/harness-profiles/{initial['id']}", json={"is_default": False}
        )
    ).status_code == 409
    for bad in [
        {"connector_version": "missing"},
        {"connector_id": "not-installed"},
        {"config": {"native_prefix": 3}},
        {"config": {"unsupported": True}},
    ]:
        response = await auth_client.post(
            "/api/harness-profiles", json={"name": "bad", "connector_id": "external.fake", **bad}
        )
        assert response.status_code == 422
    profile = await create_fake(auth_client, is_default=True)
    assert profile["is_default"]
    rows = (await auth_client.get("/api/harness-profiles")).json()
    assert sum(row["is_default"] for row in rows) == 1
    assert next(row for row in rows if row["is_default"])["id"] == profile["id"]
    assert (await auth_client.delete(f"/api/harness-profiles/{initial['id']}")).status_code == 204
    duplicate = await auth_client.post(
        "/api/harness-profiles", json={"name": profile["name"], "connector_id": "external.fake"}
    )
    assert duplicate.status_code == 409


async def test_secrets_are_write_only_and_errors_never_echo_input(
    auth_client, installed_fake, db_session
):
    value = "test-private-harness-credential-do-not-echo"
    created = await auth_client.post(
        "/api/harness-secrets", json={"name": "service", "value": value}
    )
    assert created.status_code == 201 and value not in created.text
    sid = created.json()["id"]
    assert "value" not in created.json()
    assert value not in (await auth_client.get("/api/harness-secrets")).text
    invalid = await auth_client.post(
        "/api/harness-secrets", json={"name": "bad", "value": {"nested": value}}
    )
    assert invalid.status_code == 422 and value not in invalid.text
    invalid = await auth_client.post(
        "/api/harness-profiles",
        json={"name": "bad", "connector_id": "external.fake", "config": {"api_key": value}},
    )
    assert invalid.status_code == 422 and value not in invalid.text
    profile = await create_fake(auth_client, secret_refs={"service": sid})
    assert value not in json.dumps(profile) and profile["secret_refs"] == {"service": sid}
    assert (await auth_client.delete(f"/api/harness-secrets/{sid}")).status_code == 409
    updated = await auth_client.put(
        f"/api/harness-secrets/{sid}", json={"name": "service", "value": value + "-rotated"}
    )
    assert updated.status_code == 200 and value not in updated.text
    import uuid

    resolved = await resolve_secret_refs(
        db_session, uuid.UUID(auth_client.headers["X-Workspace-Id"]), {"service": sid}
    )
    assert resolved == {"service": value + "-rotated"}
    await auth_client.delete(f"/api/harness-profiles/{profile['id']}")
    assert (await auth_client.delete(f"/api/harness-secrets/{sid}")).status_code == 204


async def test_foreign_profiles_secrets_and_models_are_rejected(client, installed_fake, db_session):
    users = []
    for name in ("a", "b"):
        response = await client.post(
            "/api/auth/register",
            json={"email": f"harness-{name}@example.com", "password": "password1234"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        users.append(
            {
                "Authorization": "Bearer " + payload["access_token"],
                "X-Workspace-Id": payload["default_workspace_id"],
            }
        )
    first, second = users
    secret = await client.post(
        "/api/harness-secrets", json={"name": "private", "value": "test-value"}, headers=first
    )
    sid = secret.json()["id"]
    response = await client.post(
        "/api/harness-profiles",
        json={"name": "private", "connector_id": "external.fake", "secret_refs": {"slot": sid}},
        headers=first,
    )
    pid = response.json()["id"]
    for method, path, body in [
        ("get", f"/api/harness-profiles/{pid}", None),
        ("patch", f"/api/harness-profiles/{pid}", {"name": "stolen"}),
        ("delete", f"/api/harness-profiles/{pid}", None),
        ("delete", f"/api/harness-secrets/{sid}", None),
    ]:
        response = await client.request(
            method, path, headers=second, **({"json": body} if body else {})
        )
        assert response.status_code == 404
    response = await client.post(
        "/api/harness-profiles",
        json={"name": "stolen", "connector_id": "external.fake", "secret_refs": {"slot": sid}},
        headers=second,
    )
    assert response.status_code == 422
    import uuid

    with pytest.raises(ProfileError):
        await resolve_secret_refs(db_session, uuid.UUID(second["X-Workspace-Id"]), {"slot": sid})
    provider = await client.post(
        "/api/providers",
        json={"name": "owner", "api_key": "test", "endpoint": "http://invalid.test/v1"},
        headers=first,
    )
    model = await client.post(
        f"/api/providers/{provider.json()['id']}/models",
        json={"display_name": "Test", "api_name": "test"},
        headers=first,
    )
    response = await client.post(
        "/api/harness-profiles",
        json={
            "name": "foreign-model",
            "connector_id": "external.fake",
            "model_id": model.json()["id"],
        },
        headers=second,
    )
    assert response.status_code == 422


async def test_member_can_read_but_cannot_change_profiles_or_secrets(
    auth_client, installed_fake, db_session
):
    import uuid

    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    member = await db_session.scalar(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
    )
    member.role = "member"
    await db_session.commit()
    assert (await auth_client.get("/api/harnesses")).status_code == 200
    assert (await auth_client.get("/api/harness-profiles")).status_code == 200
    assert (
        await auth_client.post(
            "/api/harness-profiles", json={"name": "blocked", "connector_id": "pi"}
        )
    ).status_code == 403
    assert (
        await auth_client.post(
            "/api/harness-secrets", json={"name": "blocked", "value": "test-value"}
        )
    ).status_code == 403


async def test_concurrent_default_selection_keeps_exactly_one(
    auth_client, installed_fake, db_session
):
    first, second = await asyncio.gather(
        create_fake(auth_client, "First"), create_fake(auth_client, "Second")
    )
    responses = await asyncio.gather(
        *[
            auth_client.patch(f"/api/harness-profiles/{profile['id']}", json={"is_default": True})
            for profile in (first, second)
        ]
    )
    assert all(response.status_code == 200 for response in responses)
    rows = (await auth_client.get("/api/harness-profiles")).json()
    assert sum(row["is_default"] for row in rows) == 1


async def test_catalog_rejects_duplicate_and_incompatible_declarations(installed_fake):
    path = installed_fake / "external/manifest.json"
    original = json.loads(path.read_text())
    duplicate = {**original, "id": "pi"}
    path.write_text(json.dumps(duplicate))
    with pytest.raises(CatalogError, match="Duplicate"):
        load_catalog()
    path.write_text(json.dumps({**original, "protocol_version": "99"}))
    with pytest.raises(CatalogError):
        load_catalog()
    path.write_text(
        json.dumps({**original, "config_schema": {"$ref": "https://invalid.test/schema"}})
    )
    with pytest.raises(CatalogError, match="local references"):
        load_catalog()


async def test_profile_is_not_silently_rebound_after_connector_upgrade(auth_client, installed_fake):
    profile = await create_fake(auth_client)
    path = installed_fake / "external/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["version"] = "8.0.0"
    path.write_text(json.dumps(manifest))
    response = await auth_client.post(f"/api/harness-profiles/{profile['id']}/validate")
    assert response.status_code == 422 and "Pinned" in response.text
    assert (await auth_client.get(f"/api/harness-profiles/{profile['id']}")).json()[
        "connector_version"
    ] == "7.3.2"
    response = await auth_client.patch(
        f"/api/harness-profiles/{profile['id']}", json={"connector_version": "8.0.0"}
    )
    assert response.status_code == 200


async def test_external_connector_without_model_requirement_resolves_without_core_imports(
    auth_client, db_session, installed_fake,
):
    import uuid

    from app.harnesses.launch import resolve_launch
    from app.models.task import Task

    declaration = installed_fake / "external/manifest.json"
    manifest = json.loads(declaration.read_text())
    manifest.pop("requirements", None)
    declaration.write_text(json.dumps(manifest))
    profile = await create_fake(auth_client)
    task = Task(title="Independent connector", workspace_id=uuid.UUID(auth_client.headers["X-Workspace-Id"]),
                harness_profile_id=uuid.UUID(profile["id"]), execution_mode="direct")
    db_session.add(task)
    await db_session.flush()
    resolved = await resolve_launch(db_session, task)
    assert resolved.spec["model"] is None and resolved.model is None
    assert resolved.spec["config"] == {"native_prefix": "hello"}
    assert "model_api_key" not in resolved.credentials
