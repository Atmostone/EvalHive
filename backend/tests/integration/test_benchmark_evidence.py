"""Real SQL evidence receipts and optional isolated MinIO acceptance; no owner data."""

import asyncio
import hashlib
import io
import os
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from app.benchmarks.evidence import EvidenceStore
from app.benchmarks.private_store import PrivateStore
from app.benchmarks.trials import TrialStore
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_trial import BenchmarkEnvironmentSession, BenchmarkTrial
from app.runtime.broker import ExportGrant
from app.runtime.managed_spec import OwnerScope
from evalhive_benchmark import ProtocolError
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

WORKSPACE = uuid.UUID("00000000-0000-0000-0000-000000000002")


class Response(io.BytesIO):
    released = False

    def release_conn(self):
        self.released = True


class MemoryObjects:
    def __init__(self):
        self.objects, self.puts = {}, 0
        self.fail, self.bad_stat, self.after_put = False, False, None

    def put_object(self, bucket, key, stream, size, **kwargs):
        self.puts += 1
        if self.fail:
            raise RuntimeError("fixture storage failure: private internal details")
        data = stream.read()
        assert len(data) == size
        self.objects[bucket, key] = (data, kwargs["metadata"])
        if self.after_put:
            self.after_put()

    def stat_object(self, bucket, key):
        data, metadata = self.objects[bucket, key]
        return SimpleNamespace(
            size=len(data) + int(self.bad_stat),
            metadata={
                "x-amz-meta-sha256": metadata["sha256"],
            },
        )

    def get_object(self, bucket, key):
        self.response = Response(self.objects[bucket, key][0])
        return self.response


