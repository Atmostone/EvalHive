"""Materialize verified checkpoint bytes as owned read-only runtime grants."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path

from evalhive_benchmark.peer import identity
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.benchmarks.evidence import CHUNK_BYTES, MAX_EVIDENCE_BYTES, _io
from app.benchmarks.trials import database_fence, failure
from app.models.benchmark_evaluation_input import BenchmarkEvaluationInput
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkHostReservation,
    BenchmarkResourceLease,
)
from app.runtime.registered_assets import RegisteredFile

MIB = 1024 * 1024


@dataclass(frozen=True)
class InputPlan:
    session_id: uuid.UUID
    artifacts: tuple[dict, ...]
    grants: dict[str, RegisteredFile]
    disk_mb: int


class RetainedInputs:
    def __init__(self, store, owner, policy, evidence=None):
        self.store, self.owner, self.policy, self.evidence = store, owner, policy, evidence

    def _id(self, artifact_id):
        return uuid.uuid5(self.owner.control_id, "retained:" + artifact_id)

    def _relative(self, session_id, input_id):
        return Path("benchmark-sessions") / str(session_id) / "retained-inputs" / str(input_id)

    def plan(self, spec, plan, scope):
        selected = set()
        for resource in plan["resources"]:
            values = resource["config"].get("retained_inputs", [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise failure("INVALID_RETAINED_INPUT", "Retained inputs must select artifact IDs")
            selected.update(values)
        available = {item["id"]: item for item in spec.get("checkpoint", {}).get("artifacts", [])}
        if selected - available.keys() or selected and self.owner.control_id is None:
            raise failure("INVALID_RETAINED_INPUT", "Retained input is outside the checkpoint")
        artifacts = tuple(deepcopy(available[key]) for key in sorted(selected))
        if any(item["size_bytes"] > MAX_EVIDENCE_BYTES for item in artifacts):
            raise failure(
                "ARTIFACT_TOO_LARGE", "Retained input exceeds storage policy", "recording"
            )
        grants = {
            item["id"]: RegisteredFile(
                str(
                    Path(self.policy.data.host)
                    / self._relative(scope.session_id, self._id(item["id"]))
                    / "payload"
                ),
                item["digest"],
            )
            for item in artifacts
        }
        # Downloads are sequential: all retained bytes plus one verified spool
        # may coexist. Neither allocation is part of a native container minimum.
        sizes_mb = [(item["size_bytes"] + MIB - 1) // MIB for item in artifacts]
        disk_mb = sum(sizes_mb) + max(sizes_mb, default=0) + len(artifacts)
        return InputPlan(uuid.UUID(scope.session_id), artifacts, grants, disk_mb)

    def _path(self, row, saved):
        if saved["data_mapping"] != asdict(self.policy.data):
            raise failure(
                "INPUT_STORAGE_UNAVAILABLE", "Retained input storage mapping differs", "cleanup"
            )
        return Path(self.policy.data.local) / self._relative(row.session_id, row.id)

    async def _active(self, db, *, phase):
        row, now = await self.store.current(db, self.owner)
        if (
            row.phase != phase
            or phase != "cleaning"
            and (row.cancel_requested_at is not None or row.deadline_at <= now)
        ):
            raise failure("INVALID_PHASE", "Retained input operation is outside its active phase")
        return row, now

    async def materialize(self, plan):
        if not plan.artifacts:
            return
        if self.evidence is None:
            raise failure(
                "ARTIFACT_STORAGE_UNAVAILABLE", "Retained evidence storage is required", "recording"
            )
        owner, store = self.owner, self.store
        async with database_fence(store.engine, owner.trial_id) as db:
            await self._active(db, phase="planning")
            reservation = await db.get(BenchmarkHostReservation, store.host_id)
            if (
                reservation is None
                or (reservation.trial_id, reservation.generation, reservation.controller_id)
                != (owner.trial_id, owner.generation, owner.controller_id)
                or (
                    reservation.budget.get("stage") != "full"
                    or reservation.budget.get("auxiliary_disk_mb", 0) != plan.disk_mb
                )
            ):
                raise failure(
                    "NO_RESERVATION", "Retained input storage requires its full host reservation"
                )
            for artifact in plan.artifacts:
                input_id = self._id(artifact["id"])
                row = await db.get(BenchmarkEvaluationInput, input_id)
                if row is not None:
                    if (
                        row.reference_hash != identity(artifact)
                        or row.session_id != plan.session_id
                        or row.state == "released"
                    ):
                        raise failure("INPUT_CONFLICT", "Retained input intent differs")
                    continue
                db.add(
                    BenchmarkEvaluationInput(
                        id=input_id,
                        control_id=owner.control_id,
                        trial_id=owner.trial_id,
                        workspace_id=owner.workspace_id,
                        session_id=plan.session_id,
                        origin_generation=owner.generation,
                        controller_id=owner.controller_id,
                        artifact_id=artifact["id"],
                        reference_hash=identity(artifact),
                        size_bytes=artifact["size_bytes"],
                        private_input=store.private.seal(
                            "evaluation-input:" + str(input_id),
                            {
                                "artifact": artifact,
                                "data_mapping": asdict(self.policy.data),
                            },
                        ),
                        state="reserved",
                    )
                )
            await db.commit()
        for artifact in plan.artifacts:
            async with database_fence(store.engine, owner.trial_id) as db:
                await self._active(db, phase="planning")
                spool_path = Path(self.policy.data.local) / self._relative(
                    plan.session_id, self._id(artifact["id"])
                )
                await db.commit()
                await _io(
                    partial(_directory, Path(self.policy.data.local), spool_path, create=True)
                )
                # Anonymous spools share the admitted storage and ownership
                # fence. A replacement cannot release their disk budget while
                # a bounded blocking download is still in flight.
                async with self.evidence.open_verified(
                    owner.workspace_id,
                    owner.trial_id,
                    artifact,
                    visibilities={"agent", "user", "evaluator", "private"},
                    spool_dir=spool_path,
                    db=db,
                ) as spool:
                    await self._active(db, phase="planning")
                    row = await db.scalar(
                        select(BenchmarkEvaluationInput)
                        .where(
                            BenchmarkEvaluationInput.id == self._id(artifact["id"]),
                        )
                        .options(undefer(BenchmarkEvaluationInput.private_input))
                    )
                    if (
                        row is None
                        or row.state == "released"
                        or row.controller_id != owner.controller_id
                    ):
                        raise failure("INPUT_CONFLICT", "Retained input ownership differs")
                    saved = store.private.open("evaluation-input:" + str(row.id), row.private_input)
                    path, ready = self._path(row, saved), row.state == "ready"
                    await db.commit()
                    # The advisory fence spans the physical write, while row
                    # locks are released so lease heartbeats can continue.
                    await _io(
                        partial(
                            _write_verified,
                            Path(self.policy.data.local),
                            path,
                            artifact,
                            spool,
                            ready=ready,
                        )
                    )
                    await self._active(db, phase="planning")
                    row.state = "ready"
                    await db.commit()

    async def release(self):
        if self.owner.control_id is None:
            return
        owner, store = self.owner, self.store
        async with database_fence(store.engine, owner.trial_id) as db:
            _, now = await self._active(db, phase="cleaning")
            live = await db.scalar(
                select(BenchmarkResourceLease.id)
                .join(BenchmarkEnvironmentSession)
                .where(
                    BenchmarkEnvironmentSession.control_id == owner.control_id,
                    BenchmarkResourceLease.state != "released",
                )
                .limit(1)
            )
            if live is not None:
                raise failure(
                    "CLEANUP_PENDING",
                    "Retained inputs remain mounted by resource leases",
                    "cleanup",
                )
            rows = (
                await db.scalars(
                    select(BenchmarkEvaluationInput)
                    .where(
                        BenchmarkEvaluationInput.control_id == owner.control_id,
                        BenchmarkEvaluationInput.state != "released",
                    )
                    .options(undefer(BenchmarkEvaluationInput.private_input))
                )
            ).all()
            for row in rows:
                saved = store.private.open("evaluation-input:" + str(row.id), row.private_input)
                path = self._path(row, saved)
                await db.commit()
                await _io(partial(_remove_owned, Path(self.policy.data.local), path))
                _, now = await self._active(db, phase="cleaning")
                row.state, row.released_at = "released", now
                await db.commit()


def _directory(root, path, *, create):
    """Walk only the fixed owned layout; links are never followed."""
    if root.resolve(strict=True) != root or not root.is_dir():
        raise OSError("Retained input root is unavailable")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if create:
            current.mkdir(mode=0o700, exist_ok=True)
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise OSError("Retained input path is not an owned directory")
    return True


def _write_verified(root, path, artifact, spool, *, ready):
    _directory(root, path, create=True)
    target, temporary = path / "payload", path / "payload.partial"
    if ready:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as saved:
            if not stat.S_ISREG(os.fstat(saved.fileno()).st_mode):
                raise OSError("Retained input is not a regular file")
            digest, size = hashlib.sha256(), 0
            while chunk := saved.read(CHUNK_BYTES):
                digest.update(chunk)
                size += len(chunk)
                if size > artifact["size_bytes"]:
                    break
            if (
                size != artifact["size_bytes"]
                or "sha256:" + digest.hexdigest() != artifact["digest"]
            ):
                raise failure(
                    "ARTIFACT_CORRUPT",
                    "Materialized evidence differs from its receipt",
                    "recording",
                )
        return
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        digest, size = hashlib.sha256(), 0
        while chunk := spool.read(CHUNK_BYTES):
            size += len(chunk)
            if size > artifact["size_bytes"]:
                raise failure(
                    "ARTIFACT_CORRUPT", "Retained evidence exceeds its receipt", "recording"
                )
            digest.update(chunk)
            output.write(chunk)
        if size != artifact["size_bytes"] or "sha256:" + digest.hexdigest() != artifact["digest"]:
            raise failure(
                "ARTIFACT_CORRUPT", "Retained evidence differs from its receipt", "recording"
            )
        output.flush()
        os.fchmod(output.fileno(), 0o444)
        os.fsync(output.fileno())
    os.replace(temporary, target)
    _sync_directory(path)


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_owned(root, path):
    if not _directory(root, path, create=False):
        return
    for name in ("payload", "payload.partial"):
        try:
            mode = (path / name).lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise OSError("Retained input cleanup found an unexpected file")
        (path / name).unlink()
    path.rmdir()
    _sync_directory(path.parent)
