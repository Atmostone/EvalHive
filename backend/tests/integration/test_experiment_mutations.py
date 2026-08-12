"""Mutating a finished experiment — retry, add-config, retire-config (SPA-84).

These three had no test coverage at all, which is how they came to share three
defects: no status guards, no record of what they overwrote, and a cached report
left in place afterwards so a settled experiment kept serving its pre-mutation
numbers. Every test below fails against that behaviour.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.experiment import (
    ExperimentAttempt,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.quality.experiments import (
    add_config_to_experiment,
    advance_experiment,
    create_experiment,
    pause_experiment,
    remove_config_from_experiment,
    retry_failed_experiment,
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
            "cases": [{"task_input": {"title": "Case A", "description": "da"}}],
        },
        "configurations": [
            {"template_id": str(template_id), "label": "baseline"},
            {"template_id": str(template_id), "temperature": 0.7, "label": "hot"},
        ],
        "n_runs_per_cell": 1,
        "max_parallel": 4,
    }
    payload.update(overrides)
    return payload


async def _runs(db_session, exp, *, include_retired=True):
    stmt = select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id)
    if not include_retired:
        stmt = stmt.where(ExperimentRun.retired_at.is_(None))
    return (
        (await db_session.execute(stmt.order_by(ExperimentRun.config_key)))
        .scalars()
        .all()
    )


async def _attempts(db_session, run_id):
    return (
        (
            await db_session.execute(
                select(ExperimentAttempt)
                .where(ExperimentAttempt.experiment_run_id == run_id)
                .order_by(ExperimentAttempt.attempt_index)
            )
        )
        .scalars()
        .all()
    )


async def _drain(db_session, exp, *, task_status=TaskStatus.DONE.value):
    """Run the matrix to completion, flipping claimed children to a terminal state."""
    for _ in range(12):
        for r in await _runs(db_session, exp):
            if r.status == ExperimentRunStatus.RUNNING.value and r.task_id:
                task = await db_session.get(Task, r.task_id)
                if task.status not in (TaskStatus.DONE.value, TaskStatus.FAILED.value):
                    task.status = task_status
                    task.cost_usd = Decimal("0.01")
        await db_session.commit()
        await advance_experiment(db_session, exp)
        await db_session.refresh(exp)
        if exp.status != ExperimentStatus.RUNNING.value:
            return


async def _settled_experiment(db_session, workspace_id, *, task_status=TaskStatus.DONE.value):
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await start_experiment(db_session, exp)
    await _drain(db_session, exp, task_status=task_status)
    return exp, tpl


# --- retry ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_preserves_the_superseded_attempt(auth_client, db_session):
    """The whole point: re-running a cell must not erase what it already measured."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)
    assert exp.status == ExperimentStatus.FAILED.value

    failed = [r for r in await _runs(db_session, exp) if r.status == ExperimentRunStatus.FAILED.value]
    assert failed, "fixture should have produced failed cells"
    victim = failed[0]
    old_task_id, old_attempt = victim.task_id, victim.attempt_count
    assert old_attempt == 1

    assert await retry_failed_experiment(db_session, exp) == len(failed)

    kept = await _attempts(db_session, victim.id)
    assert [a.attempt_index for a in kept] == [1]
    assert kept[0].status == ExperimentRunStatus.FAILED.value
    assert kept[0].task_id == old_task_id
    assert kept[0].retired_reason == "retry"

    await db_session.refresh(victim)
    assert victim.status == ExperimentRunStatus.PENDING.value
    assert victim.task_id is None
    assert victim.lane_index is None  # stale pin cleared


@pytest.mark.asyncio
async def test_retry_bumps_revision_and_drops_the_cached_report(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)
    exp.report = {"schema_version": 13, "stale": True}
    await db_session.commit()
    before = exp.revision

    await retry_failed_experiment(db_session, exp)
    await db_session.refresh(exp)

    assert exp.revision == before + 1
    assert exp.report is None
    assert exp.input_fingerprint


@pytest.mark.asyncio
async def test_retry_is_refused_while_the_experiment_is_running(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)
    await db_session.refresh(exp)
    assert exp.status == ExperimentStatus.RUNNING.value

    with pytest.raises(ValueError, match="cannot retry"):
        await retry_failed_experiment(db_session, exp)


