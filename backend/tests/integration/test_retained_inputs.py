"""Checkpoint files are admitted, verified, fenced and reclaimed independently."""

import asyncio
import hashlib
import io
import subprocess
import sys
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.benchmarks.evidence import EvidenceStore, reference
from app.benchmarks.retained_inputs import RetainedInputs
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_evaluation_control import BenchmarkEvaluationControl
from app.models.benchmark_evaluation_input import BenchmarkEvaluationInput
from app.models.benchmark_trial import BenchmarkEnvironmentSession, BenchmarkResourceLease
from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
from app.runtime.managed_spec import OwnerScope, PathMapping, Quota, RuntimePolicy
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.integration.test_benchmark_driver import driver as _driver
from tests.integration.test_benchmark_evidence import MemoryObjects, Response
from tests.integration.test_evaluation_controls import controls as _controls

driver, controls = _driver, _controls


@pytest_asyncio.fixture
async def retained(controls, tmp_path):
    _, store, source, owner, _ = controls
    data = b"checkpoint fixture\n" * 131072
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    session_id, artifact_id = uuid.uuid4(), uuid.uuid4()
    client, bucket, key = MemoryObjects(), "fixture-evidence", "private-checkpoint-fixture"
    client.put_object(bucket, key, io.BytesIO(data), len(data), metadata={"sha256": digest[7:]})
    async with AsyncSession(store.engine) as db:
        db.add(
            BenchmarkEnvironmentSession(
                id=session_id,
                trial_id=source.trial_id,
                workspace_id=owner.workspace_id,
                origin_generation=1,
                purpose="execution",
                plan_digest="0" * 64,
                private_plan=store.private.seal("session:" + str(session_id), {}),
            )
        )
        await db.flush()
        artifact = BenchmarkArtifact(
            id=artifact_id,
            session_id=session_id,
            trial_id=source.trial_id,
            workspace_id=owner.workspace_id,
            origin_generation=1,
            logical_id="checkpoint",
            digest=digest,
            size_bytes=len(data),
            visibility="private",
            media_type="application/octet-stream",
            private_locator=store.private.seal(
                "artifact:" + str(artifact_id), {"bucket": bucket, "key": key}
            ),
        )
        db.add(artifact)
        ref = reference(artifact)
        await db.commit()
    evidence = EvidenceStore(store, client, bucket=bucket)
    policy = RuntimePolicy(PathMapping(str(tmp_path), str(tmp_path)))
    inputs = RetainedInputs(store, owner, policy, evidence)
    scope = OwnerScope(
        str(owner.workspace_id),
        str(owner.trial_id),
        str(uuid.uuid5(owner.controller_id, "benchmark.environment")),
        1,
    )
    spec = {"checkpoint": {"artifacts": [ref]}}
    native = {"resources": [{"config": {"retained_inputs": ["checkpoint"]}}]}
    plan = inputs.plan(spec, native, scope)
    await store.transition(owner, "created", "planning")

    async def admit(disk_mb=plan.disk_mb):
        async def measure():
            return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())

        return await store.reserve(
            owner,
            demands=(ResourceDemand("worker", Quota(256, 0.1), 256, 0.1, 16),),
            native_resources={},
            measure=measure,
            runtime_policy=policy,
            group_policy=GroupPolicy(),
            auxiliary_disk_mb=disk_mb,
        )

    return SimpleNamespace(
        store=store,
        owner=owner,
        source=source,
        inputs=inputs,
        plan=plan,
        client=client,
        evidence=evidence,
        policy=policy,
        ref=ref,
        data=data,
        admit=admit,
        path=Path(plan.grants["checkpoint"].host_path),
    )


async def input_rows(item):
    async with AsyncSession(item.store.engine) as db:
        return (await db.scalars(select(BenchmarkEvaluationInput))).all()


async def clean(item):
    await item.store.transition(item.owner, "planning", "cleaning")
    await item.inputs.release()
    await item.store.transition(item.owner, "cleaning", "finished")


