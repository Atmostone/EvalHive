"""Template harness selection stays scoped and survives version round trips."""

import uuid


async def make_profile(client, name="Template Pi"):
    response = await client.post(
        "/api/harness-profiles", json={"name": name, "connector_id": "pi"}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_template_harness_version_rollback_and_reference_deletion(auth_client):
    profile = await make_profile(auth_client)
    response = await auth_client.post(
        "/api/templates",
        json={
            "name": "Direct engineer", "description": "Local engineering role", "soul_md": "Work carefully.",
            "harness_profile_id": profile["id"], "execution_mode": "direct",
        },
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["harness_profile_id"] == profile["id"]
    assert template["execution_mode"] == "direct"
    deleted = await auth_client.delete(f"/api/harness-profiles/{profile['id']}")
    assert deleted.status_code == 409
    response = await auth_client.put(
        f"/api/templates/{template['id']}",
        json={"harness_profile_id": None, "execution_mode": "orchestrated"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["harness_profile_id"] is None
    snapshot = (await auth_client.get(f"/api/templates/{template['id']}/versions/1")).json()["snapshot"]
    assert snapshot["harness_profile_id"] == profile["id"] and snapshot["execution_mode"] == "direct"
    restored = await auth_client.post(f"/api/templates/{template['id']}/rollback/1")
    assert restored.status_code == 200, restored.text
    current = (await auth_client.get(f"/api/templates/{template['id']}")).json()
    assert current["harness_profile_id"] == profile["id"] and current["execution_mode"] == "direct"
    await auth_client.put(f"/api/templates/{template['id']}", json={"harness_profile_id": None})
    assert (await auth_client.delete(f"/api/harness-profiles/{profile['id']}")).status_code == 204
    # A historical reference must not silently choose another harness.
    assert (await auth_client.post(f"/api/templates/{template['id']}/rollback/1")).status_code == 422


async def test_template_rejects_foreign_profile_and_invalid_mode(client, auth_client):
    registration = await client.post(
        "/api/auth/register", json={"email": "foreign-template@example.com", "password": "password1234"}
    )
    user = registration.json()
    response = await client.get("/api/harness-profiles", headers={
        "Authorization": "Bearer " + user["access_token"], "X-Workspace-Id": user["default_workspace_id"],
    })
    foreign = response.json()[0]["id"]
    base = {"name": "Scoped", "description": "Role", "soul_md": "Instructions"}
    for extra in (
        {"harness_profile_id": foreign},
        {"harness_profile_id": str(uuid.uuid4())},
        {"execution_mode": "unknown"},
    ):
        rejected = await auth_client.post("/api/templates", json={**base, **extra})
        assert rejected.status_code == 422, rejected.text
    created = await auth_client.post("/api/templates", json=base)
    assert created.status_code == 201, created.text
    assert created.json()["execution_mode"] == "orchestrated"
    rejected = await auth_client.put(
        f"/api/templates/{created.json()['id']}", json={"harness_profile_id": foreign}
    )
    assert rejected.status_code == 422
    assert (await auth_client.put(
        f"/api/templates/{created.json()['id']}", json={"execution_mode": None}
    )).status_code == 422
