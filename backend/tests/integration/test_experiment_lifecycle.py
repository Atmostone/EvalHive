"""Experiment Runner lifecycle (SPA-40): start → tick claims → settle →
budget cap / pause / cancel / finalize. The orchestrator loop never runs in
tests — children stay READY and are flipped manually, mirroring the variance
harness test approach."""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.experiment import ExperimentRun, ExperimentRunStatus, ExperimentStatus
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.quality.experiments import (
    advance_experiment,
    cancel_experiment,
    clone_experiment,
    create_experiment,
    estimate_preview,
    pause_experiment,
    resume_experiment,
    start_experiment,
)


async def _template(db_session, workspace_id, name="Bench"):
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


def _payload(template_id, **overrides):
    payload = {
        "name": overrides.pop("name", f"exp-{uuid.uuid4().hex[:6]}"),
        "dataset": {
            "source": "upload",
            "cases": [
                {"task_input": {"title": "Case A", "description": "da"}},
                {"task_input": {"title": "Case B"}, "case_id": "case-b"},
            ],
        },
        "configurations": [
            {"template_id": str(template_id), "label": "baseline"},
            {"template_id": str(template_id), "temperature": 0.7, "label": "hot"},
        ],
        "n_runs_per_cell": 2,
    }
    payload.update(overrides)
    return payload


async def _runs(db_session, exp):
    return (
        (
            await db_session.execute(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_id == exp.id)
                .order_by(
                    ExperimentRun.config_key,
                    ExperimentRun.case_key,
                    ExperimentRun.run_index,
                )
            )
        )
        .scalars()
        .all()
    )


async def _flip_running_tasks(db_session, exp, *, status=TaskStatus.DONE.value,
                              cost=Decimal("0.01")):
    rows = await _runs(db_session, exp)
    flipped = 0
    for r in rows:
        if r.status == ExperimentRunStatus.RUNNING.value and r.task_id:
            task = await db_session.get(Task, r.task_id)
            if task.status not in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
                task.status = status
                task.cost_usd = cost
                flipped += 1
    await db_session.commit()
    return flipped