async def test_materialization_requires_admission_and_keeps_only_verified_readonly_bytes(retained):
    item = retained
    with pytest.raises(ProtocolError) as denied:
        await item.inputs.materialize(item.plan)
    assert denied.value.code == "NO_RESERVATION" and not item.path.exists()
    assert await input_rows(item) == []
    budget = await item.admit()
    assert budget["group_resources"]["disk_mb"] == 16 + item.plan.disk_mb
    await item.inputs.materialize(item.plan)
    assert item.path.read_bytes() == item.data and item.path.stat().st_mode & 0o777 == 0o444
    rows = await input_rows(item)
    assert len(rows) == 1 and rows[0].state == "ready"
    await item.inputs.materialize(item.plan)
    assert len(await input_rows(item)) == 1
    await clean(item)
    assert not item.path.exists() and (await input_rows(item))[0].state == "released"


async def test_capacity_receipt_cannot_be_reused_with_another_input_budget(retained):
    item = retained
    await item.admit(0)
    with pytest.raises(ProtocolError) as denied:
        await item.inputs.materialize(item.plan)
    assert denied.value.code == "NO_RESERVATION" and not item.path.exists()
    with pytest.raises(ProtocolError) as conflict:
        await item.admit()
    assert conflict.value.code == "ADMISSION_CONFLICT"


async def test_corrupt_evidence_never_becomes_a_mount_and_is_recoverably_owned(retained):
    item = retained
    await item.admit()
    item.client.bad_stat = True
    with pytest.raises(ProtocolError) as corrupt:
        await item.inputs.materialize(item.plan)
    assert corrupt.value.code == "ARTIFACT_CORRUPT" and not item.path.exists()
    assert (await input_rows(item))[0].state == "reserved"
    await clean(item)
    assert (await input_rows(item))[0].state == "released"


async def test_local_corruption_cannot_be_hidden_by_an_idempotent_materialization(retained):
    item = retained
    await item.admit()
    await item.inputs.materialize(item.plan)
    item.path.chmod(0o600)
    item.path.write_bytes(b"corrupted")
    with pytest.raises(ProtocolError) as corrupt:
        await item.inputs.materialize(item.plan)
    assert corrupt.value.code == "ARTIFACT_CORRUPT"
    await clean(item)


