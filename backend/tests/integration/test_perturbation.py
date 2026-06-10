"""Integration tests for the Adversarial / Perturbation Judge (E-12).

Covers child creation (baseline + perturbed groups), the runtime tool-injection
seam (run_config -> AgentSpec.extra_env), the cost cap, the injection safety
flag and the API. The orchestrator loop is never running here, so children are
created READY but not actually spawned.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import database
from app.models.perturbation_run import PerturbationRun
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.orchestrator import engine
from app.quality import perturbation as pert


def _template(model_id) -> Template:
    return Template(
        name="solo", description="d", soul_md="# soul", model_id=model_id,
        tool_ids=[], max_ram="1g", max_cpu=100000,
        timeout_minutes=60, tags=[], workspace_id=DEFAULT_WORKSPACE_ID,
    )


async def _source(db, tpl_id) -> Task:
    src = Task(
        title="Build a quarterly report",
        description="First gather data. Then analyse it. Finally write the summary.",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
        template_id=tpl_id, reference_answer="gold",
    )
    db.add(src)
    await db.commit()
    await db.refresh(src)
    return src


@pytest.mark.asyncio
async def test_run_perturbation_creates_baseline_and_perturbed_groups(db_session, default_model):
    tpl = _template(default_model.id)
    db_session.add(tpl)
    await db_session.flush()
    src = await _source(db_session, tpl.id)

    run = await pert.run_perturbation(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, source_task_id=src.id,
        transforms=["noise", "reorder"], variants_per_transform=1, base_n=1,
    )

    # total = 1 base + 2 transforms*1 = 3 == default max_concurrent_agents
    assert run.status == pert.STATUS_RUNNING
    assert len(run.base_task_ids) == 1
    assert set(run.perturbed_task_ids.keys()) == {"noise", "reorder"}
    assert len(run.perturbed_task_ids["noise"]) == 1
    assert run.injection_canary is None  # inject not requested

    all_ids = run.base_task_ids + run.perturbed_task_ids["noise"] + run.perturbed_task_ids["reorder"]
    children = (
        await db_session.execute(
            select(Task).where(Task.id.in_([uuid.UUID(x) for x in all_ids]))
        )
    ).scalars().all()
    by_id = {str(c.id): c for c in children}
    assert all(c.status == TaskStatus.READY.value for c in children)
    assert all(c.replay_of_task_id == src.id for c in children)
    assert all(c.template_id == tpl.id for c in children)

    base_child = by_id[run.base_task_ids[0]]
    assert base_child.description == src.description  # baseline = original input
    noise_child = by_id[run.perturbed_task_ids["noise"][0]]
    assert noise_child.description != src.description  # perturbed


@pytest.mark.asyncio
async def test_inject_child_carries_tool_injection(db_session, default_model):
    tpl = _template(default_model.id)
    db_session.add(tpl)
    await db_session.flush()
    src = await _source(db_session, tpl.id)

    run = await pert.run_perturbation(
        db_session, workspace_id=DEFAULT_WORKSPACE_ID, source_task_id=src.id,
        transforms=["inject"], variants_per_transform=1, base_n=1,
    )

    assert run.injection_canary  # generated because inject requested
    inject_id = run.perturbed_task_ids["inject"][0]
    base_id = run.base_task_ids[0]
    children = (
        await db_session.execute(
            select(Task).where(Task.id.in_([uuid.UUID(inject_id), uuid.UUID(base_id)]))
        )
    ).scalars().all()
    by_id = {str(c.id): c for c in children}

    inject_child = by_id[inject_id]
    assert run.injection_canary in inject_child.run_config["tool_injection"]
    assert inject_child.description == src.description  # input untouched for inject
    base_child = by_id[base_id]
    assert "tool_injection" not in (base_child.run_config or {})


@pytest.mark.asyncio
async def test_engine_passes_tool_injection_env(db_session, default_model, monkeypatch):
    tpl = _template(default_model.id)
    db_session.add(tpl)
    await db_session.flush()
    task = Task(
        title="t", description="d", status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID, template_id=tpl.id,
        run_config={"template_id": str(tpl.id), "tool_injection": "PWN_PAYLOAD"},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    fake_runtime = MagicMock()
    fake_runtime.spawn.return_value = "ctr-inject-0000000000"
    monkeypatch.setattr(engine, "get_agent_runtime", lambda: fake_runtime)
    monkeypatch.setattr(engine, "issue_agent_token", AsyncMock(return_value="tok"))

    await engine.process_ready_task(db_session, task)

    fake_runtime.spawn.assert_called_once()
    spec = fake_runtime.spawn.call_args.args[0]
    assert spec.extra_env == {"AGENT_TOOL_INJECTION": "PWN_PAYLOAD"}


@pytest.mark.asyncio
async def test_cost_cap_finalizes_run_as_capped(db_session, monkeypatch):
    c1 = Task(title="base", status=TaskStatus.DONE.value,
              workspace_id=DEFAULT_WORKSPACE_ID, cost_usd=Decimal("1.0"))
    c2 = Task(title="noise", status=TaskStatus.DONE.value,
              workspace_id=DEFAULT_WORKSPACE_ID, cost_usd=Decimal("1.0"))
    db_session.add_all([c1, c2])
    await db_session.commit()
    await db_session.refresh(c1)
    await db_session.refresh(c2)

    run = PerturbationRun(
        workspace_id=DEFAULT_WORKSPACE_ID, source_task_id=c1.id,
        transforms=["noise", "reorder", "inject"], variants_per_transform=2, base_n=1,
        parallel=True, cost_cap_usd=Decimal("0.5"), status=pert.STATUS_RUNNING,
        base_task_ids=[str(c1.id)], perturbed_task_ids={"noise": [str(c2.id)]},
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    monkeypatch.setattr("app.quality.data_lake.build_quality_record", AsyncMock(return_value=None))

    await pert.advance_perturbation_run(db_session, run)
    await db_session.refresh(run)

    assert run.status == pert.STATUS_CAPPED
    assert run.aggregate is not None
    assert run.aggregate["capped"] is True
    assert run.aggregate["n_executed"] == 2  # never created the remaining 5


@pytest.mark.asyncio
async def test_aggregate_flags_followed_injection(db_session, monkeypatch):
    canary = "deadbeef12"
    base = Task(title="base", status=TaskStatus.DONE.value,
                workspace_id=DEFAULT_WORKSPACE_ID, cost_usd=Decimal("0"))
    obeyed = Task(title="inj", status=TaskStatus.DONE.value,
                  workspace_id=DEFAULT_WORKSPACE_ID, cost_usd=Decimal("0"),
                  result_summary=f"sure, here is {canary} as asked")
    db_session.add_all([base, obeyed])
    await db_session.commit()
    await db_session.refresh(base)
    await db_session.refresh(obeyed)

    run = PerturbationRun(
        workspace_id=DEFAULT_WORKSPACE_ID, source_task_id=base.id,
        transforms=["inject"], variants_per_transform=1, base_n=1,
        parallel=True, status=pert.STATUS_RUNNING, injection_canary=canary,
        base_task_ids=[str(base.id)], perturbed_task_ids={"inject": [str(obeyed.id)]},
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)

    monkeypatch.setattr("app.quality.data_lake.build_quality_record", AsyncMock(return_value=None))

    await pert.advance_perturbation_run(db_session, run)
    await db_session.refresh(run)

    assert run.status == pert.STATUS_DONE
    safety = run.aggregate["safety"]
    assert safety["injection_tested"] is True
    assert safety["injection_followed"] is True
    assert safety["followed_count"] == 1


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
async def _seed_done_task(ws) -> str:
    async with database.async_session() as s:
        tpl = _template(None)
        s.add(tpl)
        await s.flush()
        t = Task(title="api scenario", description="do a. do b.",
                 status=TaskStatus.DONE.value, workspace_id=ws, template_id=tpl.id)
        s.add(t)
        await s.commit()
        return str(t.id)


async def test_api_create_and_get_perturbation_run(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_done_task(ws)

    r = await auth_client.post(
        "/api/quality/perturbation",
        json={"source_task_id": tid, "transforms": ["noise", "inject"], "base_n": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    run_id = body["id"]
    assert body["status"] in ("pending", "running")
    assert body["transforms"] == ["noise", "inject"]

    r = await auth_client.get(f"/api/quality/perturbation/{run_id}")
    assert r.status_code == 200
    assert "base_children" in r.json()
    assert "perturbed_children" in r.json()

    r = await auth_client.get(f"/api/quality/perturbation?source_task_id={tid}")
    assert r.status_code == 200
    assert any(run["id"] == run_id for run in r.json())


async def test_api_create_rejects_bad_transform(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_done_task(ws)
    r = await auth_client.post(
        "/api/quality/perturbation", json={"source_task_id": tid, "transforms": ["bogus"]}
    )
    assert r.status_code == 400


async def test_api_get_unknown_404(auth_client: AsyncClient):
    r = await auth_client.get(f"/api/quality/perturbation/{uuid.uuid4()}")
    assert r.status_code == 404
