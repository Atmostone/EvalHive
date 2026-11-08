"""Native retries fork frozen inputs and preserve the source execution ledger."""

import json
import uuid
from copy import deepcopy

import pytest
import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.benchmarks.dispatch import fail_unstarted, finish_request
from app.benchmarks.jobs import JobStore
from app.config import get_settings
from app.harnesses.catalog import get_manifest
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.task import Task
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from tests.integration.test_benchmark_launch_inputs import input_selection as input_selection
from tests.unit.test_benchmark_installations import registry as registry


@pytest_asyncio.fixture
async def queued(auth_client, input_selection, monkeypatch, tmp_path, request):
    _, selection, _, _, _ = input_selection
    manifest = get_manifest("pi")
    if getattr(request, "param", True):
        manifest["requirements"]["runtime_bundle"]["image"] = "sha256:" + "3" * 64
    path = tmp_path / "harnesses" / "pi"
    path.mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(get_settings(), "harness_manifest_dir", str(path.parent))
    response = await auth_client.post(
        "/api/benchmark-trials", json={"request_id": str(uuid.uuid4()), **selection}
    )
    assert response.status_code == 202, response.text
    store = get_trial_store()
    return (
        store,
        uuid.UUID(response.json()["job_id"]),
        uuid.UUID(response.json()["id"]),
        uuid.UUID(auth_client.headers["X-Workspace-Id"]),
        uuid.UUID(response.json()["task_id"]),
    )


@pytest_asyncio.fixture
async def retry_source(queued):
    store, job_id, trial_id, workspace_id, task_id = queued
    owner = await store.claim(workspace_id, trial_id, uuid.uuid4())
    await JobStore(store).start(owner, job_id)
    await fail_unstarted(store, owner, "INSTALLATION_UNAVAILABLE")
    await finish_request(store, owner, job_id)
    return store, job_id, trial_id, task_id


@pytest.mark.parametrize("queued", [False], indirect=True)
async def test_unpinned_runtime_is_not_claimed_as_an_exact_retry(auth_client, retry_source):
    _, _, trial_id, _ = retry_source
    response = await auth_client.post(
        f"/api/benchmark-trials/{trial_id}/retry", json={"request_id": str(uuid.uuid4())}
    )
    assert response.status_code == 409 and "not pinned" in response.text, response.text


async def source_state(store, job_id, trial_id):
    async with AsyncSession(store.engine) as db:
        job = await db.get(BenchmarkJob, job_id, options=[undefer(BenchmarkJob.private_launch)])
        trial = await db.get(BenchmarkTrial, trial_id)
        return deepcopy(trial.snapshot), trial.phase, trial.next_seq, job.private_launch


async def test_retry_binds_fresh_ids_with_immutable_conditions_and_source(
    auth_client,
    db_session,
    retry_source,
    input_selection,
):
    store, source_job, source_id, task_id = retry_source
    _, _, profile, harness, provider = input_selection
    before = await source_state(store, source_job, source_id)
    profile.config = {"steps": 500}
    harness.config = {"temperature": 1}
    provider.api_key = "synthetic-rotated-provider-key"
    await db_session.commit()
    request = {"request_id": str(uuid.uuid4())}
    base = f"/api/benchmark-trials/{source_id}/retry"
    response = await auth_client.post(base, json=request)
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["id"] != str(source_id) and result["task_id"] != str(task_id)
    assert result["retry_of_id"] == str(source_id) and result["snapshot"] == before[0]
    assert "synthetic-rotated-provider-key" not in response.text
    again = await auth_client.post(base, json=request)
    assert again.status_code == 202 and again.json()["id"] == result["id"]
    async with AsyncSession(store.engine) as db:
        original = await db.get(
            BenchmarkJob, source_job, options=[undefer(BenchmarkJob.private_launch)]
        )
        copied = await db.get(
            BenchmarkJob,
            uuid.UUID(result["job_id"]),
            options=[undefer(BenchmarkJob.private_launch)],
        )
        old = store.private.open("benchmark-job:" + str(source_job), original.private_launch)
        payload = store.private.open("benchmark-job:" + str(copied.id), copied.private_launch)
        assert payload["benchmark_spec"]["config"] == old["benchmark_spec"]["config"]
        assert payload["harness_spec"]["config"] == old["harness_spec"]["config"]
        assert payload["harness_spec"]["attempt_id"] != old["harness_spec"]["attempt_id"]
        assert payload["harness_spec"]["metadata"]["benchmark_trial_id"] == result["id"]
        assert payload["benchmark_spec"]["scope"]["trial_id"] == result["id"]
        assert payload["harness_credentials"]["model_api_key"] == "synthetic-rotated-provider-key"
        task = await db.get(Task, uuid.UUID(result["task_id"]))
        assert task.replay_of_task_id == task_id and task.status == "ready"
        assert task.run_config["benchmark_job_id"] == result["job_id"]
        assert await db.scalar(select(func.count()).select_from(Task)) == 2
    assert await source_state(store, source_job, source_id) == before


async def test_active_source_cannot_be_retried(auth_client, queued):
    store, _, trial_id, _, _ = queued
    response = await auth_client.post(
        f"/api/benchmark-trials/{trial_id}/retry", json={"request_id": str(uuid.uuid4())}
    )
    assert response.status_code == 409, response.text
    async with AsyncSession(store.engine) as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == 1


async def test_changed_model_route_requires_new_explicit_conditions(
    auth_client, retry_source, input_selection, db_session
):
    store, _, trial_id, _ = retry_source
    provider = input_selection[-1]
    provider.endpoint = "http://127.0.0.1:8/v1"
    await db_session.commit()
    response = await auth_client.post(
        f"/api/benchmark-trials/{trial_id}/retry", json={"request_id": str(uuid.uuid4())}
    )
    assert response.status_code == 409 and "changed" in response.text.lower(), response.text
    async with AsyncSession(store.engine) as db:
        assert await db.scalar(select(func.count()).select_from(Task)) == 1


async def test_retry_request_id_cannot_change_source(auth_client, retry_source):
    _, _, source_id, _ = retry_source
    request = {"request_id": str(uuid.uuid4())}
    first = await auth_client.post(f"/api/benchmark-trials/{source_id}/retry", json=request)
    assert first.status_code == 202, first.text
    changed = await auth_client.post(f"/api/benchmark-trials/{uuid.uuid4()}/retry", json=request)
    assert changed.status_code == 409, changed.text


async def test_unknown_source_and_unsupported_override_are_rejected(auth_client):
    base = f"/api/benchmark-trials/{uuid.uuid4()}/retry"
    assert (await auth_client.post(base, json={"request_id": str(uuid.uuid4())})).status_code == 404
    assert (
        await auth_client.post(
            base, json={"request_id": str(uuid.uuid4()), "harness_profile_id": str(uuid.uuid4())}
        )
    ).status_code == 422
