"""Bounded object storage with immutable, scoped, digest-checked evidence receipts."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy

from evalhive_benchmark import ProtocolError, validate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.trials import current_trial, failure
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_trial import BenchmarkEnvironmentSession

MAX_EVIDENCE_BYTES = 1024 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def reference(row):
    return validate(
        "artifact",
        {
            "id": row.logical_id,
            "uri": "artifact:" + str(row.id),
            "digest": row.digest,
            "size_bytes": row.size_bytes,
            "visibility": row.visibility,
            "media_type": row.media_type,
        },
    )


class EvidenceStore:
    """Trusted host storage. Workers receive opaque references, never bucket keys.

    The client is supplied by deployment composition. This class never discovers
    credentials or falls back to a different bucket. Bucket creation is an
    installation step, not a worker permission.
    """

    def __init__(self, store, client, *, bucket, spool_dir=None):
        if not isinstance(bucket, str) or not bucket:
            raise ValueError("An installed evidence bucket is required")
        self.store, self.client, self.bucket, self.spool_dir = store, client, bucket, spool_dir

    def sink(self, owner):
        return EvidenceSink(self, owner)

    async def validate_capture(self, owner, capture, *, stage="capture"):
        """Prove references and stage-specific coverage before accepting a verdict."""
        if stage not in {"capture", "evaluation"}:
            raise ValueError("Unknown trusted capture stage")
        supplied = capture["artifacts"]
        identities = [artifact["id"] for artifact in supplied]
        if len(identities) != len(set(identities)):
            raise failure("ARTIFACT_CONFLICT", "Capture repeats an evidence identity", "recording")
        async with AsyncSession(self.store.engine) as db:
            await current_trial(db, owner)
            sessions = (
                await db.scalars(
                    select(BenchmarkEnvironmentSession)
                    .where(
                        BenchmarkEnvironmentSession.trial_id == owner.trial_id,
                        BenchmarkEnvironmentSession.workspace_id == owner.workspace_id,
                        BenchmarkEnvironmentSession.purpose == "execution",
                    )
                    .options(undefer(BenchmarkEnvironmentSession.private_plan))
                )
            ).all()
            required = set()
            for session in sessions:
                plan = self.store.private.open("session:" + str(session.id), session.private_plan)
                if plan.get("outputs") and "output_plan" not in plan.get("registration", {}):
                    raise failure(
                        "CAPTURE_POLICY_UNAVAILABLE",
                        "Saved output policy is incomplete",
                        "recording",
                    )
                for output in plan.get("registration", {}).get("output_plan", []):
                    if output["required"] and (
                        stage == "evaluation" or output.get("stage", "capture") == "capture"
                    ):
                        required.add(
                            "artifact:" + str(uuid.uuid5(session.id, "artifact:" + output["id"]))
                        )
            if required - {artifact["uri"] for artifact in supplied}:
                raise failure("ARTIFACT_MISSING", "Capture omits required evidence", "recording")
        for artifact in supplied:
            async with self.open_verified(
                owner.workspace_id,
                owner.trial_id,
                artifact,
                visibilities={"agent", "user", "evaluator", "private"},
            ):
                pass

    async def resolve(self, workspace_id, trial_id, artifact, *, visibilities):
        """Authorize the complete reference before opening a private locator.

        Callers grant visibility explicitly. Public downloads should grant only
        user/agent visibility; evaluators may grant evaluator/private as needed.
        """
        validate("artifact", artifact)
        try:
            if not artifact["uri"].startswith("artifact:"):
                raise ValueError
            artifact_id = uuid.UUID(artifact["uri"][len("artifact:") :])
        except ValueError:
            raise failure(
                "ARTIFACT_UNAVAILABLE", "Evidence reference is unavailable", "recording"
            ) from None
        async with AsyncSession(self.store.engine, expire_on_commit=False) as db:
            row = await db.scalar(
                select(BenchmarkArtifact)
                .where(
                    BenchmarkArtifact.id == artifact_id,
                    BenchmarkArtifact.workspace_id == workspace_id,
                    BenchmarkArtifact.trial_id == trial_id,
                )
                .options(undefer(BenchmarkArtifact.private_locator))
            )
            if row is None or row.visibility not in visibilities or reference(row) != artifact:
                raise failure(
                    "ARTIFACT_UNAVAILABLE", "Evidence reference is unavailable", "recording"
                )
            locator = self.store.private.open("artifact:" + str(row.id), row.private_locator)
            if locator["bucket"] != self.bucket:
                raise failure(
                    "ARTIFACT_STORAGE_UNAVAILABLE",
                    "Installed evidence storage differs",
                    "recording",
                )
            return locator

    @asynccontextmanager
    async def open_verified(self, workspace_id, trial_id, artifact, *, visibilities):
        """Spool and verify before handing any retained bytes to a consumer."""
        artifact = deepcopy(artifact)
        locator = await self.resolve(workspace_id, trial_id, artifact, visibilities=visibilities)
        if artifact["size_bytes"] > MAX_EVIDENCE_BYTES:
            raise failure("ARTIFACT_TOO_LARGE", "Evidence exceeds storage policy", "recording")
        with tempfile.TemporaryFile(dir=self.spool_dir) as spool:

            def download():
                response = None
                try:
                    stat = self.client.stat_object(self.bucket, locator["key"])
                    if stat.size != artifact["size_bytes"]:
                        raise failure(
                            "ARTIFACT_CORRUPT",
                            "Evidence size does not match its receipt",
                            "recording",
                        )
                    response = self.client.get_object(self.bucket, locator["key"])
                    size, digest = 0, hashlib.sha256()
                    while chunk := response.read(CHUNK_BYTES):
                        size += len(chunk)
                        if size > artifact["size_bytes"]:
                            raise failure(
                                "ARTIFACT_CORRUPT", "Evidence exceeds its receipt", "recording"
                            )
                        digest.update(chunk)
                        spool.write(chunk)
                    if (
                        size != artifact["size_bytes"]
                        or "sha256:" + digest.hexdigest() != artifact["digest"]
                    ):
                        raise failure(
                            "ARTIFACT_CORRUPT",
                            "Evidence digest does not match its receipt",
                            "recording",
                        )
                    spool.seek(0)
                except ProtocolError:
                    raise
                except Exception:
                    raise failure(
                        "ARTIFACT_READ_FAILED", "Retained evidence is unavailable", "recording"
                    ) from None
                finally:
                    if response is not None:
                        response.close()
                        response.release_conn()

            await _io(download)
            yield spool


async def _io(operation):
    """Keep spools alive until a cancelled blocking storage call has returned.

    Deployment clients must have finite network timeouts. Cancelling an await
    does not stop a Python thread; closing its spool early corrupts in-flight I/O.
    """
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def verify_origin(db, owner, scope):
    trial, _ = await current_trial(db, owner, row_lock=True)
    if trial.phase == "finished":
        raise failure("TRIAL_FINISHED", "Finished trials cannot publish new evidence", "recording")
    if scope.workspace_id != str(owner.workspace_id) or scope.trial_id != str(owner.trial_id):
        raise failure("FOREIGN_HANDLE", "Evidence scope is unavailable", "recording")
    if scope.generation != owner.generation and trial.phase != "cleaning":
        raise failure("RECOVERY_ONLY", "Old evidence origins require recovery", "recording")
    row = await db.get(BenchmarkEnvironmentSession, uuid.UUID(scope.session_id))
    if row is None or (row.trial_id, row.workspace_id, row.origin_generation) != (
        owner.trial_id,
        owner.workspace_id,
        scope.generation,
    ):
        raise failure("FOREIGN_HANDLE", "Evidence origin is unavailable", "recording")
    return row


class EvidenceSink:
    def __init__(self, evidence, owner):
        self.evidence, self.owner = evidence, owner

    async def save(self, scope, grant, chunks, *, publication=None):
        evidence = self.evidence
        if scope.generation != self.owner.generation and grant.visibility != "private":
            raise failure(
                "RECOVERY_PRIVATE_ONLY", "Recovered evidence must remain private", "recording"
            )
        if (
            type(grant.max_archive_bytes) is not int
            or not 1 <= grant.max_archive_bytes <= MAX_EVIDENCE_BYTES
        ):
            raise failure(
                "ARTIFACT_TOO_LARGE", "Evidence grant exceeds storage policy", "recording"
            )
        # Validate identity/visibility before reading or uploading native bytes.
        validate(
            "artifact",
            {
                "id": grant.artifact_id,
                "uri": "pending",
                "digest": "sha256:" + "0" * 64,
                "size_bytes": 0,
                "visibility": grant.visibility,
            },
        )
        if publication is not None:
            await publication.publish_artifact(None, None)
        else:
            async with AsyncSession(evidence.store.engine) as db:
                await verify_origin(db, self.owner, scope)
        with tempfile.TemporaryFile(dir=evidence.spool_dir) as spool:
            size, digest = 0, hashlib.sha256()
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise failure(
                        "ARTIFACT_INCOMPLETE", "Evidence stream contains invalid bytes", "recording"
                    )
                size += len(chunk)
                if size > grant.max_archive_bytes:
                    raise failure(
                        "ARTIFACT_TOO_LARGE", "Evidence exceeded its byte grant", "recording"
                    )
                digest.update(chunk)
                spool.write(chunk)
            digest = "sha256:" + digest.hexdigest()
            identity = uuid.uuid5(uuid.UUID(scope.session_id), "artifact:" + grant.artifact_id)
            key = (
                f"benchmarks/{scope.workspace_id}/{scope.trial_id}/{scope.session_id}/"
                f"{scope.generation}/{grant.visibility}/{digest[7:]}.tar"
            )
            spool.seek(0)

            def upload():
                try:
                    evidence.client.put_object(
                        evidence.bucket,
                        key,
                        spool,
                        size,
                        content_type="application/x-tar",
                        metadata={"sha256": digest[7:]},
                        num_parallel_uploads=1,
                    )
                    stat = evidence.client.stat_object(evidence.bucket, key)
                    if stat.size != size or stat.metadata.get("x-amz-meta-sha256") != digest[7:]:
                        raise ValueError("Storage receipt does not match the uploaded evidence")
                except Exception:
                    raise failure(
                        "ARTIFACT_UPLOAD_FAILED",
                        "Evidence could not be stored completely",
                        "recording",
                    ) from None

            await _io(upload)
        artifact = validate(
            "artifact",
            {
                "id": grant.artifact_id,
                "uri": "artifact:" + str(identity),
                "digest": digest,
                "size_bytes": size,
                "visibility": grant.visibility,
                "media_type": "application/x-tar",
            },
        )
        locator = {"bucket": evidence.bucket, "key": key}
        if publication is not None:
            return await publication.publish_artifact(artifact, locator)
        async with AsyncSession(evidence.store.engine, expire_on_commit=False) as db:
            return await publish_artifact(db, evidence.store, self.owner, scope, artifact, locator)


async def publish_artifact(db, store, owner, scope, artifact, locator):
    """Commit through the resource call's existing fenced connection when supplied."""
    await verify_origin(db, owner, scope)
    identity = uuid.uuid5(uuid.UUID(scope.session_id), "artifact:" + artifact["id"])
    if artifact["uri"] != "artifact:" + str(identity):
        raise failure("ARTIFACT_CONFLICT", "Evidence identity differs from its origin", "recording")
    row = await db.get(BenchmarkArtifact, identity)
    if row is not None:
        if reference(row) != artifact:
            raise failure(
                "ARTIFACT_CONFLICT", "Evidence identity already names different bytes", "recording"
            )
        return reference(row)
    row = BenchmarkArtifact(
        id=identity,
        session_id=uuid.UUID(scope.session_id),
        trial_id=owner.trial_id,
        workspace_id=owner.workspace_id,
        origin_generation=scope.generation,
        logical_id=artifact["id"],
        digest=artifact["digest"],
        size_bytes=artifact["size_bytes"],
        visibility=artifact["visibility"],
        media_type=artifact["media_type"],
        private_locator=store.private.seal("artifact:" + str(identity), locator),
    )
    db.add(row)
    await db.commit()
    return reference(row)
