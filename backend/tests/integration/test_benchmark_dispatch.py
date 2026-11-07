"""Product dispatch owns its lifecycle without entering the ordinary agent workflow."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.dispatch import benchmark_tick, fail_unstarted, run_execution_job
from app.benchmarks.jobs import JobStore
from app.benchmarks.task_routing import ordinary_tasks
from app.benchmarks.trials import failure
from app.config import get_settings
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.task import Task
from app.orchestrator.engine import process_ready_task
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


@pytest_asyncio.fixture
async def queued(auth_client, input_selection):
    _, selection, _, _, _ = input_selection
    response = await auth_client.post(
        "/api/benchmark-trials", json={"request_id": str(uuid.uuid4()), **selection}
    )
    assert response.status_code == 202, response.text
    trial_id = uuid.UUID(response.json()["id"])
    store = get_trial_store()
    async with AsyncSession(store.engine) as db:
        job = await db.scalar(select(BenchmarkJob).where(BenchmarkJob.trial_id == trial_id))
        trial = await db.get(BenchmarkTrial, trial_id)
        return store, job.id, trial.id, trial.workspace_id, trial.task_id


async def saved(queued):
    store, job_id, trial_id, _, task_id = queued
    async with AsyncSession(store.engine, expire_on_commit=False) as db:
        return (
            await db.get(BenchmarkJob, job_id),
            await db.get(BenchmarkTrial, trial_id),
            await db.get(Task, task_id),
        )


def never_start(*args, **kwargs):
    pytest.fail("A stopped or already owned launch must not construct a runtime")


@pytest.mark.parametrize("disabled", [False, True])
async def test_stopped_queued_launch_finishes_without_an_agent(queued, monkeypatch, disabled):
    store, job_id, trial_id, workspace_id, _ = queued
    if disabled:
        monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", False)
    else:
        await store.request_cancel(workspace_id, trial_id)
    assert await run_execution_job(store, job_id, runtime_factory=never_start)
    job, trial, task = await saved(queued)
    assert (job.status, job.error_code) == ("cancelled", "CANCELLED")
    assert (trial.phase, trial.execution_status, trial.cleanup_status) == (
        "finished",
        "cancelled",
        "complete",
    )
    assert task.status == "failed" and task.current_attempt_id is None
    assert not await run_execution_job(store, job_id, runtime_factory=never_start)


async def test_factory_failure_is_a_durable_safe_result(queued):
    store, job_id, _, _, _ = queued

    def unavailable(*args, **kwargs):
        raise failure("INSTALLATION_UNAVAILABLE", "Synthetic private path must not be published")

    assert await run_execution_job(store, job_id, runtime_factory=unavailable)
    job, trial, task = await saved(queued)
    assert (job.status, job.error_code) == ("error", "INSTALLATION_UNAVAILABLE")
    assert trial.phase == "finished" and trial.execution_status == "failed"
    assert task.failure_type == "infra" and "private path" not in task.result_summary


async def test_live_owner_is_not_stolen_by_another_dispatcher(queued):
    store, job_id, trial_id, workspace_id, _ = queued
    owner = await store.claim(workspace_id, trial_id, uuid.uuid4())
    await JobStore(store).start(owner, job_id)
    assert await run_execution_job(store, job_id, runtime_factory=never_start)
    job, trial, _ = await saved(queued)
    assert trial.generation == 1 and trial.controller_id == owner.controller_id
    assert job.status == "running"


async def test_expired_owner_uses_recovery_without_resolving_a_package(queued):
    store, job_id, trial_id, workspace_id, _ = queued
    owner = await store.claim(workspace_id, trial_id, uuid.uuid4())
    await JobStore(store).start(owner, job_id)
    await store.transition(owner, "created", "planning")
    async with AsyncSession(store.engine) as db:
        await db.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    calls = []

    class RecoveryRuntime:
        def __init__(self, actual_store, *, recovery):
            assert actual_store is store and recovery

        def recovery(self, current):
            assert current.generation == 2

            async def run(spec):
                assert spec == {}
                calls.append("recover")
                await store.finish_evaluation(current)
                await store.transition(current, "cleaning", "finished")

            return SimpleNamespace(run=run)

        async def close(self):
            calls.append("close")

    assert await run_execution_job(store, job_id, runtime_factory=RecoveryRuntime)
    job, trial, task = await saved(queued)
    assert calls == ["recover", "close"]
    assert trial.generation == 2 and trial.execution_status == "lost"
    assert trial.cleanup_status == "complete" and trial.recording_status == "incomplete"
    assert job.status == "error" and task.status == "failed"


async def test_finished_trial_projects_a_crashed_queue_receipt_without_a_new_owner(queued):
    store, job_id, trial_id, workspace_id, _ = queued
    owner = await store.claim(workspace_id, trial_id, uuid.uuid4())
    await JobStore(store).start(owner, job_id)
    await fail_unstarted(store, owner, "STORAGE_UNAVAILABLE")
    assert await run_execution_job(store, job_id, runtime_factory=never_start)
    job, trial, _ = await saved(queued)
    assert (job.status, job.error_code) == ("error", "STORAGE_UNAVAILABLE")
    assert trial.generation == 1


async def test_native_task_cannot_enter_ordinary_workflows(queued, auth_client):
    store, _, _, _, task_id = queued
    async with AsyncSession(store.engine) as db:
        task = await db.get(Task, task_id)
        await process_ready_task(db, task)
        assert task.status == "ready" and task.current_attempt_id is None
        assert await db.scalar(select(Task.id).where(ordinary_tasks())) is None
    base = "/api/tasks/" + str(task_id)
    for method, suffix, body in (
        ("post", "/start", {}),
        ("post", "/rerun", {}),
        ("patch", "", {"status": "backlog"}),
        ("patch", "/approve", {}),
        ("patch", "/reject", {"feedback": "do not retry this native run"}),
        ("delete", "", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        response = await auth_client.request(method, base + suffix, **kwargs)
        assert response.status_code == 409, (suffix, response.text)


async def test_queued_native_work_drains_regular_work_before_starting(queued, monkeypatch):
    store, _, _, _, _ = queued
    monkeypatch.setattr(
        "app.benchmarks.dispatch.get_process_runtime",
        lambda: SimpleNamespace(list_active=lambda: [{"container_id": "ordinary-active"}]),
    )
    assert await benchmark_tick(store=store, runtime_factory=never_start)
    job, _, _ = await saved(queued)
    assert job.status == "queued"