@pytest.mark.asyncio
async def test_retry_with_nothing_failed_leaves_the_revision_alone(auth_client, db_session):
    """Idempotence: a no-op must not invalidate a perfectly good cached report."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    assert exp.status == ExperimentStatus.COMPLETED.value
    exp.report = {"schema_version": 13}
    await db_session.commit()
    before = exp.revision

    assert await retry_failed_experiment(db_session, exp) == 0
    await db_session.refresh(exp)
    assert exp.revision == before
    assert exp.report is not None


# --- add config -------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_config_validates_references(auth_client, db_session):
    """create_experiment checked these; the add path used to skip the check."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)

    with pytest.raises(ValueError, match="not found in workspace"):
        await add_config_to_experiment(
            db_session, exp, {"template_id": str(uuid.uuid4()), "label": "ghost"}
        )


@pytest.mark.asyncio
async def test_add_config_bumps_revision_and_drops_the_cached_report(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)
    exp.report = {"schema_version": 13, "stale": True}
    await db_session.commit()
    before = exp.revision

    result = await add_config_to_experiment(
        db_session, exp, {"template_id": str(tpl.id), "temperature": 1.1, "label": "spicy"}
    )
    await db_session.refresh(exp)

    assert result["config_key"] == "cfg-03"
    assert result["runs_created"] == 1  # 1 case × 1 run
    assert exp.revision == before + 1
    assert exp.report is None
    assert exp.status == ExperimentStatus.RUNNING.value


# --- retire config ----------------------------------------------------------


@pytest.mark.asyncio
async def test_retiring_a_config_keeps_its_lineage(auth_client, db_session):
    """This used to hard-delete the rows, which is where orphan populations came from."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    before = exp.revision

    result = await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    assert result["runs_retired"] == 1
    assert exp.revision == before + 1

    all_rows = await _runs(db_session, exp)
    live_rows = await _runs(db_session, exp, include_retired=False)
    assert len(all_rows) == 2, "the retired cell must still exist"
    assert [r.config_key for r in live_rows] == ["cfg-01"]

    retired_row = next(r for r in all_rows if r.config_key == "cfg-02")
    assert retired_row.retired_at is not None
    kept = await _attempts(db_session, retired_row.id)
    assert [a.retired_reason for a in kept] == ["config_retired"]

    entry = next(c for c in exp.configurations if c["config_key"] == "cfg-02")
    assert entry["retired_at"], "the config entry is stamped, not dropped"


@pytest.mark.asyncio
async def test_retired_config_key_is_never_reused(auth_client, db_session):
    """Reusing cfg-02 would merge two different conditions under one key."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)

    await remove_config_from_experiment(db_session, exp, "cfg-02")
    result = await add_config_to_experiment(
        db_session, exp, {"template_id": str(tpl.id), "temperature": 0.3, "label": "next"}
    )
    assert result["config_key"] == "cfg-03"


@pytest.mark.asyncio
async def test_retiring_is_refused_while_running_and_on_the_last_config(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)
    await db_session.refresh(exp)

    with pytest.raises(ValueError, match="pause or cancel"):
        await remove_config_from_experiment(db_session, exp, "cfg-02")

    await pause_experiment(db_session, exp)
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    with pytest.raises(ValueError, match="only configuration"):
        await remove_config_from_experiment(db_session, exp, "cfg-01")
    with pytest.raises(ValueError, match="already retired"):
        await remove_config_from_experiment(db_session, exp, "cfg-02")


@pytest.mark.asyncio
async def test_the_tick_never_claims_a_retired_cell(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await start_experiment(db_session, exp)
    await pause_experiment(db_session, exp)
    await remove_config_from_experiment(db_session, exp, "cfg-02")

    exp.status = ExperimentStatus.RUNNING.value
    await db_session.commit()
    await _drain(db_session, exp)
    await db_session.refresh(exp)

    rows = await _runs(db_session, exp)
    retired = next(r for r in rows if r.config_key == "cfg-02")
    assert retired.status == ExperimentRunStatus.PENDING.value
    assert retired.task_id is None
    assert exp.status == ExperimentStatus.COMPLETED.value, (
        "a retired pending cell must not keep the experiment open forever"
    )
