"""Queued regrading is scoped, idempotent and never materializes another agent task."""

import asyncio
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.driver import DriverLimits, TrialDriver
from app.benchmarks.recovery import RecoveryService
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkJournalEntry, BenchmarkTrial
from app.models.task import Task
from evalhive_benchmark import ProtocolError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.fixtures.benchmark_execution_outcome import fixture_execution_outcome
from tests.integration.test_benchmark_driver import Host, fixture_capture_validator
from tests.integration.test_benchmark_evaluations import attempt_row


@pytest_asyncio.fixture
async def regrade_source(auth_client, db_session):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    store = get_trial_store()
    trial = await store.create(
        workspace_id,
        snapshot={"benchmark": {"model_bindings": {}}},
        deadline_at=datetime.now(UTC) + timedelta(minutes=3),
    )
    owner = await store.claim(workspace_id, trial.id, uuid.uuid4())
    host = Host(store, owner)
    controller = TrialDriver(
        host,
        RecoveryService(store, uuid.uuid4(), {}),
        capture_providers={},
        capture_validator=fixture_capture_validator,
        execution_outcome=fixture_execution_outcome(owner),
        limits=DriverLimits(
            phase_seconds=10,
            stop_seconds=1,
            capture_seconds=5,
            cleanup_seconds=10,
            service_seconds=3,
            heartbeat_seconds=0.5,
            poll_seconds=0.2,
        ),
    )
    spec = {
        "protocol_version": "1.0",
        "scope": {"workspace_id": str(workspace_id), "trial_id": str(trial.id), "generation": 1},
        "case": {
            "ref": {"dataset_id": "fixture", "revision": "1", "case_id": "regrade"},
            "title": "Regrade fixture",
            "source_digest": "sha256:" + "a" * 64,
            "native_ref": {},
            "requirements": {},
        },
        "config": {"checkpoint": True},
        "models": {},
        "credential_refs": {},
        "limits": {"wall_time_seconds": 180, "memory_mb": 1024, "cpu_cores": 1, "disk_mb": 64},
    }
    try:
        result = await controller.run(spec)
        async with AsyncSession(store.engine) as db:
            journal = list(
                await db.execute(
                    select(BenchmarkJournalEntry.event, BenchmarkJournalEntry.public_data)
                    .where(BenchmarkJournalEntry.trial_id == trial.id)
                    .order_by(BenchmarkJournalEntry.seq)
                )
            )
        failures = [dict(data) for event, data in journal if event == "controller.failed"]
        assert result["phase"] == "finished" and result["evaluation"] == "completed", failures
        source = await attempt_row(controller)
        yield store, trial.id, source
    finally:
        if host.client:
            with suppress(ProtocolError):
                await host.client.close(0.2, shutdown=False)


async def source_identity(store, trial_id):
    async with AsyncSession(store.engine) as db:
        trial = await db.get(BenchmarkTrial, trial_id)
        return trial.phase, trial.next_seq, trial.cancel_requested_at, trial.snapshot


async def request_regrade(client, trial_id, source, *, request_id=None):
    request = {
        "request_id": str(request_id or uuid.uuid4()),
        "source_evaluation_id": str(source.id),
    }
    response = await client.post(f"/api/benchmark-trials/{trial_id}/regrade", json=request)
    return request, response


async def test_regrade_request_survives_reply_loss_without_touching_source(
    auth_client,
    regrade_source,
):
    store, trial_id, source = regrade_source
    before = await source_identity(store, trial_id)
    request, first = await request_regrade(auth_client, trial_id, source)
    assert first.status_code == 202, first.text
    assert first.json()["phase"] == "queued" and first.json()["evaluation_id"] is None
    again = await auth_client.post(f"/api/benchmark-trials/{trial_id}/regrade", json=request)
    assert again.status_code == 202 and first.json() == again.json()
    assert "private" not in first.text and "api_key" not in first.text
    async with AsyncSession(store.engine) as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == 0
        assert await db.scalar(select(func.count()).select_from(BenchmarkEvaluationAttempt)) == 1
        job = await db.get(
            BenchmarkJob,
            uuid.UUID(first.json()["id"]),
            options=[undefer(BenchmarkJob.private_launch)],
        )
        inputs = store.private.open("benchmark-job:" + str(job.id), job.private_launch)
        assert inputs == {"format_version": "1.0", "auxiliary_models": {}}
        assert job.source_evaluation_id == source.id and job.status == "queued"
    assert await source_identity(store, trial_id) == before
    request["wall_time_seconds"] = 1
    conflict = await auth_client.post(f"/api/benchmark-trials/{trial_id}/regrade", json=request)
    assert conflict.status_code == 409


