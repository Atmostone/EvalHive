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
from app.models.annotation import Annotation
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.orchestrator.engine import _record_run_condition
from app.quality.experiment_report import (
    SELECTION_ALL_ATTEMPTS,
    SELECTION_FIRST_ATTEMPT,
    SELECTION_LATEST_VALID,
    compute_report,
    config_drift,
    select_runs,
)
from app.quality.experiments import (
    add_config_to_experiment,
    advance_experiment,
    clone_experiment,
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


async def _settled_two_cell_experiment(db_session, workspace_id):
    """One config, two runs of the same case — the minimum to show a split."""
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session,
        workspace_id=workspace_id,
        payload=_payload(tpl.id, n_runs_per_cell=2),
    )
    await start_experiment(db_session, exp)
    await _drain(db_session, exp)
    return exp


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
async def test_report_cache_is_rejected_after_a_mutation(auth_client, db_session):
    """The defect that made 'how many runs?' unanswerable: a settled experiment
    served its pre-mutation report, and the cache was formally valid."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)

    first = await compute_report(db_session, exp)
    exp.report = first
    await db_session.commit()
    assert first["input_revision"] == exp.revision
    assert first["input_fingerprint"], "the report records the inputs it was built from"

    await add_config_to_experiment(
        db_session, exp, {"template_id": str(tpl.id), "temperature": 1.3, "label": "new"}
    )
    await db_session.refresh(exp)

    assert exp.report is None, "the mutation itself drops the cache"
    assert first["input_revision"] != exp.revision, (
        "a stale report must no longer match the experiment it claims to describe"
    )


@pytest.mark.asyncio
async def test_selection_policies_see_different_populations(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)
    n_cells = len(await _runs(db_session, exp))

    await retry_failed_experiment(db_session, exp)
    await _drain(db_session, exp)
    await db_session.refresh(exp)

    latest = await select_runs(db_session, exp, selection=SELECTION_LATEST_VALID)
    every = await select_runs(db_session, exp, selection=SELECTION_ALL_ATTEMPTS)
    first = await select_runs(db_session, exp, selection=SELECTION_FIRST_ATTEMPT)

    assert len(latest) == n_cells
    assert len(every) == n_cells * 2, "each cell ran twice"
    assert len(first) == n_cells
    assert all(r.status == ExperimentRunStatus.SUCCESS.value for r in latest)
    assert all(r.status == ExperimentRunStatus.FAILED.value for r in first), (
        "the first attempt is the one that failed"
    )


@pytest.mark.asyncio
async def test_every_read_path_agrees_on_the_population(auth_client, db_session):
    """The original defect in miniature: the report and the other read paths must
    not describe different sets of runs for the same experiment."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    detail = await auth_client.get(f"/api/experiments/{exp.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["n_configs"] == 1
    assert body["n_retired_configs"] == 1
    assert body["revision"] == exp.revision

    live_rows = (await auth_client.get(f"/api/experiments/{exp.id}/results")).json()
    assert {r["config_key"] for r in live_rows} == {"cfg-01"}
    assert len(live_rows) == body["total_runs"]

    export = (await auth_client.get(f"/api/experiments/{exp.id}/export")).json()
    assert {r["config_key"] for r in export} == {"cfg-01"}

    # The retired lineage is kept and stays reachable on request.
    widened = (
        await auth_client.get(
            f"/api/experiments/{exp.id}/results", params={"include_retired": "true"}
        )
    ).json()
    assert {r["config_key"] for r in widened} == {"cfg-01", "cfg-02"}


@pytest.mark.asyncio
async def test_retry_after_retiring_a_config_does_not_collide(auth_client, db_session):
    """Both archiving events can land on the same execution — the ledger, not the
    counter, decides which indices are taken."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)

    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    # cfg-01 is still failed and retryable; cfg-02's cell is retired and archived.
    retried = await retry_failed_experiment(db_session, exp)
    assert retried == 1, "only the live config's cell is retried"

    retired_row = next(r for r in await _runs(db_session, exp) if r.config_key == "cfg-02")
    assert len(await _attempts(db_session, retired_row.id)) == 1
    assert retired_row.status == ExperimentRunStatus.FAILED.value, (
        "a retired cell keeps the state it was frozen in"
    )


@pytest.mark.asyncio
async def test_retiring_after_a_retry_does_not_collide(auth_client, db_session):
    """The mirror case: retry archives attempt 1 and leaves the counter alone, so
    retiring before the cell is re-claimed would archive index 1 a second time."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)

    await retry_failed_experiment(db_session, exp)
    await db_session.refresh(exp)
    exp.status = ExperimentStatus.PAUSED.value
    await db_session.commit()

    result = await remove_config_from_experiment(db_session, exp, "cfg-02")
    assert result["runs_retired"] == 1

    row = next(r for r in await _runs(db_session, exp) if r.config_key == "cfg-02")
    assert [a.attempt_index for a in await _attempts(db_session, row.id)] == [1], (
        "the execution is archived once, not twice"
    )


@pytest.mark.asyncio
async def test_all_attempts_does_not_double_count_a_retired_cell(auth_client, db_session):
    """Retiring archives the cell without clearing it, so the ledger row and the
    live row describe the same execution."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    n_cells = len(await _runs(db_session, exp))

    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    every = await select_runs(db_session, exp, selection=SELECTION_ALL_ATTEMPTS)
    assert len(every) == n_cells, "one row per execution, retired cell included once"


@pytest.mark.asyncio
async def test_report_drops_retired_configs_but_all_attempts_keeps_them(
    auth_client, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    default = await compute_report(db_session, exp)
    assert {c["config_key"] for c in default["summary"]["per_config"]} == {"cfg-01"}

    widened = await compute_report(db_session, exp, selection=SELECTION_ALL_ATTEMPTS)
    assert {c["config_key"] for c in widened["summary"]["per_config"]} == {"cfg-01", "cfg-02"}


@pytest.mark.asyncio
async def test_http_report_cache_is_served_then_invalidated(auth_client, db_session):
    """Covers the endpoint's cache branch itself, not just compute_report."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)

    first = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    second = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert first["generated_at"] == second["generated_at"], "second call served from cache"
    assert first["input_revision"] == exp.revision

    await add_config_to_experiment(
        db_session, exp, {"template_id": str(tpl.id), "temperature": 1.7, "label": "x"}
    )
    await _drain(db_session, exp)
    await db_session.refresh(exp)

    third = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert third["generated_at"] != first["generated_at"], "recomputed after the mutation"
    assert third["input_revision"] == exp.revision
    assert {c["config_key"] for c in third["summary"]["per_config"]} == {"cfg-01", "cfg-02", "cfg-03"}


@pytest.mark.asyncio
async def test_a_config_retired_while_draft_never_materializes_cells(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session, workspace_id=workspace_id, payload=_payload(tpl.id)
    )
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    await start_experiment(db_session, exp)
    assert {r.config_key for r in await _runs(db_session, exp)} == {"cfg-01"}


@pytest.mark.asyncio
async def test_clone_does_not_resurrect_a_retired_config(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    clone = await clone_experiment(db_session, exp, name=f"clone-{uuid.uuid4().hex[:6]}")
    assert len(clone.configurations) == 1
    assert not any(c.get("retired_at") for c in clone.configurations)


@pytest.mark.asyncio
async def test_superseded_tasks_leave_the_experiments_suite_tag(auth_client, db_session):
    """A retried cell's old task used to keep the plain exp:<id> tag, which is how
    an experiment came to have three times more tagged tasks than run rows."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)

    victim = next(
        r for r in await _runs(db_session, exp) if r.status == ExperimentRunStatus.FAILED.value
    )
    old_task_id = victim.task_id
    assert (await db_session.get(Task, old_task_id)).benchmark_suite == f"exp:{exp.id}"

    await retry_failed_experiment(db_session, exp)

    old_task = await db_session.get(Task, old_task_id)
    assert old_task.benchmark_suite == f"exp:{exp.id}:retired"

    live = (
        await db_session.execute(
            select(Task).where(Task.benchmark_suite == f"exp:{exp.id}")
        )
    ).scalars().all()
    assert old_task_id not in {t.id for t in live}


@pytest.mark.asyncio
async def test_retiring_a_config_moves_its_tasks_out_of_the_suite(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    retired_task_ids = {
        r.task_id for r in await _runs(db_session, exp) if r.config_key == "cfg-02" and r.task_id
    }
    assert retired_task_ids

    await remove_config_from_experiment(db_session, exp, "cfg-02")

    for tid in retired_task_ids:
        assert (await db_session.get(Task, tid)).benchmark_suite == f"exp:{exp.id}:retired"

    still_live = (
        await db_session.execute(
            select(Task).where(Task.benchmark_suite == f"exp:{exp.id}")
        )
    ).scalars().all()
    assert not (retired_task_ids & {t.id for t in still_live})


@pytest.mark.asyncio
async def test_retagging_is_idempotent(auth_client, db_session):
    """Retiring a config whose cell was already retried must not double-suffix."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)
    row = next(r for r in await _runs(db_session, exp) if r.config_key == "cfg-02")
    task_id = row.task_id

    await retry_failed_experiment(db_session, exp)
    await db_session.refresh(exp)
    exp.status = ExperimentStatus.PAUSED.value
    await db_session.commit()
    await remove_config_from_experiment(db_session, exp, "cfg-02")

    assert (await db_session.get(Task, task_id)).benchmark_suite == f"exp:{exp.id}:retired"


@pytest.mark.asyncio
async def test_start_freezes_what_each_config_resolves_to(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)

    for cfg in exp.configurations:
        resolved = cfg.get("resolved")
        assert resolved, f"{cfg['config_key']} has no frozen resolution"
        assert resolved["template_name"] == tpl.name
        assert resolved["template_content_sha256"]
        assert resolved["resolved_at"]


@pytest.mark.asyncio
async def test_editing_the_template_mid_experiment_shows_up_as_drift(auth_client, db_session):
    """The confounder that actually happened here: a template's contents changed
    while an experiment was running, invisibly, at an unchanged fingerprint."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)

    assert await config_drift(db_session, exp) == []

    tpl.soul_md = "# soul, but edited mid-flight"
    await db_session.commit()

    drift = await config_drift(db_session, exp)
    assert {d["config_key"] for d in drift} == {"cfg-01", "cfg-02"}
    assert "template_content_sha256" in drift[0]["changed"]
    entry = drift[0]["changed"]["template_content_sha256"]
    assert entry["pinned"] != entry["current"]


@pytest.mark.asyncio
async def test_a_retired_config_is_not_reported_as_drifted(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    tpl.soul_md = "# edited after the retirement"
    await db_session.commit()

    assert {d["config_key"] for d in await config_drift(db_session, exp)} == {"cfg-01"}


@pytest.mark.asyncio
async def test_drift_is_recomputed_even_when_the_report_is_cached(auth_client, db_session):
    """Drift watches inputs no fingerprint can see, so freezing it into the cache
    would report the state at cache time forever — the silence the pin exists to break."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, tpl = await _settled_experiment(db_session, workspace_id)

    first = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert first["config_drift"] == []

    tpl.soul_md = "# edited long after the report was cached"
    await db_session.commit()

    second = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert second["generated_at"] == first["generated_at"], "still the cached report"
    assert {d["config_key"] for d in second["config_drift"]} == {"cfg-01", "cfg-02"}


@pytest.mark.asyncio
async def test_an_execution_survives_retry_then_retire_in_all_attempts(auth_client, db_session):
    """A retry resets the cell without advancing the counter, so after a later
    retirement the ledger row is the only copy of that execution."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)

    await retry_failed_experiment(db_session, exp)
    await db_session.refresh(exp)
    exp.status = ExperimentStatus.PAUSED.value
    await db_session.commit()
    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    every = await select_runs(db_session, exp, selection=SELECTION_ALL_ATTEMPTS)
    retired_rows = [r for r in every if r.config_key == "cfg-02"]
    assert any(r.status == ExperimentRunStatus.FAILED.value for r in retired_rows), (
        "the execution that actually ran must still be reachable"
    )


@pytest.mark.asyncio
async def test_retiring_recomputes_cost_and_status(auth_client, db_session):
    """Every other view moved to the live population; the experiment's own totals
    are rolled up by the tick, which never runs again once settled."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    assert exp.accumulated_cost_usd == Decimal("0.02")  # 2 cells × 0.01

    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    assert exp.accumulated_cost_usd == Decimal("0.01"), (
        "the retired config's spend must leave the total with its runs"
    )
    assert exp.status == ExperimentStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_retiring_the_only_failing_config_clears_the_failure(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)
    assert exp.status == ExperimentStatus.FAILED.value

    # Make cfg-01 a success so only cfg-02 keeps the experiment failed.
    row = next(r for r in await _runs(db_session, exp) if r.config_key == "cfg-01")
    row.status = ExperimentRunStatus.SUCCESS.value
    await db_session.commit()

    await remove_config_from_experiment(db_session, exp, "cfg-02")
    await db_session.refresh(exp)

    assert exp.status == ExperimentStatus.COMPLETED.value
    assert exp.error is None


@pytest.mark.asyncio
async def test_spawn_stamps_the_condition_onto_the_right_cell(auth_client, db_session):
    """The engine writes it back by the cell key, at spawn — the only moment the
    template, model and tool set are authoritative."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    target = (await _runs(db_session, exp))[0]
    task = await db_session.get(Task, target.task_id)

    await _record_run_condition(db_session, task, "cond-abc")
    await db_session.commit()
    await db_session.refresh(target)

    assert target.condition_fingerprint == "cond-abc"
    others = [r for r in await _runs(db_session, exp) if r.id != target.id]
    assert all(r.condition_fingerprint is None for r in others), "only its own cell"


@pytest.mark.asyncio
async def test_a_non_experiment_task_is_ignored(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    plain = Task(title="plain", status=TaskStatus.READY.value, workspace_id=workspace_id)
    db_session.add(plain)
    await db_session.commit()
    await _record_run_condition(db_session, plain, "cond-xyz")  # must not raise


@pytest.mark.asyncio
async def test_cells_that_ran_under_different_conditions_are_reported(auth_client, db_session):
    """Two spawns of one config under different conditions — the pin cannot see
    this, and neither could a claim-time record."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp = await _settled_two_cell_experiment(db_session, workspace_id)
    rows = [r for r in await _runs(db_session, exp) if r.config_key == "cfg-01"]
    assert len(rows) == 2
    rows[0].condition_fingerprint = "cond-before"
    rows[1].condition_fingerprint = "cond-after"
    rows[0].core_condition_fingerprint = rows[1].core_condition_fingerprint = "core-same"
    await db_session.commit()

    split = [d for d in await config_drift(db_session, exp) if d.get("split_cases")]
    assert [d["config_key"] for d in split] == ["cfg-01"]
    assert split[0]["split_cases"] == {"upload-001": ["cond-after", "cond-before"]}
    assert split[0]["changed"] == {}, "the pin still matches — only the runs disagree"


@pytest.mark.asyncio
async def test_an_edit_between_two_cases_is_caught(auth_client, db_session):
    """The hole a per-case comparison leaves: with the default one run per cell
    each case has exactly one run, so a template edited between case A and case B
    — and reverted before the report — has nothing to disagree with locally."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session,
        workspace_id=workspace_id,
        payload=_payload(
            tpl.id,
            dataset={
                "source": "upload",
                "cases": [
                    {"task_input": {"title": "Case A"}, "case_id": "case-a"},
                    {"task_input": {"title": "Case B"}, "case_id": "case-b"},
                ],
            },
        ),
    )
    await start_experiment(db_session, exp)
    await _drain(db_session, exp)

    for r in await _runs(db_session, exp):
        if r.config_key != "cfg-01":
            continue
        # The tool set differs by case (legitimate); the core differs because the
        # template was edited between the two cases.
        r.condition_fingerprint = f"full-{r.case_key}"
        r.core_condition_fingerprint = "core-before" if r.case_key == "case-a" else "core-after"
    await db_session.commit()

    drift = await config_drift(db_session, exp)
    flagged = [d for d in drift if d.get("core_conditions")]
    assert [d["config_key"] for d in flagged] == ["cfg-01"]
    assert flagged[0]["core_conditions"] == ["core-after", "core-before"]
    assert "split_cases" not in flagged[0], "no case disagrees with itself"


@pytest.mark.asyncio
async def test_different_cases_are_not_a_split(auth_client, db_session):
    """The Toolathlon shape: the resolved MCP set comes from the CASE, so one
    unchanged configuration legitimately differs across cases. Comparing those
    would flag every such experiment."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"T-{uuid.uuid4().hex[:6]}")
    exp = await create_experiment(
        db_session,
        workspace_id=workspace_id,
        payload=_payload(
            tpl.id,
            dataset={
                "source": "upload",
                "cases": [
                    {"task_input": {"title": "Case A"}, "case_id": "case-a"},
                    {"task_input": {"title": "Case B"}, "case_id": "case-b"},
                ],
            },
        ),
    )
    await start_experiment(db_session, exp)
    await _drain(db_session, exp)

    for r in await _runs(db_session, exp):
        # Same config, different case → different tool set → different full hash,
        # but the case-independent core is identical: nothing actually changed.
        r.condition_fingerprint = f"full-{r.case_key}"
        r.core_condition_fingerprint = "core-same"
    await db_session.commit()

    drift = await config_drift(db_session, exp)
    assert [d for d in drift if d.get("split_cases")] == []
    assert [d for d in drift if d.get("core_conditions")] == []


@pytest.mark.asyncio
async def test_a_condition_change_across_retries_survives_in_the_ledger(auth_client, db_session):
    """The earlier attempt is the only evidence once the cell has been reset."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)
    row = next(r for r in await _runs(db_session, exp) if r.config_key == "cfg-01")
    row.condition_fingerprint = "cond-first"
    await db_session.commit()

    await retry_failed_experiment(db_session, exp)
    kept = await _attempts(db_session, row.id)
    assert [a.condition_fingerprint for a in kept] == ["cond-first"]

    # The cell is re-claimed and spawns under a changed condition.
    await db_session.refresh(row)
    row.condition_fingerprint = "cond-second"
    await db_session.commit()

    split = [d for d in await config_drift(db_session, exp) if d.get("split_cases")]
    assert split and split[0]["config_key"] == "cfg-01"
    assert split[0]["split_cases"] == {"upload-001": ["cond-first", "cond-second"]}


@pytest.mark.asyncio
async def test_unknown_selection_policy_is_rejected(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)
    with pytest.raises(ValueError, match="unknown selection policy"):
        await select_runs(db_session, exp, selection="whatever")


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


@pytest.mark.asyncio
async def test_a_human_rating_invalidates_the_cached_report(auth_client, db_session):
    """SPA-88: the calibration is an input no experiment mutation touches.

    A person rates a run; which axes the report may draw a conclusion from changes;
    the experiment does not, so neither the revision nor the input fingerprint can
    see it. While calibration only drove a badge that was cosmetic. Now it decides
    which axes carry a number, and a cache served on those two checks alone hands
    back a pre-annotation winner for as long as the experiment stays settled."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)

    first = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    second = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert first["generated_at"] == second["generated_at"], "second call served from cache"
    assert first["trusted"]["available"] is False, "nothing is rated yet"

    run = next(r for r in await _runs(db_session, exp) if r.task_id)
    record = (
        await db_session.execute(
            select(QualityRecord).where(QualityRecord.task_id == run.task_id)
        )
    ).scalars().first()
    if record is None:
        record = QualityRecord(
            task_id=run.task_id,
            workspace_id=workspace_id,
            quality_profile={
                "dimensions": [
                    {"key": "correctness", "name": "Correctness", "score": 8,
                     "weight": 1, "status": "scored"}
                ],
                "weighted_score": 8.0,
            },
        )
        db_session.add(record)
        await db_session.flush()
    db_session.add(
        Annotation(
            quality_record_id=record.id,
            task_id=run.task_id,
            workspace_id=workspace_id,
            annotator_type="human",
            annotator_label="reviewer",
            blind_to_model=True,
            blind_to_judge=True,
            blind_to_peers=True,
            verdict="approve",
            dimensions=[{"key": "correctness", "name": "Correctness", "score": 8}],
            judge_observation={"outcome": {"scores": {"correctness": 8}, "gate_passed": True}},
        )
    )
    await db_session.commit()
    await db_session.refresh(exp)

    third = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert third["generated_at"] != first["generated_at"], "recomputed after the rating"
    assert third["calibration_fingerprint"] != first["calibration_fingerprint"]
    # The experiment itself did not move — which is exactly why the two existing
    # checks could not have caught this.
    assert third["input_revision"] == first["input_revision"]
    assert third["input_fingerprint"] == first["input_fingerprint"]

    fourth = (await auth_client.get(f"/api/experiments/{exp.id}/report")).json()
    assert fourth["generated_at"] == third["generated_at"], "and caches again afterwards"


@pytest.mark.asyncio
async def test_the_calibration_fingerprint_is_sampled_before_the_calibration(monkeypatch, db_session, auth_client):
    """Which side of the race the cache falls on is the whole question.

    Sampled AFTER the calibration read, an annotation committed in between is
    stamped into a report that did not use it — and that cache then looks valid
    for as long as the experiment stays settled. Sampled before, the same race
    leaves the stored fingerprint older than reality and the next read simply
    recomputes. One direction is a permanently wrong number, the other is a
    wasted recompute."""
    import app.quality.judge_calibration as jc
    from app.quality import experiment_report as er

    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id)

    order: list[str] = []
    real_fp, real_pairs = er.calibration_fingerprint, jc.collect_judge_human_pairs

    async def spy_fp(*a, **kw):
        order.append("fingerprint")
        return await real_fp(*a, **kw)

    async def spy_pairs(*a, **kw):
        order.append("calibration")
        return await real_pairs(*a, **kw)

    monkeypatch.setattr(er, "calibration_fingerprint", spy_fp)
    monkeypatch.setattr(jc, "collect_judge_human_pairs", spy_pairs)

    await er.compute_report(db_session, exp)
    assert order == ["fingerprint", "calibration"]
