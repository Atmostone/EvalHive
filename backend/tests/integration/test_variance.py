"""Integration tests for the Variance / Robustness Harness + re-run core (E-11).

Covers the re-run primitive (clone_task_for_rerun), the engine pinned-template
fast path, the variance service (child creation, spec mode, cost cap) and the
API endpoints. The orchestrator loop is never running here, so children are
created READY but not actually spawned — exactly what we assert.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import database
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.variance_run import VarianceRun
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.orchestrator import engine
from app.orchestrator.rerun import clone_task_for_rerun
from app.quality import variance as var


def _template(model_id) -> Template:
    return Template(
        name="solo", description="d", soul_md="# soul", model_id=model_id,
        tool_ids=[], max_ram="1g", max_cpu=100000,
        timeout_minutes=60, tags=[], workspace_id=DEFAULT_WORKSPACE_ID,
    )


@pytest.mark.asyncio
async def test_clone_task_for_rerun_copies_input_and_pins_template(db_session, default_model):
    tpl = _template(default_model.id)
    db_session.add(tpl)
    await db_session.flush()
    source = Task(
        title="Build report", description="do it", priority="high",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
        template_id=tpl.id, reference_answer="gold", max_retries=2,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    clone = await clone_task_for_rerun(db_session, source, title_suffix=" [r]")

    assert clone.id != source.id
    assert clone.replay_of_task_id == source.id
    assert clone.title == "Build report [r]"
    assert clone.description == "do it"
    assert clone.reference_answer == "gold"
    assert clone.template_id == tpl.id  # pinned to the source's template
    assert clone.run_config == {"template_id": str(tpl.id)}
    assert clone.status == TaskStatus.READY.value
    assert clone.max_retries == 0  # a re-run is a single deliberate execution


@pytest.mark.asyncio
async def test_pinned_template_skips_decomposition_and_selection(
    db_session, default_model, monkeypatch
):
    tpl = _template(default_model.id)
    db_session.add(tpl)
    await db_session.flush()
    # A task that already carries a pinned template (as a re-run child would).
    task = Task(
        title="t", description="d", status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID, template_id=tpl.id,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    select_mock = AsyncMock()
    decompose_mock = AsyncMock()
    monkeypatch.setattr(engine, "select_template_for_task", select_mock)
    monkeypatch.setattr(engine, "decide_decomposition", decompose_mock)
    fake_runtime = MagicMock()
    fake_runtime.spawn.return_value = "ctr-pinned-000000000000"
    monkeypatch.setattr(engine, "get_agent_runtime", lambda: fake_runtime)
    monkeypatch.setattr(engine, "issue_agent_token", AsyncMock(return_value="tok"))

    await engine.process_ready_task(db_session, task)
    await db_session.refresh(task)

    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.agent_container_id == "ctr-pinned-000000000000"
    fake_runtime.spawn.assert_called_once()
    select_mock.assert_not_called()
    decompose_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_variance_creates_children_from_task(db_session, default_model):
    tpl = _template(default_model.id)
    db_session.add(tpl)
    await db_session.flush()
    source = Task(
        title="scenario", description="d", status=TaskStatus.DONE.value,
        workspace_id=DEFAULT_WORKSPACE_ID, template_id=tpl.id,
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)

    run = await var.run_variance(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID,
        source_task_id=source.id, n=3, parallel=True,
    )

    assert run.status == var.STATUS_RUNNING
    assert len(run.child_task_ids) == 3  # default max_concurrent_agents
    children = (
        await db_session.execute(
            select(Task).where(Task.id.in_([uuid.UUID(x) for x in run.child_task_ids]))
        )
    ).scalars().all()
    assert all(c.status == TaskStatus.READY.value for c in children)
    assert all(c.replay_of_task_id == source.id for c in children)
    assert all(c.template_id == tpl.id for c in children)


@pytest.mark.asyncio
async def test_run_variance_spec_mode(db_session, default_model):
    run = await var.run_variance(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID,
        source_spec={"title": "Fresh scenario", "description": "x"},
        n=2, parallel=True,
    )
    assert len(run.child_task_ids) == 2
    children = (
        await db_session.execute(
            select(Task).where(Task.id.in_([uuid.UUID(x) for x in run.child_task_ids]))
        )
    ).scalars().all()
    assert all(c.replay_of_task_id is None for c in children)
    assert all("Fresh scenario" in c.title for c in children)


@pytest.mark.asyncio
async def test_cost_cap_finalizes_run_as_capped(db_session, monkeypatch):
    # Two finished children whose cost already exceeds the cap -> no more spawns.
    c1 = Task(title="r1", status=TaskStatus.DONE.value,
              workspace_id=DEFAULT_WORKSPACE_ID, cost_usd=Decimal("1.0"))
    c2 = Task(title="r2", status=TaskStatus.DONE.value,
              workspace_id=DEFAULT_WORKSPACE_ID, cost_usd=Decimal("1.0"))
    db_session.add_all([c1, c2])
    await db_session.commit()
    await db_session.refresh(c1)
    await db_session.refresh(c2)

    run = VarianceRun(
        workspace_id=DEFAULT_WORKSPACE_ID, n=5, parallel=True,
        cost_cap_usd=Decimal("0.5"), status=var.STATUS_RUNNING,
        child_task_ids=[str(c1.id), str(c2.id)],
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    # Keep it hermetic: no record-build / judge calls.
    monkeypatch.setattr("app.quality.data_lake.build_quality_record", AsyncMock(return_value=None))

    await var.advance_variance_run(db_session, run)
    await db_session.refresh(run)

    assert run.status == var.STATUS_CAPPED
    assert run.aggregate is not None
    assert run.aggregate["capped"] is True
    assert run.aggregate["n_executed"] == 2
    assert run.aggregate["success_rate"] == 1.0
    assert len(run.child_task_ids) == 2  # never created the remaining 3


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
async def _seed_done_task(ws) -> str:
    async with database.async_session() as s:
        tpl = _template(None)
        s.add(tpl)
        await s.flush()
        t = Task(title="api scenario", status=TaskStatus.DONE.value,
                 workspace_id=ws, template_id=tpl.id)
        s.add(t)
        await s.commit()
        return str(t.id)


async def test_api_create_and_get_variance_run(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_done_task(ws)

    r = await auth_client.post(
        "/api/quality/variance", json={"source_task_id": tid, "n": 3}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    run_id = body["id"]
    assert body["status"] in ("pending", "running")
    assert body["n"] == 3
    assert len(body["child_task_ids"]) >= 1

    r = await auth_client.get(f"/api/quality/variance/{run_id}")
    assert r.status_code == 200
    assert "children" in r.json()

    r = await auth_client.get(f"/api/quality/variance?source_task_id={tid}")
    assert r.status_code == 200
    assert any(run["id"] == run_id for run in r.json())


async def test_api_create_validation_requires_one_source(auth_client: AsyncClient):
    # Neither source nor spec.
    r = await auth_client.post("/api/quality/variance", json={"n": 3})
    assert r.status_code == 422
    # Both.
    r = await auth_client.post(
        "/api/quality/variance",
        json={"n": 3, "source_task_id": str(uuid.uuid4()), "spec": {"title": "x"}},
    )
    assert r.status_code == 422


async def test_api_get_unknown_and_cross_workspace_404(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_done_task(ws)
    r = await auth_client.post("/api/quality/variance", json={"source_task_id": tid, "n": 2})
    run_id = r.json()["id"]

    # Unknown id.
    r = await auth_client.get(f"/api/quality/variance/{uuid.uuid4()}")
    assert r.status_code == 404

    # Register a second user (fresh workspace) and try to read the first run.
    email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    reg = await auth_client.post(
        "/api/auth/register",
        json={"email": email, "password": "password1234", "display_name": "Other"},
    )
    tok = reg.json()
    auth_client.headers["Authorization"] = f"Bearer {tok['access_token']}"
    auth_client.headers["X-Workspace-Id"] = tok["default_workspace_id"]
    r = await auth_client.get(f"/api/quality/variance/{run_id}")
    assert r.status_code == 404
