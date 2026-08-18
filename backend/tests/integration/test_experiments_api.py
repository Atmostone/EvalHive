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


# --- eval_config.trace: the trim policy is per experiment (SPA-86) ------------


@pytest.mark.asyncio
async def test_create_accepts_a_trace_trim_block(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="trace cfg ok")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(
            tpl.id,
            name="trace-cfg-ok",
            eval_config={
                "trace": {
                    "tool_output_token_cap": 0,
                    "tool_args_token_cap": 0,
                    "max_input_tokens": 0,
                    "keep_tail_on_error": True,
                }
            },
        ),
    )
    assert r.status_code == 201, r.text
    detail = (await auth_client.get(f"/api/experiments/{r.json()['id']}")).json()
    assert detail["eval_config"]["trace"]["max_input_tokens"] == 0


@pytest.mark.asyncio
async def test_create_rejects_a_misspelled_trace_key(auth_client: AsyncClient, db_session):
    """A typo here fails silently and quietly changes what every run in the
    experiment was judged on — better a 400 than a corpus mislabelled."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="trace cfg typo")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(
            tpl.id,
            name="trace-cfg-typo",
            eval_config={"trace": {"max_input_token": 0}},  # missing the plural
        ),
    )
    assert r.status_code == 400, r.text
    assert "max_input_token" in r.text


@pytest.mark.asyncio
async def test_create_rejects_a_negative_trace_cap(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="trace cfg negative")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(
            tpl.id,
            name="trace-cfg-negative",
            eval_config={"trace": {"tool_output_token_cap": -5}},
        ),
    )
    assert r.status_code == 400, r.text


# --- eval_config as a whole: the judge threshold is a pre-registration (SPA-87) ---


@pytest.mark.asyncio
async def test_create_accepts_a_pre_registered_judge_threshold(
    auth_client: AsyncClient, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="threshold ok")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(tpl.id, name="threshold-ok", eval_config={"judge_threshold": 6.0}),
    )
    assert r.status_code == 201, r.text
    detail = (await auth_client.get(f"/api/experiments/{r.json()['id']}")).json()
    assert detail["eval_config"]["judge_threshold"] == 6.0


@pytest.mark.asyncio
async def test_create_rejects_a_misspelled_top_level_eval_key(
    auth_client: AsyncClient, db_session
):
    """The hole this field exists to close: `judge_threshld: 6` used to be stored,
    fingerprinted and ignored, and the report went on using the constant in the
    code — an experiment whose recorded intent and actual conduct disagreed."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="threshold typo")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(tpl.id, name="threshold-typo", eval_config={"judge_threshld": 6}),
    )
    assert r.status_code == 400, r.text
    assert "judge_threshld" in r.text


@pytest.mark.asyncio
async def test_create_rejects_a_threshold_off_the_judge_scale(
    auth_client: AsyncClient, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="threshold range")

    for bad in (11, -1, "high"):
        r = await auth_client.post(
            "/api/experiments",
            json=_body(
                tpl.id, name=f"threshold-bad-{bad}", eval_config={"judge_threshold": bad}
            ),
        )
        assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_create_rejects_a_wrong_typed_eval_flag(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="flag type")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(tpl.id, name="flag-type", eval_config={"judge_incomplete_runs": "yes"}),
    )
    assert r.status_code == 400, r.text
    assert "judge_incomplete_runs" in r.text


@pytest.mark.asyncio
async def test_create_rejects_an_unknown_eval_mode(auth_client: AsyncClient, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="eval mode")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(tpl.id, name="eval-mode-bad", eval_config={"eval_mode": "judged"}),
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_the_threshold_reaches_the_report_as_pre_registered(
    auth_client: AsyncClient, db_session
):
    """A field nobody reads is worse than no field. The report must say which
    threshold it used and that the experiment committed to it in advance."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="threshold report")

    r = await auth_client.post(
        "/api/experiments",
        json=_body(tpl.id, name="threshold-report", eval_config={"judge_threshold": 7.0}),
    )
    assert r.status_code == 201, r.text
    report = (await auth_client.get(f"/api/experiments/{r.json()['id']}/report")).json()
    assert report["rq2"]["judge_threshold"] == 7.0
    assert report["rq2"]["threshold_source"] == "pre_registered"
    assert report["rq2"]["primary"] is False
    assert report["judge_discrimination"]["primary"] is True


# --- the exclusion must hold everywhere, not only in the report (SPA-87) ------


async def _experiment_with_a_contaminated_run(auth_client, db_session):
    """One clean run scoring 9.0 and one quota casualty scoring 1.0. If anything
    averages them the answer is 5.0, which is the whole bug."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"excl {uuid.uuid4().hex[:4]}")
    r = await auth_client.post("/api/experiments", json=_body(tpl.id))
    assert r.status_code == 201, r.text
    exp_id = r.json()["id"]

    db_session.add_all([
        ExperimentRun(
            experiment_id=uuid.UUID(exp_id), config_key="cfg-01", case_key="case-a",
            run_index=0, status=ExperimentRunStatus.SUCCESS.value,
            weighted_score=9.0, trajectory_score=9.0, cost_usd=Decimal("0.01"),
            duration_seconds=10, attempt_count=1,
        ),
        ExperimentRun(
            experiment_id=uuid.UUID(exp_id), config_key="cfg-01", case_key="case-b",
            run_index=0, status=ExperimentRunStatus.FAILED.value,
            weighted_score=1.0, trajectory_score=1.0, cost_usd=Decimal("0.01"),
            duration_seconds=10, attempt_count=1, failure_type="llm_rate_limit",
        ),
    ])
    await db_session.commit()
    return exp_id