async def test_cancelled_download_holds_ownership_until_blocking_io_returns(retained):
    item = retained
    await item.admit()
    entered, resume = threading.Event(), threading.Event()

    class SlowResponse(Response):
        def read(self, size=-1):
            entered.set()
            if not resume.wait(5):
                raise TimeoutError("Fixture download was not released")
            return super().read(size)

    item.client.get_object = lambda *args: SlowResponse(item.data)
    task = asyncio.create_task(item.inputs.materialize(item.plan))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0.1)
        assert not task.done()
        await item.store.heartbeat(item.owner)
        await item.store.request_stop(item.owner)
        cleanup = asyncio.create_task(item.store.transition(item.owner, "planning", "cleaning"))
        await asyncio.sleep(0.1)
        assert not cleanup.done()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await cleanup
        assert not item.path.exists()
        await item.inputs.release()
        await item.store.transition(item.owner, "cleaning", "finished")
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_takeover_reclaims_predecessor_files_without_restart_or_source_changes(retained):
    item = retained
    await item.admit()
    await item.inputs.materialize(item.plan)
    async with item.store.engine.begin() as db:
        await db.execute(
            update(BenchmarkEvaluationControl)
            .where(
                BenchmarkEvaluationControl.id == item.owner.control_id,
            )
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovered = await item.store.claim(
        item.owner.workspace_id, item.owner.trial_id, item.owner.control_id, uuid.uuid4()
    )
    with pytest.raises(ProtocolError):
        await item.inputs.release()
    fresh = RetainedInputs(item.store, recovered, item.policy)
    await fresh.release()
    await item.store.transition(recovered, "cleaning", "finished")
    assert not item.path.exists() and (await input_rows(item))[0].state == "released"


async def test_unreleased_files_prevent_terminal_cleanup_and_storage_mapping_drift(retained):
    item = retained
    await item.admit()
    await item.inputs.materialize(item.plan)
    await item.store.transition(item.owner, "planning", "cleaning")
    with pytest.raises(ProtocolError) as pending:
        await item.store.transition(item.owner, "cleaning", "finished")
    assert pending.value.code == "CLEANUP_PENDING"
    other = RetainedInputs(item.store, item.owner, RuntimePolicy(PathMapping("/other", "/other")))
    with pytest.raises(ProtocolError) as mapping:
        await other.release()
    assert mapping.value.code == "INPUT_STORAGE_UNAVAILABLE" and item.path.exists()
    await item.inputs.release()
    await item.store.transition(item.owner, "cleaning", "finished")


async def test_input_cleanup_waits_for_every_owned_resource_lease(retained):
    item = retained
    await item.admit()
    await item.inputs.materialize(item.plan)
    session_id, lease_id = uuid.uuid4(), uuid.uuid4()
    async with AsyncSession(item.store.engine) as db:
        db.add(
            BenchmarkEnvironmentSession(
                id=session_id,
                trial_id=item.owner.trial_id,
                workspace_id=item.owner.workspace_id,
                control_id=item.owner.control_id,
                origin_generation=1,
                purpose="evaluation",
                plan_digest="0" * 64,
                private_plan=item.store.private.seal("session:" + str(session_id), {}),
            )
        )
        await db.flush()
        db.add(
            BenchmarkResourceLease(
                id=lease_id,
                session_id=session_id,
                trial_id=item.owner.trial_id,
                workspace_id=item.owner.workspace_id,
                origin_generation=1,
                logical_id="grader",
                provider_id="fixture.rootless",
                kind="container",
                state="allocated",
                private_value=item.store.private.seal("lease:" + str(lease_id), {}),
            )
        )
        await db.commit()
    await item.store.transition(item.owner, "planning", "cleaning")
    with pytest.raises(ProtocolError) as pending:
        await item.inputs.release()
    assert pending.value.code == "CLEANUP_PENDING" and item.path.exists()
    async with item.store.engine.begin() as db:
        await db.execute(
            update(BenchmarkResourceLease)
            .where(BenchmarkResourceLease.id == lease_id)
            .values(state="released")
        )
    await item.inputs.release()
    await item.store.transition(item.owner, "cleaning", "finished")


async def test_evidence_lookup_reuses_the_fenced_connection_in_a_single_slot_pool(retained):
    item = retained
    await item.admit()
    previous = item.store.engine
    single = create_async_engine(previous.url, pool_size=1, max_overflow=0, pool_timeout=0.3)
    item.store.engine = single
    try:
        await asyncio.wait_for(item.inputs.materialize(item.plan), 5)
        assert item.path.read_bytes() == item.data
        await clean(item)
    finally:
        item.store.engine = previous
        await single.dispose()


@pytest.mark.parametrize(
    "values", [{"artifact_id": "changed"}, {"size_bytes": 1}, {"state": "reserved"}]
)
async def test_materialized_identity_and_forward_state_are_database_enforced(retained, values):
    item = retained
    await item.admit()
    await item.inputs.materialize(item.plan)
    async with item.store.engine.begin() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(update(BenchmarkEvaluationInput).values(**values))
    await clean(item)


async def test_checkpoint_selection_cannot_introduce_foreign_references(retained):
    item = retained
    scope = OwnerScope(str(item.owner.workspace_id), str(item.owner.trial_id), str(uuid.uuid4()), 1)
    with pytest.raises(ProtocolError):
        item.inputs.plan(
            {"checkpoint": {"artifacts": [item.ref]}},
            {"resources": [{"config": {"retained_inputs": ["foreign"]}}]},
            scope,
        )
    assert (
        item.inputs.plan(
            {"checkpoint": {"artifacts": [item.ref]}}, {"resources": []}, scope
        ).disk_mb
        == 0
    )
    changed = deepcopy(item.ref)
    changed["size_bytes"] = 1024**3 + 1
    with pytest.raises(ProtocolError) as large:
        item.inputs.plan(
            {"checkpoint": {"artifacts": [changed]}},
            {"resources": [{"config": {"retained_inputs": ["checkpoint"]}}]},
            scope,
        )
    assert large.value.code == "ARTIFACT_TOO_LARGE"


async def test_schema_rollback_cannot_discard_live_file_cleanup_intents(retained):
    item = retained
    await item.admit()
    await item.inputs.materialize(item.plan)
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "downgrade", "bg10"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0 and "Release retained evaluation inputs" in result.stderr
    async with item.store.engine.connect() as db:
        assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "bg11"
    assert item.path.read_bytes() == item.data and (await input_rows(item))[0].state == "ready"
    await clean(item)