@pytest_asyncio.fixture
async def fixture(_engine, db_session):
    store = TrialStore(_engine, PrivateStore("isolated-evidence-fixture"))
    trial = await store.create(
        WORKSPACE,
        snapshot={"case": "evidence-fixture"},
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    owner = await store.claim(WORKSPACE, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    scope = OwnerScope(str(WORKSPACE), str(trial.id), str(uuid.uuid4()), 1)
    async with AsyncSession(_engine) as db:
        db.add(
            BenchmarkEnvironmentSession(
                id=uuid.UUID(scope.session_id),
                trial_id=trial.id,
                workspace_id=WORKSPACE,
                origin_generation=1,
                purpose="execution",
                plan_digest="0" * 64,
                private_plan=store.private.seal("session:" + scope.session_id, {"fixture": True}),
            )
        )
        await db.commit()
    client = MemoryObjects()
    evidence = EvidenceStore(store, client, bucket="fixture-evidence")
    return store, owner, scope, evidence, client


async def chunks(*values):
    for value in values:
        yield value


def grant(visibility="user", **kwargs):
    return ExportGrant("result", "/native-output", visibility, kwargs.get("limit", 1024))


async def rows(store):
    async with AsyncSession(store.engine) as db:
        return (await db.scalars(select(BenchmarkArtifact))).all()


async def test_immutable_receipt_and_verified_stream(fixture):
    store, owner, scope, evidence, client = fixture
    artifact = await evidence.sink(owner).save(scope, grant(), chunks(b"native ", b"evidence"))
    assert artifact["digest"] == "sha256:" + hashlib.sha256(b"native evidence").hexdigest()
    assert artifact["size_bytes"] == 15 and artifact["visibility"] == "user"
    assert artifact["uri"].startswith("artifact:") and "/native-output" not in artifact["uri"]
    async with evidence.open_verified(
        WORKSPACE, owner.trial_id, artifact, visibilities={"user"}
    ) as stream:
        assert stream.read() == b"native evidence"
    assert client.response.closed and client.response.released
    again = await evidence.sink(owner).save(scope, grant(), chunks(b"native evidence"))
    assert again == artifact and len(await rows(store)) == 1
    async with AsyncSession(store.engine) as db:
        row = await db.scalar(
            select(BenchmarkArtifact).options(undefer(BenchmarkArtifact.private_locator))
        )
        assert b"benchmarks/" not in row.private_locator


async def test_changed_capture_cannot_replace_first_receipt(fixture):
    store, owner, scope, evidence, _ = fixture
    original = await evidence.sink(owner).save(scope, grant(), chunks(b"first"))
    with pytest.raises(ProtocolError, match="already names different bytes"):
        await evidence.sink(owner).save(scope, grant(), chunks(b"second"))
    async with evidence.open_verified(
        WORKSPACE, owner.trial_id, original, visibilities={"user"}
    ) as stream:
        assert stream.read() == b"first"
    assert len(await rows(store)) == 1


@pytest.mark.parametrize("failure", ["oversized", "upload", "stat", "stream"])
async def test_failed_transfer_never_publishes_receipt(fixture, failure):
    store, owner, scope, evidence, client = fixture
    client.fail, client.bad_stat = failure == "upload", failure == "stat"

    async def broken():
        yield b"first"
        raise OSError("fixture stream interrupted")

    with pytest.raises((ProtocolError, OSError)):
        await evidence.sink(owner).save(
            scope,
            grant(limit=1 if failure == "oversized" else 1024),
            broken() if failure == "stream" else chunks(b"data"),
        )
    assert await rows(store) == []
    if failure in {"stream", "oversized"}:
        assert client.puts == 0


@pytest.mark.parametrize("change", ["workspace", "trial", "session", "generation"])
async def test_foreign_origin_rejected_before_storage(fixture, change):
    store, owner, scope, evidence, client = fixture
    scope = replace(
        scope,
        **({"generation": 2} if change == "generation" else {change + "_id": str(uuid.uuid4())}),
    )
    with pytest.raises(ProtocolError):
        await evidence.sink(owner).save(scope, grant(), chunks(b"data"))
    assert client.puts == 0 and await rows(store) == []


@pytest.mark.parametrize("visibility", ["private", "evaluator"])
async def test_private_evidence_cannot_be_read_as_public(fixture, visibility):
    _, owner, scope, evidence, _ = fixture
    artifact = await evidence.sink(owner).save(
        scope, grant(visibility), chunks(b"hidden native data")
    )
    for candidate in [artifact, {**artifact, "visibility": "user"}]:
        with pytest.raises(ProtocolError):
            await evidence.resolve(
                WORKSPACE, owner.trial_id, candidate, visibilities={"user", "agent"}
            )
    async with evidence.open_verified(
        WORKSPACE, owner.trial_id, artifact, visibilities={visibility}
    ) as stream:
        assert stream.read() == b"hidden native data"


@pytest.mark.parametrize("change", ["workspace", "trial", "uri", "digest", "size"])
async def test_reference_cannot_be_forged_or_moved(fixture, change):
    _, owner, scope, evidence, _ = fixture
    artifact = await evidence.sink(owner).save(scope, grant(), chunks(b"data"))
    workspace, trial = WORKSPACE, owner.trial_id
    if change == "workspace":
        workspace = uuid.uuid4()
    if change == "trial":
        trial = uuid.uuid4()
    if change == "uri":
        artifact["uri"] = "artifact:" + str(uuid.uuid4())
    if change == "digest":
        artifact["digest"] = "sha256:" + "a" * 64
    if change == "size":
        artifact["size_bytes"] += 1
    with pytest.raises(ProtocolError):
        await evidence.resolve(workspace, trial, artifact, visibilities={"user"})


@pytest.mark.parametrize("mutation", ["missing", "size", "digest"])
async def test_missing_or_corrupt_object_is_not_returned(fixture, mutation):
    _, owner, scope, evidence, client = fixture
    artifact = await evidence.sink(owner).save(scope, grant(), chunks(b"data"))
    key, (data, metadata) = next(iter(client.objects.items()))
    if mutation == "missing":
        del client.objects[key]
    else:
        client.objects[key] = (b"bad" if mutation == "size" else b"evil", metadata)
    with pytest.raises(ProtocolError):
        async with evidence.open_verified(
            WORKSPACE, owner.trial_id, artifact, visibilities={"user"}
        ):
            pytest.fail("Corrupt evidence reached the consumer")


async def test_owner_loss_during_upload_cannot_publish_receipt(fixture):
    store, owner, scope, evidence, client = fixture
    entered, release = threading.Event(), threading.Event()
    client.after_put = lambda: (entered.set(), release.wait(5))
    task = asyncio.create_task(evidence.sink(owner).save(scope, grant(), chunks(b"data")))
    assert await asyncio.to_thread(entered.wait, 5)
    async with store.engine.begin() as connection:
        await connection.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    release.set()
    with pytest.raises(ProtocolError) as error:
        await task
    assert error.value.code == "STALE_GENERATION" and await rows(store) == []


async def test_cancellation_keeps_upload_spool_alive_but_publishes_nothing(fixture):
    store, owner, scope, evidence, client = fixture
    entered, release = threading.Event(), threading.Event()
    client.after_put = lambda: (entered.set(), release.wait(5))
    task = asyncio.create_task(evidence.sink(owner).save(scope, grant(), chunks(b"data")))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await rows(store) == []


async def test_evidence_record_is_database_immutable(fixture):
    store, owner, scope, evidence, _ = fixture
    await evidence.sink(owner).save(scope, grant(), chunks(b"data"))
    async with store.engine.begin() as connection:
        with pytest.raises(DBAPIError, match="immutable"):
            await connection.execute(update(BenchmarkArtifact).values(visibility="private"))


async def test_recovery_can_privately_preserve_old_origin(fixture):
    store, owner, scope, evidence, _ = fixture
    async with store.engine.begin() as connection:
        await connection.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovery = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    artifact = await evidence.sink(recovery).save(scope, grant("private"), chunks(b"preserved"))
    assert artifact["visibility"] == "private" and len(await rows(store)) == 1


async def test_recovery_cannot_declassify_old_origin(fixture):
    store, owner, scope, evidence, client = fixture
    async with store.engine.begin() as connection:
        await connection.execute(
            update(BenchmarkTrial)
            .where(BenchmarkTrial.id == owner.trial_id)
            .values(controller_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovery = await store.claim(WORKSPACE, owner.trial_id, uuid.uuid4())
    with pytest.raises(ProtocolError) as error:
        await evidence.sink(recovery).save(scope, grant("user"), chunks(b"preserved"))
    assert error.value.code == "RECOVERY_PRIVATE_ONLY" and client.puts == 0


@pytest.mark.parametrize("output_stage", ["capture", "evaluation"])
async def test_required_output_coverage_and_receipts_are_verified(fixture, output_stage):
    store, owner, scope, evidence, _ = fixture
    # Install an independent immutable fixture plan with a required output.
    async with AsyncSession(store.engine) as db:
        session_id = uuid.uuid4()
        db.add(
            BenchmarkEnvironmentSession(
                id=session_id,
                trial_id=owner.trial_id,
                workspace_id=WORKSPACE,
                origin_generation=1,
                purpose="execution",
                plan_digest="a" * 64,
                private_plan=store.private.seal(
                    "session:" + str(session_id),
                    {
                        "registration": {
                            "output_plan": [
                                {"id": "result", "required": True, "stage": output_stage}
                            ]
                        },
                    },
                ),
            )
        )
        await db.commit()
    if output_stage == "evaluation":
        await evidence.validate_capture(owner, {"artifacts": []})
    with pytest.raises(ProtocolError) as missing:
        await evidence.validate_capture(owner, {"artifacts": []}, stage=output_stage)
    assert missing.value.code == "ARTIFACT_MISSING"
    wrong_session = await evidence.sink(owner).save(scope, grant(), chunks(b"wrong result"))
    with pytest.raises(ProtocolError) as foreign:
        await evidence.validate_capture(owner, {"artifacts": [wrong_session]}, stage=output_stage)
    assert foreign.value.code == "ARTIFACT_MISSING"
    artifact = await evidence.sink(owner).save(
        replace(scope, session_id=str(session_id)), grant(), chunks(b"result")
    )
    await evidence.validate_capture(owner, {"artifacts": [artifact]}, stage=output_stage)
    with pytest.raises(ProtocolError) as repeated:
        await evidence.validate_capture(owner, {"artifacts": [artifact, artifact]})
    assert repeated.value.code == "ARTIFACT_CONFLICT"


async def test_usage_rebuilds_from_scoped_immutable_journal_without_double_charge(fixture):
    from app.benchmarks.usage import trial_usage

    store, owner, _, _, _ = fixture
    actor = uuid.uuid4()
    await store.register_stream(owner, actor)
    events = [
        {
            "protocol_version": "1.0",
            "type": "event",
            "id": "usage-" + str(sequence),
            "scope": {
                "workspace_id": str(WORKSPACE),
                "trial_id": str(owner.trial_id),
                "generation": 1,
            },
            "seq": sequence,
            "source": "native-simulator",
            "event": "usage",
            "data": {
                "usage_id": "call-1",
                "role": "user",
                "model": "fixture",
                "purpose": "simulation",
                "mode": "cumulative",
                "input_tokens": tokens,
            },
        }
        for sequence, tokens in enumerate([2, 5, 5])
    ]
    await store.ingest(owner, actor, events)
    await store.ingest(owner, actor, events)
    summary = await trial_usage(store, WORKSPACE, owner.trial_id)
    group = summary["groups"][0]
    assert group["calls"] == 1 and group["tokens"]["input_tokens"]["value"] == 5
    assert group["cost"]["amount"] is None and group["cost"]["unknown_calls"] == 1
    assert not summary["measurement_complete"]
    with pytest.raises(ProtocolError) as foreign:
        await trial_usage(store, uuid.uuid4(), owner.trial_id)
    assert foreign.value.code == "TRIAL_NOT_FOUND"


async def test_broker_publication_reuses_its_fenced_connection(fixture):
    from dataclasses import asdict

    from app.benchmarks.resource_authority import ActorPolicy, SqlResourceAuthority, install_session
    from app.runtime.admission import GroupPolicy, HostCapacity, ResourceDemand
    from app.runtime.broker import ResourceLease
    from app.runtime.docker_provider import RootlessDockerProvider
    from app.runtime.managed_spec import ContainerSpec
    from sqlalchemy.ext.asyncio import create_async_engine

    from tests.integration.test_benchmark_trials import (
        IMAGE,
        MANIFEST,
        POLICY,
        PROVIDER,
        QUOTA,
        FakeRuntime,
        request,
    )

    store, owner, scope, evidence, _ = fixture

    async def measure():
        return HostCapacity(8192, 8192, 4, datetime.now(UTC).isoformat())

    await store.reserve(
        owner,
        demands=(ResourceDemand("environment", QUOTA, 256, 0.1, 10),),
        native_resources={},
        measure=measure,
        runtime_policy=POLICY,
        group_policy=GroupPolicy(),
    )
    scope = replace(scope, session_id=str(uuid.uuid4()))
    handle, actor = str(uuid.uuid4()), uuid.uuid4()
    lease = ResourceLease(
        handle, scope, "container", ContainerSpec(scope, handle, IMAGE, quota=QUOTA)
    )
    await install_session(
        store,
        owner,
        scope,
        leases={"environment": (PROVIDER, lease)},
        actors={
            actor: ActorPolicy((PROVIDER,), ("environment",), ("planning",), ("allocate", "export"))
        },
        outputs={"environment": {"result": {"actors": [str(actor)], "grant": asdict(grant())}}},
    )

    class ArchiveRuntime(FakeRuntime):
        def container(self, native):
            return SimpleNamespace(get_archive=lambda path: (iter([b"native archive"]), {}))

    # A second hidden checkout deadlocks this export. SQL publication must use
    # the resource call's already checked-out connection.
    pooled = create_async_engine(store.engine.url, pool_size=1, max_overflow=0, pool_timeout=1)
    previous, store.engine = store.engine, pooled
    try:
        authority = SqlResourceAuthority(store, owner, scope.session_id, actor, PROVIDER)
        provider = RootlessDockerProvider(
            ArchiveRuntime(), authority, MANIFEST, artifacts=evidence.sink(owner)
        )
        await provider.handle(request(scope))
        result = await asyncio.wait_for(
            provider.handle(
                request(scope, "export", handle=handle, payload={"output_id": "result"})
            ),
            8,
        )
        async with evidence.open_verified(
            WORKSPACE, owner.trial_id, result["artifact"], visibilities={"user"}
        ) as stream:
            assert stream.read() == b"native archive"
    finally:
        store.engine = previous
        await pooled.dispose()


@pytest.mark.skipif(
    not os.getenv("EVALHIVE_EVIDENCE_MINIO"), reason="Isolated MinIO fixture not configured"
)
async def test_real_minio_roundtrip(fixture):
    import urllib3
    from minio import Minio

    store, owner, scope, _, _ = fixture
    client = Minio(
        os.environ["EVALHIVE_EVIDENCE_MINIO"],
        access_key="fixture-evidence",
        secret_key="fixture-evidence-password",
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3, read=5), retries=False),
    )
    bucket = "evidence-" + uuid.uuid4().hex
    await asyncio.to_thread(client.make_bucket, bucket)
    evidence = EvidenceStore(store, client, bucket=bucket)
    data = b"native archive bytes" * 4096
    artifact = await evidence.sink(owner).save(
        scope, grant(limit=len(data)), chunks(data[:10000], data[10000:])
    )
    async with evidence.open_verified(
        WORKSPACE, owner.trial_id, artifact, visibilities={"user"}
    ) as stream:
        assert stream.read() == data