async def test_regrade_cancellation_is_independent_and_durable(auth_client, regrade_source):
    store, trial_id, source = regrade_source
    before = await source_identity(store, trial_id)
    _, response = await request_regrade(auth_client, trial_id, source)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]
    endpoint = f"/api/benchmark-trials/{trial_id}/evaluation-jobs/{job_id}/cancel"
    assert (await auth_client.post(endpoint)).json() == {"requested": True}
    listing = await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluation-jobs")
    assert listing.status_code == 200 and len(listing.json()) == 1
    first = listing.json()[0]["cancel_requested_at"]
    assert first is not None
    assert (await auth_client.post(endpoint)).json() == {"requested": True}
    assert (await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluation-jobs")).json()[0][
        "cancel_requested_at"
    ] == first
    assert await source_identity(store, trial_id) == before


@pytest.mark.parametrize("change", ["trial", "evaluation"])
async def test_regrade_cannot_cross_a_source_identity(auth_client, regrade_source, change):
    _, trial_id, source = regrade_source
    if change == "trial":
        trial_id = uuid.uuid4()
    else:
        source.id = uuid.uuid4()
    _, response = await request_regrade(auth_client, trial_id, source)
    assert response.status_code == 404, response.text


def never_start(*args, **kwargs):
    pytest.fail("A stopped or actively owned regrade must not construct another runtime")


async def queued_job(auth_client, regrade_source):
    store, trial_id, source = regrade_source
    _, response = await request_regrade(auth_client, trial_id, source)
    assert response.status_code == 202, response.text
    return store, trial_id, source, uuid.UUID(response.json()["id"])


async def test_cancelled_queued_regrade_needs_no_controller_or_runtime(auth_client, regrade_source):
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl

    store, trial_id, _, job_id = await queued_job(auth_client, regrade_source)
    before = await source_identity(store, trial_id)
    response = await auth_client.post(
        f"/api/benchmark-trials/{trial_id}/evaluation-jobs/{job_id}/cancel"
    )
    assert response.json() == {"requested": True}
    assert await run_regrade_job(store, job_id, runtime_factory=never_start)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        assert (job.status, job.error_code) == ("cancelled", "CANCELLED")
        assert await db.get(BenchmarkEvaluationControl, job_id) is None
        assert await db.scalar(select(func.count()).select_from(BenchmarkEvaluationAttempt)) == 1
    assert await source_identity(store, trial_id) == before


async def test_disabled_runtime_cancels_only_the_new_evaluation(
    auth_client, regrade_source, monkeypatch
):
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.config import get_settings
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl

    monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", False)
    store, trial_id, _, job_id = await queued_job(auth_client, regrade_source)
    before = await source_identity(store, trial_id)
    assert await run_regrade_job(store, job_id, runtime_factory=never_start)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        control = await db.get(BenchmarkEvaluationControl, job_id)
        attempt = await db.get(BenchmarkEvaluationAttempt, job_id)
        assert job.status == "cancelled" and control.phase == "finished"
        assert control.cleanup_status == "complete" and attempt.status == "cancelled"
    assert await source_identity(store, trial_id) == before


