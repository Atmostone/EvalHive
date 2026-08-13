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
async def test_a_cell_that_ran_before_the_counter_existed_is_still_preserved(
    auth_client, db_session
):
    """Rows predating the attempt column read as attempt_count=0. Treating that as
    'never ran' is what would have destroyed 23 real evaluations on the live DB."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    exp, _ = await _settled_experiment(db_session, workspace_id, task_status=TaskStatus.FAILED.value)

    victim = next(
        r for r in await _runs(db_session, exp) if r.status == ExperimentRunStatus.FAILED.value
    )
    victim.attempt_count = 0  # simulate a pre-migration row
    victim.weighted_score = 7.5
    await db_session.commit()

    await retry_failed_experiment(db_session, exp)

    kept = await _attempts(db_session, victim.id)
    assert [a.attempt_index for a in kept] == [1]
    assert kept[0].weighted_score == 7.5, "the evaluation the cap-hit run carried survives"


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