@pytest.mark.asyncio
async def test_the_live_matrix_counts_a_contaminated_run_but_does_not_average_it(
    auth_client: AsyncClient, db_session
):
    exp_id = await _experiment_with_a_contaminated_run(auth_client, db_session)
    detail = (await auth_client.get(f"/api/experiments/{exp_id}")).json()
    cells = {(c["config_key"], c["case_key"]): c for c in detail["matrix"]}

    clean = cells[("cfg-01", "case-a")]
    dirty = cells[("cfg-01", "case-b")]
    assert clean["quality_mean"] == 9.0 and clean["contaminated"] == 0
    # The run still EXISTS — the matrix is a progress view — but its score is not
    # the model's, so the cell reports no quality at all rather than a 1.0.
    assert dirty["counts"] == {"failed": 1}
    assert dirty["contaminated"] == 1
    assert dirty["quality_mean"] is None and dirty["trajectory_mean"] is None
    assert detail["run_totals"]["contaminated"] == 1


@pytest.mark.asyncio
async def test_analytics_excludes_contaminated_runs_and_reports_the_count(
    auth_client: AsyncClient, db_session
):
    exp_id = await _experiment_with_a_contaminated_run(auth_client, db_session)
    rows = (await auth_client.get("/api/analytics/configs")).json()
    row = next(r for r in rows if r["config_id"] == f"{exp_id}:cfg-01")
    assert row["run_count"] == 1
    assert row["contaminated"] == 1
    assert row["quality_mean"] == 9.0        # not 5.0
    assert row["success_rate"] == 1.0        # not 0.5


@pytest.mark.asyncio
async def test_results_and_export_mark_a_contaminated_run(
    auth_client: AsyncClient, db_session
):
    """Raw endpoints stay unfiltered on purpose — they are the ledger — but a
    consumer must be able to tell a quota casualty from a weak result."""
    exp_id = await _experiment_with_a_contaminated_run(auth_client, db_session)

    results = (await auth_client.get(f"/api/experiments/{exp_id}/results")).json()
    by_case = {r["case_key"]: r for r in results}
    assert len(by_case) == 2  # nothing was dropped
    assert by_case["case-b"]["failure_type"] == "llm_rate_limit"
    assert by_case["case-b"]["contaminated"] is True
    assert by_case["case-a"]["contaminated"] is False

    csv_text = (
        await auth_client.get(f"/api/experiments/{exp_id}/export?format=csv")
    ).text
    assert "failure_type" in csv_text.splitlines()[0]
    assert "llm_rate_limit" in csv_text


@pytest.mark.asyncio
async def test_a_new_experiment_records_the_threshold_it_accepted(
    auth_client: AsyncClient, db_session
):
    """Accepting the default IS a pre-registration — as long as it is on the
    record before any result exists, rather than read out of a constant later."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name="threshold stamp")

    r = await auth_client.post("/api/experiments", json=_body(tpl.id, name="stamped"))
    assert r.status_code == 201, r.text
    detail = (await auth_client.get(f"/api/experiments/{r.json()['id']}")).json()
    assert detail["eval_config"]["judge_threshold"] == 5.0

    report = (await auth_client.get(f"/api/experiments/{r.json()['id']}/report")).json()
    assert report["rq2"]["threshold_source"] == "pre_registered"


@pytest.mark.asyncio
async def test_an_arm_with_no_clean_runs_reports_nothing_rather_than_zero(
    auth_client: AsyncClient, db_session
):
    """The sharpest form of «infrastructure looks like a weak model»: a
    configuration a provider outage wiped out entirely has no observations, and
    coercing that to 0.0 made it lose every comparison on the strength of the
    outage. Absent is not zero."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id, name=f"wiped {uuid.uuid4().hex[:4]}")
    r = await auth_client.post("/api/experiments", json=_body(tpl.id))
    assert r.status_code == 201, r.text
    exp_id = r.json()["id"]

    db_session.add_all([
        ExperimentRun(
            experiment_id=uuid.UUID(exp_id), config_key="cfg-01", case_key="case-a",
            run_index=0, status=ExperimentRunStatus.SUCCESS.value,
            weighted_score=8.0, cost_usd=Decimal("0.01"), duration_seconds=10,
            attempt_count=1,
        ),
        # cfg-02: every run killed by the quota — no observations at all.
        ExperimentRun(
            experiment_id=uuid.UUID(exp_id), config_key="cfg-02", case_key="case-a",
            run_index=0, status=ExperimentRunStatus.FAILED.value,
            weighted_score=0.4, cost_usd=Decimal("0.01"), duration_seconds=10,
            attempt_count=1, failure_type="llm_rate_limit",
        ),
        ExperimentRun(
            experiment_id=uuid.UUID(exp_id), config_key="cfg-02", case_key="case-b",
            run_index=0, status=ExperimentRunStatus.FAILED.value,
            weighted_score=0.6, cost_usd=Decimal("0.01"), duration_seconds=10,
            attempt_count=1, failure_type="llm_rate_limit",
        ),
    ])
    await db_session.commit()

    rows = (await auth_client.get("/api/analytics/configs")).json()
    wiped = next(r for r in rows if r["config_id"] == f"{exp_id}:cfg-02")
    assert wiped["run_count"] == 0
    assert wiped["contaminated"] == 2
    # Not 0.0 anywhere — nothing is being claimed about this arm.
    for key in (
        "success_rate", "failure_rate", "quality_mean",
        "trajectory_mean", "pass_rate", "avg_time_seconds", "avg_cost_usd",
    ):
        assert wiped[key] is None, key

    survivor = next(r for r in rows if r["config_id"] == f"{exp_id}:cfg-01")
    assert survivor["quality_mean"] == 8.0
