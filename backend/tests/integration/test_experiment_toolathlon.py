"""Toolathlon executable-eval lifecycle in the Experiment Runner.

Drives advance_experiment through the two extra states
(PREPROCESSING / EVALUATING) with the Docker boundary (app.quality.external_eval)
stubbed — the orchestrator/agent never run in tests, so the agent task is
flipped manually, mirroring test_experiment_lifecycle.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.event import AgentEvent
from app.models.experiment import (
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.models.registry_entry import RegistryEntry
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.quality import experiments as exp_mod
from app.quality.experiments import advance_experiment, start_experiment


class FakeExt:
    """Stub of app.quality.external_eval: records calls, scripts poll results by
    container-name prefix (``pre-`` / ``eval-``)."""

    def __init__(self, *, pre=None, ev=None):
        self.pre_responses = list(pre or [])
        self.eval_responses = list(ev or [])
        self.removed: list[str] = []
        self.preprocess_calls: list[tuple] = []
        self.eval_calls: list[tuple] = []

    def launch_time_pair(self):
        return ("2026-06-14 10:00:00 Saturday", "2026-06-14 10:00:00")

    def start_preprocess(
        self, task_id, task_path, cmd, launch_time, *, keep_alive=False, pg_host=None
    ):
        # pg_host: SPA-69 per-lane PG routing (None for the shared/serial path).
        self.preprocess_calls.append((str(task_id), launch_time, keep_alive, pg_host))
        return f"pre-{str(task_id)[:8]}"

    def preprocess_container_name(self, task_id):
        return f"tlpre-{str(task_id)[:8]}"

    def start_eval(self, task_id, task_path, cmd, gt, launch_time, *, pg_host=None):
        self.eval_calls.append((str(task_id), launch_time, pg_host))
        return f"eval-{str(task_id)[:8]}"

    def poll_exit(self, container_id):
        if container_id.startswith("pre-"):
            return self.pre_responses.pop(0) if self.pre_responses else (None, "")
        if container_id.startswith("eval-"):
            return self.eval_responses.pop(0) if self.eval_responses else (None, "")
        return (None, "")

    def has_unconverted_data_error(self, logs):
        return "unconverted data remains" in (logs or "")

    def remove(self, container_id):
        if container_id:
            self.removed.append(container_id)


async def _template(db, workspace_id):
    t = Template(
        name="Toolathlon Runner",
        description="tl",
        soul_md="# soul",
        tool_ids=[],
        tags=[],
        workspace_id=workspace_id,
    )
    db.add(t)
    await db.commit()
    return t


async def _registry(db, workspace_id, server="terminal"):
    e = RegistryEntry(
        workspace_id=workspace_id,
        name=f"toolathlon-{server}",
        kind="mcp",
        config={"command": "echo", "args": []},
        secrets={},
    )
    db.add(e)
    await db.commit()
    return e


def _case(key="tl-1", path="tasks/x"):
    return {
        "case_key": key,
        "title": f"TL {key}",
        "description": "do the thing",
        "external_eval": {
            "preprocess_command": (
                f"python ${{TOOLATHLON_GYM_PATH}}/{path}/preprocess/main.py "
                "--agent_workspace ${AGENT_WORKSPACE} --launch_time ${LAUNCH_TIME}"
            ),
            "eval_command": (
                f"python ${{TOOLATHLON_GYM_PATH}}/{path}/evaluation/main.py "
                "--agent_workspace ${AGENT_WORKSPACE} "
                "--groundtruth_workspace ${GROUNDTRUTH_WORKSPACE} "
                "--launch_time ${LAUNCH_TIME} --res_log_file ${RES_LOG_FILE}"
            ),
            "groundtruth_path": f"{path}/groundtruth_workspace",
        },
        "environment": {"required_services": ["toolathlon_pg"], "mcp_servers": ["terminal"]},
        "meta": {"task_path": path},
    }


async def _make_exp(db, workspace_id, tpl, cases, *, n_toolathlon_lanes=None, max_parallel=None):
    config = {
        "config_key": "cfg-01",
        "label": "glm",
        "fingerprint": "fp1",
        "orchestrator": False,
        "template_id": str(tpl.id),
    }
    exp = Experiment(
        workspace_id=workspace_id,
        name=f"tl-{uuid.uuid4().hex[:6]}",
        dataset={"source": "benchmark_suite", "suite": "toolathlon"},
        dataset_cases=cases,
        matrix_spec={"configurations": [config], "axes": None},
        configurations=[config],
        n_runs_per_cell=1,
        eval_config={"trajectory": False},
        status=ExperimentStatus.DRAFT.value,
        n_toolathlon_lanes=n_toolathlon_lanes,
        max_parallel=max_parallel,
    )
    db.add(exp)
    await db.commit()
    await db.refresh(exp)
    return exp


async def _runs(db, exp):
    return (
        (
            await db.execute(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_id == exp.id)
                .order_by(ExperimentRun.case_key, ExperimentRun.run_index)
            )
        )
        .scalars()
        .all()
    )


async def _flip_agent(db, exp, status=TaskStatus.DONE.value):
    """Flip the RUNNING run's agent task terminal (orchestrator doesn't run)."""
    for r in await _runs(db, exp):
        if r.status == ExperimentRunStatus.RUNNING.value and r.task_id:
            task = await db.get(Task, r.task_id)
            task.status = status
            task.cost_usd = Decimal("0.02")
    await db.commit()


@pytest.mark.asyncio
async def test_toolathlon_full_lifecycle_pass(auth_client, db_session, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt(pre=[(0, "preprocess ok")], ev=[(0, "eval pass")])
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    exp = await _make_exp(db_session, ws, tpl, [_case()])
    await start_experiment(db_session, exp)
    assert [r.status for r in await _runs(db_session, exp)] == [
        ExperimentRunStatus.PENDING.value
    ]

    # Tick 1: claim → BACKLOG task + preprocess detached.
    await advance_experiment(db_session, exp)
    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.PREPROCESSING.value
    assert run.launch_time == "2026-06-14 10:00:00 Saturday"
    task = await db_session.get(Task, run.task_id)
    assert task.status == TaskStatus.BACKLOG.value  # not yet spawnable
    assert task.run_config["agent_image"] == "evalhive-agent-toolathlon:latest"
    assert task.run_config["max_iterations"] == exp_mod.TOOLATHLON_MAX_ITERATIONS
    assert task.run_config["tools_override"]["enable"]  # the toolathlon-terminal id
    assert len(fake.preprocess_calls) == 1

    # Tick 2: preprocess exited 0 → flip task READY, run RUNNING.
    await advance_experiment(db_session, exp)
    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.RUNNING.value
    task = await db_session.get(Task, run.task_id)
    assert task.status == TaskStatus.READY.value
    assert any(c.startswith("pre-") for c in fake.removed)

    # Agent runs (stubbed): flip it DONE, then tick → eval starts.
    await _flip_agent(db_session, exp)
    await advance_experiment(db_session, exp)
    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.EVALUATING.value
    assert len(fake.eval_calls) == 1
    # eval reuses the SAME launch_time captured at preprocess.
    assert fake.eval_calls[0][1] == "2026-06-14 10:00:00 Saturday"

    # Tick 4: eval exited 0 → verdict pass, settle, finalize.
    await advance_experiment(db_session, exp)
    await db_session.refresh(exp)
    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.SUCCESS.value
    assert run.external_verdict is True
    assert exp.status == ExperimentStatus.COMPLETED.value
    assert any(c.startswith("eval-") for c in fake.removed)

    events = (
        (
            await db_session.execute(
                select(AgentEvent).where(
                    AgentEvent.task_id == run.task_id,
                    AgentEvent.event_type == "external_eval_verdict",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].data["passed"] is True


@pytest.mark.asyncio
async def test_toolathlon_eval_fail_is_verdict_not_run_failure(
    auth_client, db_session, monkeypatch
):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt(pre=[(0, "ok")], ev=[(1, "assertion failed")])
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    exp = await _make_exp(db_session, ws, tpl, [_case()])
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)  # → PREPROCESSING
    await advance_experiment(db_session, exp)  # → RUNNING
    await _flip_agent(db_session, exp)
    await advance_experiment(db_session, exp)  # → EVALUATING
    await advance_experiment(db_session, exp)  # eval exit 1
    await db_session.refresh(exp)

    (run,) = await _runs(db_session, exp)
    # Agent finished → SUCCESS; checker failed → verdict False (the RQ2 crux).
    assert run.status == ExperimentRunStatus.SUCCESS.value
    assert run.external_verdict is False


@pytest.mark.asyncio
async def test_toolathlon_preprocess_failure_fails_run_and_task(
    auth_client, db_session, monkeypatch
):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt(pre=[(1, "boom: setup error")])
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    exp = await _make_exp(db_session, ws, tpl, [_case()])
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)  # → PREPROCESSING
    await advance_experiment(db_session, exp)  # preprocess exit 1, no unconverted
    await db_session.refresh(exp)

    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.FAILED.value
    assert run.external_verdict is None
    assert "boom" in (run.preprocess_log or "")
    task = await db_session.get(Task, run.task_id)
    assert task.status == TaskStatus.FAILED.value  # orphan BACKLOG task cleaned up
    assert exp.status == ExperimentStatus.FAILED.value
    assert not fake.eval_calls


@pytest.mark.asyncio
async def test_toolathlon_unconverted_data_retries_with_short_launch_time(
    auth_client, db_session, monkeypatch
):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt(pre=[(1, "ValueError: unconverted data remains:  Saturday"), (0, "ok")])
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    exp = await _make_exp(db_session, ws, tpl, [_case()])
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)  # → PREPROCESSING (long launch_time)
    await advance_experiment(db_session, exp)  # exit 1 + unconverted → retry (short)
    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.PREPROCESSING.value
    assert run.preprocess_retried is True
    assert run.launch_time == "2026-06-14 10:00:00"  # short form, reused at eval
    assert len(fake.preprocess_calls) == 2

    await advance_experiment(db_session, exp)  # retry exit 0 → RUNNING
    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.RUNNING.value


@pytest.mark.asyncio
async def test_toolathlon_portal_case_shares_preprocess_netns(
    auth_client, db_session, monkeypatch
):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt()  # preprocess kept alive → never "exits" in this stub
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    case = _case()
    case["description"] = "Read the methodology at http://localhost:30215 then proceed."
    exp = await _make_exp(db_session, ws, tpl, [case])
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)  # claim → seed + preprocess (kept alive)

    (run,) = await _runs(db_session, exp)
    assert run.status == ExperimentRunStatus.PREPROCESSING.value
    # preprocess started with keep_alive=True (3rd tuple element)
    assert fake.preprocess_calls and fake.preprocess_calls[-1][2] is True
    # the BACKLOG task carries the netns share so the agent reaches localhost:PORT
    task = await db_session.get(Task, run.task_id)
    assert task.run_config["network_mode"] == f"container:tlpre-{str(task.id)[:8]}"


@pytest.mark.asyncio
async def test_toolathlon_non_portal_case_uses_default_network(
    auth_client, db_session, monkeypatch
):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt(pre=[(0, "ok")])
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    exp = await _make_exp(db_session, ws, tpl, [_case()])  # no localhost portal
    await start_experiment(db_session, exp)
    await advance_experiment(db_session, exp)

    (run,) = await _runs(db_session, exp)
    assert fake.preprocess_calls[-1][2] is False  # keep_alive off
    task = await db_session.get(Task, run.task_id)
    assert "network_mode" not in (task.run_config or {})  # default bridge network


@pytest.mark.asyncio
async def test_toolathlon_runs_serially_one_cell_at_a_time(
    auth_client, db_session, monkeypatch
):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt()  # never let preprocess finish → first cell stays in flight
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    exp = await _make_exp(
        db_session, ws, tpl, [_case("tl-1", "tasks/a"), _case("tl-2", "tasks/b")]
    )
    await start_experiment(db_session, exp)
    assert len(await _runs(db_session, exp)) == 2

    await advance_experiment(db_session, exp)  # claim — but only ONE (shared PG)
    runs = await _runs(db_session, exp)
    statuses = sorted(r.status for r in runs)
    assert statuses == [
        ExperimentRunStatus.PENDING.value,
        ExperimentRunStatus.PREPROCESSING.value,
    ]

    # Still one in flight on the next tick (the first preprocess never exits).
    await advance_experiment(db_session, exp)
    runs = await _runs(db_session, exp)
    assert sorted(r.status for r in runs) == [
        ExperimentRunStatus.PENDING.value,
        ExperimentRunStatus.PREPROCESSING.value,
    ]


@pytest.mark.asyncio
async def test_toolathlon_lanes_claim_parallel_cells_on_distinct_lanes(
    auth_client, db_session, monkeypatch
):
    # SPA-69: with n_toolathlon_lanes=2 the scheduler claims up to TWO Toolathlon
    # cells at once (vs one in the serial path), each pinned to a DISTINCT lane —
    # so concurrent preprocess re-seeds target different toolathlon_pg_lane_<i>.
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, ws)
    await _registry(db_session, ws)
    fake = FakeExt()  # preprocess never exits → claimed cells stay in flight
    monkeypatch.setattr(exp_mod, "ext_eval", fake)

    # Pin parallelism well above the lane count so LANES (not the ambient
    # max_concurrent_agents, which persists across tests) are the binding cap.
    async def _target(db, *, parallel):
        return 1 if not parallel else 8

    monkeypatch.setattr(exp_mod, "inflight_target", _target)

    exp = await _make_exp(
        db_session,
        ws,
        tpl,
        [_case("tl-1", "tasks/a"), _case("tl-2", "tasks/b"), _case("tl-3", "tasks/c")],
        n_toolathlon_lanes=2,
    )
    await start_experiment(db_session, exp)
    assert len(await _runs(db_session, exp)) == 3

    await advance_experiment(db_session, exp)  # claim exactly 2 (the lane cap)
    runs = await _runs(db_session, exp)
    inflight = [r for r in runs if r.status == ExperimentRunStatus.PREPROCESSING.value]
    pending = [r for r in runs if r.status == ExperimentRunStatus.PENDING.value]
    assert len(inflight) == 2
    assert len(pending) == 1
    # the two in-flight runs hold the two distinct lanes 0 and 1
    assert sorted(r.lane_index for r in inflight) == [0, 1]