@pytest.mark.asyncio
async def test_create_validates_refs_and_caps(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    with pytest.raises(ValueError, match="not found in workspace"):
        await create_experiment(
            db_session,
            workspace_id=workspace_id,
            payload=_payload(uuid.uuid4()),
        )

    tpl = await _template(db_session, workspace_id)
    with pytest.raises(ValueError, match="n_runs_per_cell"):
        await create_experiment(
            db_session,
            workspace_id=workspace_id,
            payload=_payload(tpl.id, n_runs_per_cell=99),
        )


@pytest.mark.asyncio
async def test_full_lifecycle_to_completed(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    assert exp.status == ExperimentStatus.DRAFT.value
    assert [c["config_key"] for c in exp.configurations] == ["cfg-01", "cfg-02"]
    assert [c["case_key"] for c in exp.dataset_cases] == ["upload-001", "case-b"]

    await start_experiment(db_session, exp)
    rows = await _runs(db_session, exp)
    assert len(rows) == 8  # 2 configs × 2 cases × 2 runs
    assert all(r.status == ExperimentRunStatus.PENDING.value for r in rows)

    # First tick claims up to max_concurrent_agents (default 3).
    await advance_experiment(db_session, exp)
    rows = await _runs(db_session, exp)
    claimed = [r for r in rows if r.status == ExperimentRunStatus.RUNNING.value]
    assert len(claimed) == 3

    child = await db_session.get(Task, claimed[0].task_id)
    assert child.status == TaskStatus.READY.value
    assert child.origin == "experiment"
    assert child.template_id == tpl.id  # orchestrator:off → pinned fast path
    assert child.max_retries == 0
    assert child.run_config["benchmark_mode"] is True
    assert child.run_config["experiment"]["config_key"] == claimed[0].config_key
    assert child.benchmark_case_id == claimed[0].case_key
    assert child.benchmark_suite == f"exp:{exp.id}"
    assert child.title in ("Case A", "Case B")  # no suffix — input identical across configs

    # Drain the matrix: flip running children DONE, tick, repeat.
    for _ in range(8):
        await _flip_running_tasks(db_session, exp)
        await advance_experiment(db_session, exp)
        await db_session.refresh(exp)
        if exp.status != ExperimentStatus.RUNNING.value:
            break

    rows = await _runs(db_session, exp)
    assert all(r.status == ExperimentRunStatus.SUCCESS.value for r in rows)
    assert all(r.completed_at is not None for r in rows)
    assert exp.status == ExperimentStatus.COMPLETED.value
    assert exp.accumulated_cost_usd == Decimal("0.08")  # 8 × 0.01


@pytest.mark.asyncio
async def test_budget_cap_skips_rest_and_caps(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    exp = await create_experiment(
        db_session,
        workspace_id=workspace_id,
        payload=_payload(tpl.id, budget_limit_usd=0.02),
    )
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)  # claims 3

    await _flip_running_tasks(db_session, exp, cost=Decimal("0.01"))
    await advance_experiment(db_session, exp)  # settle 3 → 0.03 ≥ 0.02 → cap
    await db_session.refresh(exp)

    rows = await _runs(db_session, exp)
    by_status = {}
    for r in rows:
        by_status.setdefault(r.status, []).append(r)
    assert len(by_status.get(ExperimentRunStatus.SUCCESS.value, [])) == 3
    assert len(by_status.get(ExperimentRunStatus.SKIPPED.value, [])) == 5
    assert exp.status == ExperimentStatus.CAPPED.value


@pytest.mark.asyncio
async def test_pause_blocks_claims_resume_continues(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await start_experiment(db_session, exp)
    await pause_experiment(db_session, exp)

    await advance_experiment(db_session, exp)  # no-op while paused
    rows = await _runs(db_session, exp)
    assert all(r.status == ExperimentRunStatus.PENDING.value for r in rows)

    await resume_experiment(db_session, exp)
    await advance_experiment(db_session, exp)
    rows = await _runs(db_session, exp)
    assert any(r.status == ExperimentRunStatus.RUNNING.value for r in rows)


@pytest.mark.asyncio
async def test_cancel_skips_unsettled_keeps_settled(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)  # 3 running
    await _flip_running_tasks(db_session, exp)
    await advance_experiment(db_session, exp)  # settle 3, claim 3 more

    await cancel_experiment(db_session, exp)
    await db_session.refresh(exp)
    rows = await _runs(db_session, exp)
    statuses = [r.status for r in rows]
    assert exp.status == ExperimentStatus.CANCELLED.value
    assert statuses.count(ExperimentRunStatus.SUCCESS.value) == 3
    assert statuses.count(ExperimentRunStatus.SKIPPED.value) == 5
    assert ExperimentRunStatus.RUNNING.value not in statuses
    assert ExperimentRunStatus.PENDING.value not in statuses

    # Tick on a cancelled experiment is a no-op.
    await advance_experiment(db_session, exp)
    await db_session.refresh(exp)
    assert exp.status == ExperimentStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_max_parallel_caps_claims(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    exp = await create_experiment(
        db_session,
        workspace_id=workspace_id,
        payload=_payload(tpl.id, max_parallel=1),
    )
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)
    rows = await _runs(db_session, exp)
    running = [r for r in rows if r.status == ExperimentRunStatus.RUNNING.value]
    assert len(running) == 1


@pytest.mark.asyncio
async def test_preview_estimates_and_warnings(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    preview = await estimate_preview(
        db_session,
        workspace_id=workspace_id,
        payload=_payload(tpl.id, budget_limit_usd=0.0001),
    )
    assert preview["n_configs"] == 2
    assert preview["n_cases"] == 2
    assert preview["total_runs"] == 8
    assert preview["est_cost_usd"] > 0
    assert preview["est_duration_minutes"] > 0
    joined = " ".join(preview["warnings"])
    assert "exceeds budget" in joined
    assert "temperature" in joined  # the 'hot' config uses the temperature axis


@pytest.mark.asyncio
async def test_clone_copies_frozen_dataset_and_applies_changes(
    auth_client, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )

    clone = await clone_experiment(
        db_session, exp, changes={"n_runs_per_cell": 3}
    )
    assert clone.id != exp.id
    assert clone.status == ExperimentStatus.DRAFT.value
    assert clone.name.startswith(exp.name)
    assert clone.n_runs_per_cell == 3
    assert clone.dataset_cases == exp.dataset_cases
    assert [c["fingerprint"] for c in clone.configurations] == [
        c["fingerprint"] for c in exp.configurations
    ]

    with pytest.raises(ValueError, match="unknown clone changes"):
        await clone_experiment(db_session, exp, changes={"bogus": 1})