async def test_regrade_factory_failure_is_private_and_source_immutable(
    auth_client, regrade_source, monkeypatch
):
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.benchmarks.trials import failure
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", True)
    store, trial_id, _, job_id = await queued_job(auth_client, regrade_source)
    before = await source_identity(store, trial_id)

    def unavailable(*args, **kwargs):
        raise failure("INSTALLATION_UNAVAILABLE", "Synthetic private path must remain private")

    assert await run_regrade_job(store, job_id, runtime_factory=unavailable)
    rows = (await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluation-jobs")).json()
    assert rows[0]["status"] == "error" and rows[0]["error_code"] == "INSTALLATION_UNAVAILABLE"
    assert "private path" not in str(rows)
    assert await source_identity(store, trial_id) == before


async def test_active_regrade_owner_is_not_replayed(auth_client, regrade_source, monkeypatch):
    from app.benchmarks.evaluation_controls import EvaluationControls
    from app.benchmarks.jobs import JobStore
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.config import get_settings
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl

    monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", True)
    store, trial_id, source, job_id = await queued_job(auth_client, regrade_source)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        workspace, deadline = job.workspace_id, job.deadline_at
    controls = EvaluationControls(store, None)
    owner = await controls.create(
        workspace, trial_id, source.id, uuid.uuid4(), deadline_at=deadline, evaluation_id=job_id
    )
    await JobStore(controls).start(owner, job_id)
    assert await run_regrade_job(store, job_id, runtime_factory=never_start)
    async with AsyncSession(store.engine) as db:
        control = await db.get(BenchmarkEvaluationControl, job_id)
        assert control.generation == 1 and control.controller_id == owner.controller_id
        assert (await db.get(BenchmarkJob, job_id)).status == "running"


async def test_regrade_completes_its_own_sdk_lifecycle_without_an_agent(
    auth_client, regrade_source, monkeypatch
):
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.benchmarks.regrade_driver import RegradeDriver
    from app.config import get_settings
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl

    monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", True)
    store, trial_id, source, job_id = await queued_job(auth_client, regrade_source)
    before, observed = await source_identity(store, trial_id), []

    class Runtime:
        def __init__(self, controls, *, recovery):
            assert not recovery and controls.trials is store
            self.store, self.host = controls, None

        async def evaluation(self, owner, payload):
            assert payload == {"format_version": "1.0", "auxiliary_models": {}}
            observed.append("evaluation")
            self.host = Host(self.store, owner)
            return RegradeDriver(
                self.host,
                RecoveryService(self.store, uuid.uuid4(), {}),
                capture_providers={},
                capture_validator=fixture_capture_validator,
                limits=DriverLimits(
                    phase_seconds=10,
                    stop_seconds=1,
                    capture_seconds=5,
                    cleanup_seconds=10,
                    service_seconds=3,
                    heartbeat_seconds=0.5,
                    poll_seconds=0.2,
                ),
            )

        async def close(self):
            observed.append("close")
            if self.host is not None and self.host.client is not None:
                with suppress(ProtocolError):
                    await self.host.client.close(0.2, shutdown=False)

    runs = await asyncio.gather(
        run_regrade_job(store, job_id, runtime_factory=Runtime),
        run_regrade_job(store, job_id, runtime_factory=Runtime),
    )
    assert any(runs)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        control = await db.get(BenchmarkEvaluationControl, job_id)
        evaluation = await db.get(BenchmarkEvaluationAttempt, job_id)
        assert (job.status, job.error_code) == ("completed", None)
        assert (control.phase, control.cleanup_status, control.recording_status) == (
            "finished",
            "complete",
            "complete",
        )
        assert evaluation.kind == "regrade" and evaluation.source_evaluation_id == source.id
        assert evaluation.status == "completed" and evaluation.success is True
        assert await db.scalar(select(func.count()).select_from(Task)) == 0
    assert observed == ["evaluation", "close"]
    assert await source_identity(store, trial_id) == before
    assert not await run_regrade_job(store, job_id, runtime_factory=never_start)


async def test_expired_evaluation_ownership_cleans_without_reopening_source(
    auth_client, regrade_source, monkeypatch
):
    from types import SimpleNamespace

    from app.benchmarks.evaluation_controls import EvaluationControls
    from app.benchmarks.jobs import JobStore
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.config import get_settings
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
    from sqlalchemy import update

    monkeypatch.setattr(get_settings(), "benchmark_runtime_enabled", True)
    store, trial_id, source, job_id = await queued_job(auth_client, regrade_source)
    before, observed = await source_identity(store, trial_id), []
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        workspace, deadline = job.workspace_id, job.deadline_at
    controls = EvaluationControls(store, None)
    owner = await controls.create(
        workspace, trial_id, source.id, uuid.uuid4(), deadline_at=deadline, evaluation_id=job_id
    )
    await JobStore(controls).start(owner, job_id)
    await controls.transition(owner, "created", "planning")
    async with AsyncSession(store.engine) as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(BenchmarkEvaluationControl.id == job_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()

    class RecoveryRuntime:
        def __init__(self, current, *, recovery):
            assert recovery and current.trials is store
            self.store = current

        def recovery(self, current):
            assert current.generation == 2

            async def run():
                observed.append("cleanup")
                await self.store.finish_evaluation(current)
                await self.store.transition(current, "cleaning", "finished")

            return SimpleNamespace(run=run)

        async def close(self):
            observed.append("close")

    assert await run_regrade_job(store, job_id, runtime_factory=RecoveryRuntime)
    assert observed == ["cleanup", "close"]
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        evaluation = await db.get(BenchmarkEvaluationAttempt, job_id)
        control = await db.get(BenchmarkEvaluationControl, job_id)
        assert job.status == "error" and evaluation.status == "error"
        assert control.generation == 2 and control.phase == "finished"
        assert control.recording_status == "incomplete" and control.cleanup_status == "complete"
        assert (await db.get(BenchmarkEvaluationAttempt, source.id)).status == "completed"
    assert await source_identity(store, trial_id) == before


async def test_viewer_can_read_but_cannot_queue_or_cancel_regrades(
    auth_client, regrade_source, db_session
):
    from app.models.workspace import WorkspaceMember
    from sqlalchemy import update

    store, trial_id, source, job_id = await queued_job(auth_client, regrade_source)
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await db_session.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace)
        .values(role="viewer")
    )
    await db_session.commit()
    _, denied = await request_regrade(auth_client, trial_id, source)
    assert denied.status_code == 403, denied.text
    cancel = await auth_client.post(
        f"/api/benchmark-trials/{trial_id}/evaluation-jobs/{job_id}/cancel"
    )
    assert cancel.status_code == 403
    assert (
        await auth_client.get(f"/api/benchmark-trials/{trial_id}/evaluation-jobs")
    ).status_code == 200
    async with AsyncSession(store.engine) as db:
        assert (await db.get(BenchmarkJob, job_id)).cancel_requested_at is None


