"""Public downloads verify bytes and never expose private evidence or locators."""

import hashlib
import uuid

import pytest
from app.api.benchmark_trials import get_public_evidence
from app.benchmarks.evidence import EvidenceStore
from app.main import app
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_trial import BenchmarkEnvironmentSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.test_benchmark_evidence import MemoryObjects
from tests.integration.test_benchmark_trial_api import public_trial as public_trial


@pytest.mark.parametrize("corrupt", [False, True])
async def test_download_checks_visibility_and_full_digest_before_response(
    auth_client, public_trial, tmp_path, corrupt
):
    store, trial, workspace_id = public_trial
    body = b"fixture artifact bytes\x00\xff\n"
    client = MemoryObjects()
    key, bucket = "opaque-private-object-key", "fixture-evidence"
    client.objects[bucket, key] = (body if not corrupt else body + b"corrupt", {"sha256": "unused"})
    evidence = EvidenceStore(store, client, bucket=bucket, spool_dir=tmp_path)
    artifact_id = uuid.uuid4()
    async with AsyncSession(store.engine) as db:
        session = await db.scalar(
            select(BenchmarkEnvironmentSession).where(
                BenchmarkEnvironmentSession.trial_id == trial.id
            )
        )
        db.add(
            BenchmarkArtifact(
                id=artifact_id,
                workspace_id=workspace_id,
                trial_id=trial.id,
                session_id=session.id,
                origin_generation=1,
                logical_id="download.fixture",
                visibility="user",
                digest="sha256:" + hashlib.sha256(body).hexdigest(),
                size_bytes=len(body),
                media_type="text/plain",
                private_locator=store.private.seal(
                    "artifact:" + str(artifact_id), {"bucket": bucket, "key": key}
                ),
            )
        )
        private_id = await db.scalar(
            select(BenchmarkArtifact.id).where(
                BenchmarkArtifact.trial_id == trial.id, BenchmarkArtifact.visibility == "private"
            )
        )
        await db.commit()
    app.dependency_overrides[get_public_evidence] = lambda: evidence
    try:
        base = f"/api/benchmark-trials/{trial.id}/artifacts/"
        response = await auth_client.get(base + str(artifact_id) + "/download")
        assert response.status_code == (409 if corrupt else 200), response.text
        if not corrupt:
            assert response.content == body and client.response.closed and client.response.released
            assert response.headers["content-disposition"].startswith("attachment;")
            assert response.headers["x-content-type-options"] == "nosniff"
        assert key.encode() not in response.content
        for path in (
            base + str(private_id) + "/download",
            base + str(uuid.uuid4()) + "/download",
            f"/api/benchmark-trials/{uuid.uuid4()}/artifacts/{artifact_id}/download",
        ):
            assert (await auth_client.get(path)).status_code == 404
        assert list(tmp_path.iterdir()) == []
    finally:
        app.dependency_overrides.pop(get_public_evidence, None)
