"""Benchmark HTTP configuration respects tenant roles and private metadata."""

import asyncio
import json
import uuid

import pytest
from app.config import get_settings
from app.models.workspace import WorkspaceMember
from sqlalchemy import select

from tests.integration import test_benchmark_datasets as dataset_fixtures

installed_dataset_fixture = dataset_fixtures.installed_dataset_fixture


@pytest.fixture
def installed_api_benchmark(installed_dataset_fixture):
    path, receipt = installed_dataset_fixture
    manifest_path = path.parent.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["secret_schema"] = {
        "type": "object",
        "properties": {"api_key": {"type": "string"}},
        "required": ["api_key"],
        "additionalProperties": False,
    }
    manifest_path.write_text(json.dumps(manifest))
    return path, receipt


async def create_profile(client, **extra):
    response = await client.post(
        "/api/benchmark-profiles", json={"name": "Example", "connector_id": "example", **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_catalog_profile_and_dataset_http_lifecycle(auth_client, installed_api_benchmark):
    assert (await auth_client.get("/api/benchmarks/suites")).status_code == 200
    catalog = await auth_client.get("/api/benchmarks")
    assert catalog.status_code == 200 and catalog.json()[0]["installed"]
    assert catalog.json()[0]["configured"] is None and not catalog.json()[0]["live_verified"]
    profile = await create_profile(auth_client)
    assert profile["installed"] and not profile["configured"]
    assert profile["missing"] == ["secret.api_key"]
    value = "private-benchmark-fixture-never-return"
    secret = await auth_client.post(
        "/api/benchmark-secrets", json={"name": "Native account", "value": value}
    )
    assert secret.status_code == 201 and value not in secret.text
    sid = secret.json()["id"]
    update = await auth_client.patch(
        f"/api/benchmark-profiles/{profile['id']}", json={"secret_refs": {"api_key": sid}}
    )
    assert update.status_code == 200 and update.json()["configured"] and value not in update.text
    assert not update.json()["live_verified"] and update.json()["compatible"] is None
    check = await auth_client.post(f"/api/benchmark-profiles/{profile['id']}/validate")
    assert check.json() == {
        "valid": True,
        "configured": True,
        "missing": [],
        "live_verified": False,
    }
    assert value not in (await auth_client.get("/api/benchmark-secrets")).text
    assert (await auth_client.delete(f"/api/benchmark-secrets/{sid}")).status_code == 409
    replaced = await auth_client.put(
        f"/api/benchmark-secrets/{sid}",
        json={"name": "Rotated account", "value": value + "-rotated"},
    )
    assert replaced.status_code == 200 and value not in replaced.text
    request = {"profile_id": profile["id"], "dataset_id": "fixture", "revision": "native/v1"}
    first, second = await asyncio.gather(
        *[auth_client.post("/api/benchmark-datasets", json=request) for _ in range(2)]
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    dataset_id = first.json()["id"]
    assert (await auth_client.get(f"/api/benchmark-datasets/{dataset_id}")).json()[
        "case_count"
    ] == 1
    listing = await auth_client.get(f"/api/benchmark-datasets/{dataset_id}/cases")
    assert listing.status_code == 200 and listing.json()["total"] == 1
    assert listing.json()["cases"][0]["requirements"]["resources"]["memory_mb"] == 4096
    assert "hidden" not in listing.text and "native_ref" not in listing.text
    assert len((await auth_client.get("/api/benchmark-datasets")).json()) == 1
    assert (await auth_client.delete(f"/api/benchmark-profiles/{profile['id']}")).status_code == 204
    assert (await auth_client.delete(f"/api/benchmark-secrets/{sid}")).status_code == 204
    assert (await auth_client.get(f"/api/benchmark-datasets/{dataset_id}")).status_code == 200


async def test_schema_errors_do_not_echo_credential_values(auth_client, installed_api_benchmark):
    private = "private-benchmark-input-never-echo"
    for path, body in [
        ("/api/benchmark-secrets", {"name": "Bad", "value": {"nested": private}}),
        (
            "/api/benchmark-profiles",
            {"name": "Bad", "connector_id": "example", "config": {"api_key": private}},
        ),
        (
            "/api/benchmark-profiles",
            {"name": "Bad", "connector_id": "example", "entrypoint": [private]},
        ),
        (
            "/api/benchmark-profiles",
            {"name": "Bad", "connector_id": "example", "connector_version": "missing"},
        ),
    ]:
        response = await auth_client.post(path, json=body)
        assert response.status_code == 422 and private not in response.text
    profile = await create_profile(auth_client)
    assert (
        await auth_client.patch(f"/api/benchmark-profiles/{profile['id']}", json={"config": None})
    ).status_code == 422
    path, receipt = installed_api_benchmark
    receipt["cases"][0]["title"] = "Changed"
    registration = {"profile_id": profile["id"], "dataset_id": "fixture", "revision": "native/v1"}
    assert (await auth_client.post("/api/benchmark-datasets", json=registration)).status_code == 201
    path.write_text(json.dumps(receipt))
    assert (await auth_client.post("/api/benchmark-datasets", json=registration)).status_code == 409


async def test_member_can_browse_but_cannot_modify_benchmark_configuration(
    auth_client, installed_api_benchmark, db_session
):
    profile = await create_profile(auth_client)
    member = await db_session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == uuid.UUID(auth_client.headers["X-Workspace-Id"])
        )
    )
    member.role = "member"
    await db_session.commit()
    assert (await auth_client.get("/api/benchmarks")).status_code == 200
    assert (await auth_client.get("/api/benchmark-profiles")).status_code == 200
    for method, path, body in [
        ("post", "/api/benchmark-profiles", {"name": "Denied", "connector_id": "example"}),
        ("patch", f"/api/benchmark-profiles/{profile['id']}", {"name": "Denied"}),
        ("delete", f"/api/benchmark-profiles/{profile['id']}", None),
        ("post", "/api/benchmark-secrets", {"name": "Denied", "value": "fixture"}),
        (
            "post",
            "/api/benchmark-datasets",
            {"profile_id": profile["id"], "dataset_id": "fixture", "revision": "native/v1"},
        ),
    ]:
        response = await auth_client.request(method, path, **({"json": body} if body else {}))
        assert response.status_code == 403, response.text


async def test_foreign_workspace_profile_secret_and_dataset_are_inaccessible(
    auth_client, installed_api_benchmark
):
    profile = await create_profile(auth_client)
    secret = (
        await auth_client.post(
            "/api/benchmark-secrets", json={"name": "Private", "value": "fixture"}
        )
    ).json()
    dataset = (
        await auth_client.post(
            "/api/benchmark-datasets",
            json={"profile_id": profile["id"], "dataset_id": "fixture", "revision": "native/v1"},
        )
    ).json()
    provider = (
        await auth_client.post(
            "/api/providers",
            json={
                "name": "Native user fixture",
                "api_key": "fixture-only",
                "endpoint": "http://invalid.example/v1",
            },
        )
    ).json()
    model = (
        await auth_client.post(
            f"/api/providers/{provider['id']}/models",
            json={"display_name": "Fixture", "api_name": "fixture"},
        )
    ).json()
    signup = await auth_client.post(
        "/api/auth/register",
        json={
            "email": "other-" + uuid.uuid4().hex + "@example.com",
            "password": "password1234",
            "display_name": "Other",
        },
    )
    assert signup.status_code == 200, signup.text
    headers = {
        "Authorization": "Bearer " + signup.json()["access_token"],
        "X-Workspace-Id": signup.json()["default_workspace_id"],
    }
    for method, path, body in [
        ("get", f"/api/benchmark-profiles/{profile['id']}", None),
        ("delete", f"/api/benchmark-secrets/{secret['id']}", None),
        ("get", f"/api/benchmark-datasets/{dataset['id']}", None),
        ("get", f"/api/benchmark-datasets/{dataset['id']}/cases", None),
    ]:
        response = await auth_client.request(
            method, path, headers=headers, **({"json": body} if body else {})
        )
        assert response.status_code == 404, response.text
    stolen = await auth_client.post(
        "/api/benchmark-profiles",
        headers=headers,
        json={
            "name": "Stolen",
            "connector_id": "example",
            "secret_refs": {"api_key": secret["id"]},
        },
    )
    assert stolen.status_code == 422
    stolen_model = await auth_client.post(
        "/api/benchmark-profiles",
        headers=headers,
        json={
            "name": "Stolen model",
            "connector_id": "example",
            "model_bindings": {"user": model["id"]},
        },
    )
    assert stolen_model.status_code == 422


async def test_preflight_preserves_native_requirements_and_does_not_claim_runtime_admission(
    auth_client, installed_api_benchmark, monkeypatch
):
    monkeypatch.setattr(get_settings(), "benchmark_group_memory_mb", 2048)
    profile = await create_profile(auth_client)
    dataset = (
        await auth_client.post(
            "/api/benchmark-datasets",
            json={"profile_id": profile["id"], "dataset_id": "fixture", "revision": "native/v1"},
        )
    ).json()
    case = (await auth_client.get(f"/api/benchmark-datasets/{dataset['id']}/cases")).json()[
        "cases"
    ][0]
    harness = (await auth_client.get("/api/harness-profiles")).json()[0]
    request = {
        "profile_id": profile["id"],
        "dataset_revision_id": dataset["id"],
        "case_id": case["id"],
        "harness_profile_id": harness["id"],
    }
    response = await auth_client.post("/api/benchmarks/preflight", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["installed"] and not result["configured"] and not result["compatible"]
    assert result["requirements"]["resources"]["memory_mb"] == 4096
    assert any(item["code"] == "insufficient_resources" for item in result["reasons"])
    assert result["missing"] == ["model.agent", "secret.api_key"]
    assert not result["can_run"] and result["runtime_admission"] == "pending"
    assert not result["live_verified"] and result["stage"] == "static"
    assert "native_ref" not in response.text and "hidden_tests" not in response.text
    request["case_id"] = str(uuid.uuid4())
    assert (await auth_client.post("/api/benchmarks/preflight", json=request)).status_code == 404
