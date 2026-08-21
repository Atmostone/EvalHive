"""Broad CRUD smoke tests across the API surface.

Each handler runs the auth + workspace dependency chain and basic logic, which
gives us a fat coverage win for free. Specifics (idempotency, scoping) are
covered in dedicated test files.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health(auth_client: AsyncClient):
    r = await auth_client.get("/api/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_me_returns_workspaces(auth_client: AsyncClient):
    r = await auth_client.get("/api/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"]
    assert any(w["role"] == "owner" for w in body["workspaces"])


@pytest.mark.asyncio
async def test_tasks_full_crud(auth_client: AsyncClient):
    create = await auth_client.post(
        "/api/tasks", json={"title": "smoke", "description": "x", "priority": "high"}
    )
    assert create.status_code == 201
    tid = create.json()["id"]

    listed = await auth_client.get("/api/tasks")
    assert listed.status_code == 200
    assert any(t["id"] == tid for t in listed.json())

    one = await auth_client.get(f"/api/tasks/{tid}")
    assert one.status_code == 200

    updated = await auth_client.patch(
        f"/api/tasks/{tid}", json={"description": "updated"}
    )
    assert updated.status_code == 200
    assert updated.json()["description"] == "updated"

    deleted = await auth_client.delete(f"/api/tasks/{tid}")
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_templates_full_crud(auth_client: AsyncClient):
    listed = await auth_client.get("/api/templates")
    assert listed.status_code == 200
    # Default workspace seeds 5 templates that are copied to new users on register.
    assert len(listed.json()) >= 1

    create = await auth_client.post(
        "/api/templates",
        json={"name": "smoke-tpl", "description": "x", "soul_md": "# smoke"},
    )
    assert create.status_code == 201
    tpl_id = create.json()["id"]

    one = await auth_client.get(f"/api/templates/{tpl_id}")
    assert one.status_code == 200

    upd = await auth_client.put(
        f"/api/templates/{tpl_id}", json={"description": "y"}
    )
    assert upd.status_code == 200
    assert upd.json()["description"] == "y"

    versions = await auth_client.get(f"/api/templates/{tpl_id}/versions")
    assert versions.status_code == 200
    assert len(versions.json()) >= 1
    v_num = versions.json()[0]["version"]

    v_one = await auth_client.get(f"/api/templates/{tpl_id}/versions/{v_num}")
    assert v_one.status_code == 200

    deleted = await auth_client.delete(f"/api/templates/{tpl_id}")
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_memory_entities_crud(auth_client: AsyncClient):
    create = await auth_client.post(
        "/api/memory/entities",
        json={"type": "person", "name": "Smoke Person", "attributes": {"role": "tester"}},
    )
    assert create.status_code == 201
    eid = create.json()["id"]

    listed = await auth_client.get("/api/memory/entities")
    assert listed.status_code == 200
    assert any(e["id"] == eid for e in listed.json())

    one = await auth_client.get(f"/api/memory/entities/{eid}")
    assert one.status_code == 200

    upd = await auth_client.patch(
        f"/api/memory/entities/{eid}",
        json={"attributes": {"role": "qa"}},
    )
    assert upd.status_code == 200

    deleted = await auth_client.delete(f"/api/memory/entities/{eid}")
    assert deleted.status_code in (200, 204)


@pytest.mark.asyncio
async def test_analytics_endpoints_return_200(auth_client: AsyncClient):
    for path in ("/api/analytics/templates?period=week",
                 "/api/analytics/timeline?days=7",
                 "/api/analytics/models?period=week"):
        r = await auth_client.get(path)
        assert r.status_code == 200, f"{path}: {r.text}"


@pytest.mark.asyncio
async def test_scheduled_jobs_crud(auth_client: AsyncClient):
    create = await auth_client.post(
        "/api/scheduled-jobs",
        json={
            "name": "rollup",
            "kind": "interval",
            "interval_seconds": 3600,
            "enabled": False,  # disabled so we don't accidentally fire
            "payload": {"action": "noop"},
        },
    )
    assert create.status_code == 201
    jid = create.json()["id"]

    listed = await auth_client.get("/api/scheduled-jobs")
    assert listed.status_code == 200
    assert any(j["id"] == jid for j in listed.json())

    upd = await auth_client.patch(
        f"/api/scheduled-jobs/{jid}", json={"enabled": False, "name": "rollup-2"}
    )
    assert upd.status_code == 200

    deleted = await auth_client.delete(f"/api/scheduled-jobs/{jid}")
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_settings_get_and_patch(auth_client: AsyncClient):
    r = await auth_client.get("/api/settings")
    assert r.status_code == 200

    patch = await auth_client.patch(
        "/api/settings", json={"max_concurrent_agents": "5"}
    )
    assert patch.status_code == 200


@pytest.mark.asyncio
async def test_events_list_returns_200(auth_client: AsyncClient):
    r = await auth_client.get("/api/events?limit=10")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_agents_list_returns_200(auth_client: AsyncClient, monkeypatch):
    # Patch runtime so we don't poke real Docker.
    from app.plugins import runtime as rt

    class _NoopRuntime:
        def list_active(self, workspace_id=None):
            return []
    rt.set_agent_runtime(_NoopRuntime())
    try:
        r = await auth_client.get("/api/agents")
        assert r.status_code == 200
        assert r.json() == []
    finally:
        rt.set_agent_runtime(None)


@pytest.mark.asyncio
async def test_chat_history_endpoint(auth_client: AsyncClient):
    r = await auth_client.get("/api/chat/history?limit=5")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_template_rollback_creates_new_version(auth_client: AsyncClient):
    create = await auth_client.post(
        "/api/templates",
        json={"name": "rollback-target", "description": "v1", "soul_md": "# v1"},
    )
    tpl_id = create.json()["id"]

    # Update to v2 — produces version 1 (auto-snapshot of v1).
    await auth_client.put(
        f"/api/templates/{tpl_id}", json={"description": "v2"}
    )
    versions = (await auth_client.get(f"/api/templates/{tpl_id}/versions")).json()
    v1 = next(v for v in versions if "pre-update" in (v["commit_message"] or ""))

    rb = await auth_client.post(
        f"/api/templates/{tpl_id}/rollback/{v1['version']}"
    )
    assert rb.status_code == 200
    assert rb.json()["description"] == "v1"


@pytest.mark.asyncio
async def test_template_version_404_for_missing(auth_client: AsyncClient):
    create = await auth_client.post(
        "/api/templates",
        json={"name": "vmiss", "description": "x", "soul_md": "# x"},
    )
    tpl_id = create.json()["id"]
    r = await auth_client.get(f"/api/templates/{tpl_id}/versions/9999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_events_with_filters(auth_client: AsyncClient):
    """Exercise the optional filter branches on /api/events."""
    r = await auth_client.get(
        "/api/events?event_type=test_type&source=test&limit=5&offset=0"
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_settings_export_all_returns_zip(auth_client: AsyncClient):
    """Hits export-all path which streams a multi-CSV zip."""
    r = await auth_client.get("/api/settings/export-all")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/")


@pytest.mark.asyncio
async def test_task_create_with_template_id(auth_client: AsyncClient):
    tpls = (await auth_client.get("/api/templates")).json()
    if not tpls:
        return
    r = await auth_client.post(
        "/api/tasks",
        json={
            "title": "with-tpl",
            "description": "x",
            "priority": "low",
            "template_id": tpls[0]["id"],
        },
    )
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_task_approve_path(auth_client: AsyncClient, db_session):
    """Approve transitions awaiting_approval → done."""
    from app.models.task import Task, TaskStatus
    create = await auth_client.post(
        "/api/tasks", json={"title": "approve me", "priority": "low"}
    )
    tid = create.json()["id"]
    # Manually transition to awaiting_approval (orchestrator does this normally).
    import uuid as _uuid
    task = await db_session.get(Task, _uuid.UUID(tid))
    task.status = TaskStatus.AWAITING_APPROVAL.value
    await db_session.commit()

    r = await auth_client.patch(f"/api/tasks/{tid}/approve")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_template_404_for_missing_id(auth_client: AsyncClient):
    bad_id = uuid.uuid4()
    r = await auth_client.get(f"/api/templates/{bad_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_task_404_for_missing_id(auth_client: AsyncClient):
    bad_id = uuid.uuid4()
    r = await auth_client.get(f"/api/tasks/{bad_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_memory_entity_404_for_missing_id(auth_client: AsyncClient):
    bad_id = uuid.uuid4()
    r = await auth_client.get(f"/api/memory/entities/{bad_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_memory_relation_create_and_delete(auth_client: AsyncClient):
    e1 = await auth_client.post(
        "/api/memory/entities",
        json={"type": "x", "name": "From"},
    )
    e2 = await auth_client.post(
        "/api/memory/entities",
        json={"type": "x", "name": "To"},
    )
    rel = await auth_client.post(
        "/api/memory/relations",
        json={
            "from_id": e1.json()["id"],
            "to_id": e2.json()["id"],
            "relation_type": "knows",
        },
    )
    assert rel.status_code in (200, 201)
    rel_id = rel.json()["id"]

    listed = await auth_client.get("/api/memory/relations")
    assert listed.status_code == 200
    assert any(r["id"] == rel_id for r in listed.json())

    deleted = await auth_client.delete(f"/api/memory/relations/{rel_id}")
    assert deleted.status_code in (200, 204)


@pytest.mark.asyncio
async def test_knowledge_rules_and_memory_endpoints(auth_client: AsyncClient):
    g = await auth_client.get("/api/knowledge/rules")
    assert g.status_code == 200

    p = await auth_client.put(
        "/api/knowledge/rules", json={"content": "# rules\n- be nice"}
    )
    assert p.status_code == 200

    g2 = await auth_client.get("/api/knowledge/rules")
    assert "be nice" in g2.json().get("content", "")

    pm = await auth_client.put(
        "/api/knowledge/memory", json={"content": "memory"}
    )
    assert pm.status_code == 200

    docs = await auth_client.get("/api/knowledge/documents")
    assert docs.status_code == 200


@pytest.mark.asyncio
async def test_a_manual_requeue_drops_the_failed_attempts_reason(auth_client: AsyncClient, db_session):
    """SPA-113. `failure_type` describes the attempt that failed. Carried across a
    manual re-queue it marks the next run with a reason that is no longer true —
    and a contaminating one silently removes a *successful* measurement from every
    aggregate, because `measures_the_model()` drops it."""
    import uuid as _uuid

    from app.models.task import Task, TaskStatus
    from app.models.template import Template
    from app.utils.failures import FAILURE_AGENT, FAILURE_INFRA

    tpl = Template(
        name=f"pin-{_uuid.uuid4().hex[:6]}", description="d", soul_md="# soul",
        tool_ids=[], tags=[],
        workspace_id=_uuid.UUID(auth_client.headers["X-Workspace-Id"]),
    )
    db_session.add(tpl)
    await db_session.commit()

    async def _failed(failure_type, *, template_id=None, condition=False):
        r = await auth_client.post(
            "/api/tasks", json={"title": f"requeue {failure_type}", "priority": "low"}
        )
        tid = r.json()["id"]
        task = await db_session.get(Task, _uuid.UUID(tid))
        task.status = TaskStatus.FAILED.value
        task.failure_type = failure_type
        task.template_id = template_id
        task.condition_contaminated = condition
        await db_session.commit()
        return tid

    # The value is visible at all — it was recorded from the start and readable
    # only in SQL until now.
    tid = await _failed(FAILURE_AGENT)
    assert (await auth_client.get(f"/api/tasks/{tid}")).json()["failure_type"] == FAILURE_AGENT

    # An agent-caused reason belongs to the attempt and goes with it.
    body = (await auth_client.patch(f"/api/tasks/{tid}", json={"status": "ready"})).json()
    assert body["failure_type"] is None

    # A contamination with nothing pinned goes too: the orchestrator never got as
    # far as choosing, so the next attempt inherits nothing to be tainted by.
    tid = await _failed(FAILURE_INFRA)
    body = (await auth_client.patch(f"/api/tasks/{tid}", json={"status": "ready"})).json()
    assert body["failure_type"] is None

    # …but a contamination describing the CONDITION survives (SPA-87): the
    # degraded orchestrator substituted this template and pinned it, so the next
    # attempt runs under the same substituted condition.
    tid = await _failed(FAILURE_INFRA, template_id=tpl.id, condition=True)
    body = (await auth_client.patch(f"/api/tasks/{tid}", json={"status": "ready"})).json()
    assert body["failure_type"] == FAILURE_INFRA
    assert body["condition_contaminated"] is True


@pytest.mark.asyncio
async def test_a_pinned_template_alone_does_not_make_a_contamination_permanent(
    auth_client: AsyncClient, db_session
):
    """SPA-115. The re-queue guard used to read `template_id is not None` as «the
    orchestrator degraded and pinned a substitute». But every orchestrated task
    has a template_id — including one that chose its template perfectly well and
    then died on an agent-side 429.

    Such a task kept `llm_rate_limit` through every re-queue, so its next run —
    a successful one, measuring the model exactly as intended — was dropped from
    every aggregate by `measures_the_model()`, permanently and silently. Whose
    contamination it is has to be recorded, not guessed."""
    import uuid as _uuid

    from app.models.task import Task, TaskStatus
    from app.models.template import Template
    from app.utils.failures import FAILURE_LLM_RATE_LIMIT

    tpl = Template(
        name=f"ordinary-{_uuid.uuid4().hex[:6]}", description="d", soul_md="# soul",
        tool_ids=[], tags=[],
        workspace_id=_uuid.UUID(auth_client.headers["X-Workspace-Id"]),
    )
    db_session.add(tpl)
    await db_session.commit()

    r = await auth_client.post("/api/tasks", json={"title": "429 mid-run", "priority": "low"})
    tid = r.json()["id"]
    task = await db_session.get(Task, _uuid.UUID(tid))
    task.status = TaskStatus.FAILED.value
    task.failure_type = FAILURE_LLM_RATE_LIMIT
    task.template_id = tpl.id          # chosen normally, not substituted
    task.condition_contaminated = False
    await db_session.commit()

    body = (await auth_client.patch(f"/api/tasks/{tid}", json={"status": "ready"})).json()
    assert body["failure_type"] is None, (
        "the quota hit the attempt, not the condition — it must not outlive it"
    )
    assert body["condition_contaminated"] is False