async def test_foreign_workspace_cannot_read_or_control_regrade(auth_client, regrade_source):
    _, trial_id, source, job_id = await queued_job(auth_client, regrade_source)
    registered = await auth_client.post(
        "/api/auth/register",
        json={
            "email": f"foreign-regrade-{uuid.uuid4().hex}@example.com",
            "password": "fixture-only-password",
        },
    )
    assert registered.status_code == 200, registered.text
    foreign = registered.json()
    headers = {
        "Authorization": "Bearer " + foreign["access_token"],
        "X-Workspace-Id": foreign["default_workspace_id"],
    }
    base = f"/api/benchmark-trials/{trial_id}"
    response = await auth_client.post(
        base + "/regrade",
        headers=headers,
        json={"request_id": str(uuid.uuid4()), "source_evaluation_id": str(source.id)},
    )
    assert response.status_code == 404, response.text
    assert (await auth_client.get(base + "/evaluation-jobs", headers=headers)).status_code == 404
    assert (
        await auth_client.post(base + f"/evaluation-jobs/{job_id}/cancel", headers=headers)
    ).status_code == 404


async def test_expired_queued_request_never_allocates_evaluation_control(
    auth_client, regrade_source
):
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl

    store, trial_id, source = regrade_source
    before = await source_identity(store, trial_id)
    response = await auth_client.post(
        f"/api/benchmark-trials/{trial_id}/regrade",
        json={
            "request_id": str(uuid.uuid4()),
            "source_evaluation_id": str(source.id),
            "wall_time_seconds": 1,
        },
    )
    assert response.status_code == 202, response.text
    job_id = uuid.UUID(response.json()["id"])
    await asyncio.sleep(1.1)
    assert await run_regrade_job(store, job_id, runtime_factory=never_start)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        assert (job.status, job.error_code) == ("error", "DEADLINE_EXCEEDED")
        assert await db.get(BenchmarkEvaluationControl, job_id) is None
    assert await source_identity(store, trial_id) == before


async def test_finished_evaluation_repairs_queue_receipt_even_after_lease_expiry(
    auth_client, regrade_source, monkeypatch
):
    from app.benchmarks.dispatch import fail_unstarted
    from app.benchmarks.evaluation_controls import EvaluationControls
    from app.benchmarks.jobs import JobStore
    from app.benchmarks.regrade_dispatch import run_regrade_job
    from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
    from sqlalchemy import update

    store, trial_id, source, job_id = await queued_job(auth_client, regrade_source)
    before = await source_identity(store, trial_id)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        workspace, deadline = job.workspace_id, job.deadline_at
    controls = EvaluationControls(store, None)
    owner = await controls.create(
        workspace, trial_id, source.id, uuid.uuid4(), deadline_at=deadline, evaluation_id=job_id
    )
    await JobStore(controls).start(owner, job_id)
    await fail_unstarted(controls, owner, "INSTALLATION_UNAVAILABLE")
    async with AsyncSession(store.engine) as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(BenchmarkEvaluationControl.id == job_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()
    assert await run_regrade_job(store, job_id, runtime_factory=never_start)
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id)
        control = await db.get(BenchmarkEvaluationControl, job_id)
        assert (job.status, job.error_code) == ("error", "INSTALLATION_UNAVAILABLE")
        assert control.generation == 1
    assert await source_identity(store, trial_id) == before
