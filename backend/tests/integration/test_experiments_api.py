"""Experiments API (SPA-40): CRUD, lifecycle endpoints, report, results,
clone, export, role/workspace enforcement."""

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.experiment import ExperimentRun, ExperimentRunStatus
from app.models.task import Task, TaskStatus
from app.models.template import Template


async def _template(db_session, workspace_id, name="Bench API"):
    t = Template(
        name=name,
        description="bench template",
        soul_md="# soul",
        tool_ids=[],
        tags=[],
        workspace_id=workspace_id,
    )
    db_session.add(t)
    await db_session.commit()
    return t


def _body(template_id, **overrides):
    body = {
        "name": overrides.pop("name", f"api-exp-{uuid.uuid4().hex[:6]}"),
        "dataset": {
            "source": "upload",
            "cases": [
                {"task_input": {"title": "Case A"}},
                {"task_input": {"title": "Case B"}, "case_id": "case-b"},
            ],
        },
        "configurations": [
            {"template_id": str(template_id), "label": "baseline"},
            {"template_id": str(template_id), "soul_md": "v2 prompt", "label": "v2"},
        ],
        "n_runs_per_cell": 1,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_create_list_get_delete(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)

    r = await auth_client.post("/api/experiments", json=_body(tpl.id, name="crud-exp"))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "draft"
    assert created["n_configs"] == 2
    assert created["n_cases"] == 2
    assert created["total_runs"] == 4
    assert created["preview"]["total_runs"] == 4
    assert [c["config_key"] for c in created["configurations"]] == ["cfg-01", "cfg-02"]

    r = await auth_client.get("/api/experiments")
    assert any(e["name"] == "crud-exp" for e in r.json())

    r = await auth_client.get(f"/api/experiments/{created['id']}")
    assert r.status_code == 200
    assert r.json()["matrix"] == []  # draft: no run rows yet

    # Duplicate name → 409.
    r = await auth_client.post("/api/experiments", json=_body(tpl.id, name="crud-exp"))
    assert r.status_code == 409

    # Invalid config → 400 with a clear message.
    bad = _body(tpl.id, name="bad-exp")
    bad["configurations"] = [{"model_id": "no-template"}]
    r = await auth_client.post("/api/experiments", json=bad)
    assert r.status_code == 400
    assert "requires template_id" in r.json()["detail"]

    r = await auth_client.delete(f"/api/experiments/{created['id']}")
    assert r.status_code == 204
    r = await auth_client.get(f"/api/experiments/{created['id']}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_preview_endpoint(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    r = await auth_client.post(
        "/api/experiments/preview", json=_body(tpl.id, n_runs_per_cell=5)
    )
    assert r.status_code == 200
    preview = r.json()
    assert preview["total_runs"] == 20
    assert preview["est_cost_usd"] > 0
    assert isinstance(preview["warnings"], list)


@pytest.mark.asyncio
async def test_run_lifecycle_and_progress_matrix(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    created = (
        await auth_client.post("/api/experiments", json=_body(tpl.id))
    ).json()
    exp_id = created["id"]

    r = await auth_client.post(f"/api/experiments/{exp_id}/run")
    assert r.status_code == 202
    assert r.json()["status"] == "running"

    # /run claimed the first batch immediately.
    r = await auth_client.get(f"/api/experiments/{exp_id}")
    detail = r.json()
    assert detail["run_totals"].get("running", 0) > 0
    assert len(detail["matrix"]) == 4  # 2 configs × 2 cases

    # Double-run → 409.
    r = await auth_client.post(f"/api/experiments/{exp_id}/run")
    assert r.status_code == 409

    r = await auth_client.post(f"/api/experiments/{exp_id}/pause")
    assert r.status_code == 202 and r.json()["status"] == "paused"
    r = await auth_client.post(f"/api/experiments/{exp_id}/resume")
    assert r.status_code == 202 and r.json()["status"] == "running"
    r = await auth_client.post(f"/api/experiments/{exp_id}/cancel")
    assert r.status_code == 202 and r.json()["status"] == "cancelled"
    # Cancel is terminal → further transitions conflict.
    r = await auth_client.post(f"/api/experiments/{exp_id}/cancel")
    assert r.status_code == 409


async def _drain(auth_client, db_session, exp_id):
    """Flip running children DONE and tick until the experiment is terminal."""
    from app.models.experiment import Experiment
    from app.quality.experiments import advance_experiment

    exp = await db_session.get(Experiment, uuid.UUID(exp_id))
    for _ in range(10):
        rows = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == exp.id,
                    ExperimentRun.status == ExperimentRunStatus.RUNNING.value,
                )
            )
        ).scalars().all()
        for r in rows:
            task = await db_session.get(Task, r.task_id)
            if task and task.status not in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
                task.status = TaskStatus.DONE.value
                task.cost_usd = Decimal("0.01")
                task.result_summary = "done"
        await db_session.commit()
        await advance_experiment(db_session, exp)
        await db_session.refresh(exp)
        if exp.status != "running":
            break
    return exp


