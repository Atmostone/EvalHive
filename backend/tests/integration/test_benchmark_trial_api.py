"""Public benchmark reads preserve tenant scope, private evidence and lifecycle facts."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from app.api.benchmark_trials import get_trial_store
from app.models.benchmark_artifact import BenchmarkArtifact
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_trial import BenchmarkEnvironmentSession, BenchmarkTrial
from app.models.workspace import Workspace, WorkspaceMember
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest_asyncio.fixture
async def public_trial(auth_client, _engine):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    store = get_trial_store()
    trial = await store.create(
        workspace_id,
        snapshot={"benchmark": {"id": "fixture", "version": "1.0"}},
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    owner = await store.claim(workspace_id, trial.id, uuid.uuid4())
    await store.transition(owner, "created", "planning")
    await store.note(owner, "configuration.applied", private={"hidden": "private-evaluator-data"})
    async with AsyncSession(_engine) as db:
        session_id = uuid.uuid4()
        db.add(
            BenchmarkEnvironmentSession(
                id=session_id,
                trial_id=trial.id,
                workspace_id=workspace_id,
                origin_generation=1,
                purpose="execution",
                state="planned",
                plan_digest="a" * 64,
                private_plan=store.private.seal(
                    "session:" + str(session_id), {"hidden": "private-evaluator-data"}
                ),
            )
        )
        await db.flush()
        for visibility in ("agent", "user", "evaluator", "private"):
            artifact_id = uuid.uuid4()
            db.add(
                BenchmarkArtifact(
                    id=artifact_id,
                    trial_id=trial.id,
                    workspace_id=workspace_id,
                    session_id=session_id,
                    origin_generation=1,
                    logical_id=visibility + ".artifact",
                    visibility=visibility,
                    digest="sha256:" + "b" * 64,
                    size_bytes=0,
                    media_type="application/x-tar",
                    private_locator=store.private.seal(
                        "artifact:" + str(artifact_id), {"key": "private-evaluator-data"}
                    ),
                )
            )
        evaluation_id = uuid.uuid4()
        db.add(
            BenchmarkEvaluationAttempt(
                id=evaluation_id,
                trial_id=trial.id,
                workspace_id=workspace_id,
                kind="initial",
                origin_generation=1,
                controller_id=owner.controller_id,
                worker_actor_id=uuid.uuid4(),
                input_digest="c" * 64,
                private_inputs=store.private.seal(
                    "evaluation-inputs:" + str(evaluation_id), {"hidden": "private-evaluator-data"}
                ),
                status="completed",
                verdict="incorrect",
                success=False,
                metrics=[
                    {
                        "name": "native.reward",
                        "value": 0,
                        "direction": "higher",
                        "aggregation": "native",
                    }
                ],
                private_result=store.private.seal(
                    "evaluation-result:" + str(evaluation_id), {"hidden": "private-evaluator-data"}
                ),
                result_digest="d" * 64,
                finished_at=datetime.now(UTC),
            )
        )
        await db.commit()
    return store, trial, workspace_id


async def test_public_reads_keep_private_evidence_and_checker_state_separate(
    auth_client, public_trial
):
    _, trial, _ = public_trial
    base = "/api/benchmark-trials/" + str(trial.id)
    listing = await auth_client.get("/api/benchmark-trials")
    assert listing.status_code == 200 and [row["id"] for row in listing.json()] == [str(trial.id)]
    detail = await auth_client.get(base)
    assert detail.status_code == 200
    assert detail.json()["execution_status"] == "pending"
    assert detail.json()["cleanup_status"] == "pending"
    events = await auth_client.get(base + "/events?limit=1")
    assert events.status_code == 200 and len(events.json()["events"]) == 1
    cursor = events.json()["next_seq"]
    remainder = await auth_client.get(base + f"/events?after_seq={cursor}")
    assert all(row["seq"] > cursor for row in remainder.json()["events"])
    evaluations = await auth_client.get(base + "/evaluations")
    assert evaluations.status_code == 200
    assert [(row["status"], row["success"], row["verdict"]) for row in evaluations.json()] == [
        ("completed", False, "incorrect")
    ]
    assert evaluations.json()[0]["metrics"][0]["value"] == 0
    artifacts = await auth_client.get(base + "/artifacts")
    assert artifacts.status_code == 200
    assert {row["artifact"]["visibility"] for row in artifacts.json()} == {"user", "agent"}
    for response in (listing, detail, events, remainder, evaluations, artifacts):
        assert "private-evaluator-data" not in response.text
        assert "private_inputs" not in response.text and "private_result" not in response.text
        assert "private_locator" not in response.text and "private_plan" not in response.text
    assert (await auth_client.get(base + "/events?limit=501")).status_code == 422
    assert (await auth_client.get(base + "/events?after_seq=-2")).status_code == 422


async def test_other_workspace_trials_are_indistinguishable_from_missing(
    auth_client, public_trial, _engine
):
    store, _, workspace_id = public_trial
    async with AsyncSession(_engine, expire_on_commit=False) as db:
        source = await db.get(Workspace, workspace_id)
        other = Workspace(
            name="Other benchmark workspace",
            slug="other-" + uuid.uuid4().hex,
            created_by=source.created_by,
        )
        db.add(other)
        await db.commit()
        other_id = other.id
    hidden = await store.create(
        other_id, snapshot={}, deadline_at=datetime.now(UTC) + timedelta(minutes=5)
    )
    base = "/api/benchmark-trials/" + str(hidden.id)
    for suffix in ("", "/events", "/evaluations", "/artifacts", "/usage"):
        response = await auth_client.get(base + suffix)
        assert response.status_code == 404
    assert (await auth_client.post(base + "/cancel")).status_code == 404
    listing = await auth_client.get("/api/benchmark-trials")
    assert str(hidden.id) not in listing.text


async def test_cancel_is_monotonic_and_viewers_cannot_control(auth_client, public_trial, _engine):
    _, trial, workspace_id = public_trial
    base = "/api/benchmark-trials/" + str(trial.id)
    first = await auth_client.post(base + "/cancel")
    assert first.status_code == 200 and first.json()["requested"]
    at = (await auth_client.get(base)).json()["cancel_requested_at"]
    assert at
    assert (await auth_client.post(base + "/cancel")).json()["requested"]
    assert (await auth_client.get(base)).json()["cancel_requested_at"] == at
    async with AsyncSession(_engine) as db:
        member = await db.scalar(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        )
        member.role = "viewer"
        await db.commit()
    assert (await auth_client.get(base)).status_code == 200
    assert (await auth_client.post(base + "/cancel")).status_code == 403
    async with AsyncSession(_engine) as db:
        member = await db.scalar(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id)
        )
        member.role = "owner"
        row = await db.get(BenchmarkTrial, trial.id)
        row.phase, row.finished_at = "finished", datetime.now(UTC)
        await db.commit()
    assert (await auth_client.post(base + "/cancel")).json() == {"requested": False}