@pytest.mark.asyncio
async def test_report_results_export(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    created = (
        await auth_client.post("/api/experiments", json=_body(tpl.id))
    ).json()
    exp_id = created["id"]
    await auth_client.post(f"/api/experiments/{exp_id}/run")
    exp = await _drain(auth_client, db_session, exp_id)
    assert exp.status == "completed"

    r = await auth_client.get(f"/api/experiments/{exp_id}/report")
    assert r.status_code == 200
    report = r.json()
    assert report["partial"] is False
    assert report["summary"]["success"] == 4
    assert {row["config_key"] for row in report["heatmap"]["rows"]} == {"cfg-01", "cfg-02"}
    assert "pareto" in report and "leaderboard" in report
    assert "orchestrator" in report

    # Cached now; second read returns the same generated_at.
    r2 = await auth_client.get(f"/api/experiments/{exp_id}/report")
    assert r2.json()["generated_at"] == report["generated_at"]
    # Elo variant recomputes (different method).
    r3 = await auth_client.get(f"/api/experiments/{exp_id}/report?method=elo")
    assert r3.json()["leaderboard"]["method"] == "elo"

    r = await auth_client.get(
        f"/api/experiments/{exp_id}/results", params={"config": "cfg-01"}
    )
    rows = r.json()
    assert len(rows) == 2
    assert all(row["config_key"] == "cfg-01" for row in rows)
    assert all(row["task_status"] == "done" for row in rows)

    r = await auth_client.get(f"/api/experiments/{exp_id}/export?format=json")
    rows = r.json()
    assert len(rows) == 4
    assert rows[0]["experiment_id"] == exp_id
    assert "weighted_score" in rows[0]

    r = await auth_client.get(f"/api/experiments/{exp_id}/export?format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert len(lines) == 5  # header + 4 runs
    assert lines[0].startswith("experiment_id,experiment_name,config_key")


@pytest.mark.asyncio
async def test_clone_endpoint(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    created = (
        await auth_client.post("/api/experiments", json=_body(tpl.id))
    ).json()

    r = await auth_client.post(
        f"/api/experiments/{created['id']}/clone",
        json={"changes": {"n_runs_per_cell": 2}},
    )
    assert r.status_code == 201, r.text
    clone = r.json()
    assert clone["id"] != created["id"]
    assert clone["status"] == "draft"
    assert clone["n_runs_per_cell"] == 2
    assert clone["n_cases"] == created["n_cases"]


@pytest.mark.asyncio
async def test_workspace_scoping_and_roles(client: AsyncClient, db_session):
    # Owner A creates an experiment; user B in another workspace can't see it.
    ra = await client.post(
        "/api/auth/register",
        json={
            "email": f"a-{uuid.uuid4().hex[:8]}@x.dev",
            "password": "password1234",
            "display_name": "A",
        },
    )
    pa = ra.json()
    headers_a = {
        "Authorization": f"Bearer {pa['access_token']}",
        "X-Workspace-Id": pa["default_workspace_id"],
    }
    tpl = await _template(
        db_session, uuid.UUID(pa["default_workspace_id"]), name=f"T-{uuid.uuid4().hex[:6]}"
    )
    r = await client.post("/api/experiments", json=_body(tpl.id), headers=headers_a)
    assert r.status_code == 201
    exp_id = r.json()["id"]

    rb = await client.post(
        "/api/auth/register",
        json={
            "email": f"b-{uuid.uuid4().hex[:8]}@x.dev",
            "password": "password1234",
            "display_name": "B",
        },
    )
    pb = rb.json()
    headers_b = {
        "Authorization": f"Bearer {pb['access_token']}",
        "X-Workspace-Id": pb["default_workspace_id"],
    }
    r = await client.get(f"/api/experiments/{exp_id}", headers=headers_b)
    assert r.status_code == 404
    r = await client.post(f"/api/experiments/{exp_id}/run", headers=headers_b)
    assert r.status_code in (403, 404)
