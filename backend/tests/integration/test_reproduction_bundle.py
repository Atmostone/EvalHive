"""The acceptance criterion of SPA-90, executed.

«A clean checkout plus the bundle reproduces the headline numbers exactly» — so a
real experiment is run, exported, and recomputed from the archive alone. And then
the same archive is corrupted and must FAIL, because a verifier that cannot fail
is not a verifier.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.annotation import Annotation
from app.models.experiment import Experiment, ExperimentRun
from app.models.quality_record import QualityRecord
from app.models.user import User
from app.quality import bundle as B

from .test_experiments_api import _body, _drain, _template

pytestmark = pytest.mark.asyncio


def _dims(correctness: int):
    return [
        {"key": "correctness", "name": "Correctness", "score": correctness,
         "weight": 3, "status": "scored"},
        {"key": "completeness", "name": "Completeness", "score": 7,
         "weight": 1, "status": "scored"},
    ]


async def _settled_experiment(auth_client: AsyncClient, db_session, *, judged=False):
    """A finished experiment; with ``judged``, one that also carries judge scores
    and human ratings.

    The stand's own runs are judged and annotated, and the sections a bundle exists
    to protect — the trusted view and E-17 — only exist when they are. A fixture
    without them would let the tamper tests pass by never reaching the code."""
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tpl = await _template(db_session, workspace_id)
    created = (await auth_client.post("/api/experiments", json=_body(tpl.id))).json()
    await auth_client.post(f"/api/experiments/{created['id']}/run")
    exp = await _drain(auth_client, db_session, created["id"])
    assert exp.status == "completed"
    if judged:
        await _judge_and_annotate(db_session, exp, workspace_id)
    return exp


async def _judge_and_annotate(db_session, exp, workspace_id):
    runs = (
        await db_session.execute(
            select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id)
        )
    ).scalars().all()
    # A real person behind the rating: `annotator_id` is a foreign key, and SPA-85
    # made n_humans mean people rather than distinct account strings.
    rater = User(email=f"rater-{uuid.uuid4().hex[:8]}@x.dev", display_name="rater")
    db_session.add(rater)
    await db_session.flush()
    annotator = rater.id
    for i, run in enumerate(runs):
        score = 9 - i  # a spread, so a corrupted value actually moves a mean
        run.weighted_score = float(score)
        rec = (
            await db_session.execute(
                select(QualityRecord).where(QualityRecord.task_id == run.task_id)
            )
        ).scalar_one_or_none()
        if rec is None:
            continue
        rec.quality_profile = {
            "dimensions": _dims(score),
            "gate": {"passed": True},
            "judge_cost_usd": 0.0,
        }
        db_session.add(
            Annotation(
                quality_record_id=rec.id, task_id=run.task_id, workspace_id=workspace_id,
                annotator_type="human", annotator_id=annotator, annotator_label="rater",
                verdict="pass" if score >= 7 else "fail", blind_to_peers=True,
                dimensions=_dims(score),
                judge_observation={
                    "outcome": {
                        "judge_model": "judge-m", "gate_passed": True,
                        "scores": {"correctness": score, "completeness": 7},
                        "reasoning": {"correctness": "j", "completeness": "j"},
                    }
                },
            )
        )
    await db_session.commit()


async def test_a_bundle_recomputes_the_platform_s_own_numbers(auth_client, db_session):
    exp = await _settled_experiment(auth_client, db_session)

    files, manifest = await B.build_bundle(db_session, exp, selection="latest_valid")
    assert manifest["selection"] == "latest_valid"
    assert manifest["counts"]["runs"] == 4
    assert manifest["counts"]["records"] > 0
    # The honest name and the unpinned list travel inside the archive, not in a
    # README a reader may never open.
    assert manifest["replay"]["kind"] == "best_effort_input_replay"
    assert "provider_endpoints" in manifest["replay"]["not_pinned"]

    result = B.verify_bundle(files)
    assert result["ok"] is True, (result["headline_diffs"], result["blob_problems"])
    assert result["reproduced"] is True and result["complete"] is True
    assert result["full_report_matches"] is True
    assert result["report_schema_version"]["bundle"] == result["report_schema_version"]["checkout"]


async def test_verification_survives_a_round_trip_through_the_archive(auth_client, db_session):
    """Recomputing from the in-memory dict proves the pipeline; recomputing from
    the bytes on disk proves the FORMAT — different failure, same word."""
    exp = await _settled_experiment(auth_client, db_session)
    files, _ = await B.build_bundle(db_session, exp, selection="latest_valid")

    blob = B.write_tar(files)
    assert B.verify_bundle(B.read_tar(blob))["ok"] is True
    # Deterministic on the wire, so a re-export is not mistaken for a change.
    assert B.write_tar(files) == blob


async def test_a_tampered_profile_fails_verification(auth_client, db_session):
    """Without this, «verified» is decoration. The trusted view is the section that
    re-derives from `quality_profile.dimensions`, so that is where a corrupted
    profile surfaces — which is exactly what the manifest's derivation note says."""
    exp = await _settled_experiment(auth_client, db_session, judged=True)
    files, _ = await B.build_bundle(db_session, exp, selection="latest_valid")

    lines = files[B.RECORDS_NAME].decode().splitlines()
    tampered = False
    for i, line in enumerate(lines):
        row = json.loads(line)
        for d in (row.get("quality_profile") or {}).get("dimensions") or []:
            if d.get("score") is not None:
                d["score"] = d["score"] + 3
                tampered = True
                break
        if tampered:
            lines[i] = json.dumps(row, sort_keys=True)
            break
    assert tampered, "the fixture produced no scored dimension to corrupt"
    files[B.RECORDS_NAME] = ("\n".join(lines) + "\n").encode()

    result = B.verify_bundle(files)
    assert result["reproduced"] is False
    assert "trusted" in {d["metric"] for d in result["headline_diffs"]}


async def test_a_tampered_run_score_fails_verification(auth_client, db_session):
    """The other derivation path. The raw summary reads the outcome score from the
    denormalized run column rather than the profile, so corrupting a profile alone
    would never move it — a reader watching only that section could conclude the
    check does not work. Both paths are therefore pinned."""
    exp = await _settled_experiment(auth_client, db_session, judged=True)
    files, _ = await B.build_bundle(db_session, exp, selection="latest_valid")

    lines = files[B.RUNS_NAME].decode().splitlines()
    moved = False
    for i, line in enumerate(lines):
        row = json.loads(line)
        if row.get("weighted_score") is not None:
            row["weighted_score"] = float(row["weighted_score"]) / 2 + 1
            lines[i] = json.dumps(row, sort_keys=True)
            moved = True
            break
    assert moved, "the fixture produced no scored run to corrupt"
    files[B.RUNS_NAME] = ("\n".join(lines) + "\n").encode()

    result = B.verify_bundle(files)
    assert result["reproduced"] is False
    assert "summary" in {d["metric"] for d in result["headline_diffs"]}


async def test_the_endpoint_streams_an_archive_that_verifies(auth_client, db_session):
    """The UI's one click has to produce the same artifact the CLI does — otherwise
    the button is a second implementation, which is the drift this ticket's siblings
    kept being caused by."""
    exp = await _settled_experiment(auth_client, db_session)

    r = await auth_client.get(f"/api/experiments/{exp.id}/bundle")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/gzip")
    assert f"bundle-{exp.id}.tar.gz" in r.headers["content-disposition"]

    files = B.read_tar(r.content)
    assert B.verify_bundle(files)["ok"] is True
    manifest = json.loads(files[B.MANIFEST_NAME])
    # Traces are CLI-only, and the manifest says so rather than leaving their
    # absence to be read as "this experiment had none".
    assert manifest["blob_tiers"]["logs"] is False
    assert "with-traces" in manifest["blob_tiers"]["logs_note"]


async def test_an_unknown_selection_policy_is_rejected(auth_client, db_session):
    exp = await _settled_experiment(auth_client, db_session)
    r = await auth_client.get(f"/api/experiments/{exp.id}/bundle?selection=whatever")
    assert r.status_code == 400


async def test_the_bundle_is_scoped_to_its_workspace(auth_client, db_session):
    """A reproduction artifact that crosses a workspace boundary is a data leak
    wearing a research hat."""
    exp = await _settled_experiment(auth_client, db_session)
    stray = (
        await db_session.execute(select(Experiment).where(Experiment.id != exp.id))
    ).scalars().first()
    assert stray is None or stray.workspace_id == exp.workspace_id

    r = await auth_client.get(f"/api/experiments/{uuid.uuid4()}/bundle")
    assert r.status_code == 404


async def test_losing_the_object_store_is_not_a_reproduced_bundle(auth_client, db_session):
    """The failure SPA-90 was written for, spelled out. Every number in the report
    recomputes from Postgres alone, so an archive that shipped without its record
    blobs still says `reproduced: true` — and would have passed as an artifact that
    demonstrably cannot survive the loss it is meant to insure against. `ok` is the
    verdict; `reproduced` alone is only half of one."""
    exp = await _settled_experiment(auth_client, db_session, judged=True)
    files, manifest = await B.build_bundle(db_session, exp, selection="latest_valid")
    assert manifest["counts"]["expected_record_blobs"] == manifest["counts"]["record_blobs"] > 0

    stripped = {k: v for k, v in files.items() if not k.startswith(B.RECORD_BLOB_DIR)}
    result = B.verify_bundle(stripped)
    assert result["reproduced"] is True   # the arithmetic is untouched...
    assert result["complete"] is False    # ...and the evidence behind it is gone
    assert result["ok"] is False
    assert any("missing from the archive" in p for p in result["blob_problems"])


async def test_the_bundle_carries_the_report_it_expects(auth_client, db_session):
    """Stored, not just hashed: a digest can say THAT something moved, only the
    report can say where. It also lets a reader see the expected numbers without
    running anything."""
    exp = await _settled_experiment(auth_client, db_session, judged=True)
    files, _ = await B.build_bundle(db_session, exp, selection="latest_valid")

    expected = json.loads(files[B.EXPECTED_NAME])
    assert isinstance(expected["report"], dict)
    assert "generated_at" not in expected["report"]
    assert B.sha256_of(expected["report"]) == expected["full_report_sha256"]

    # a drift OUTSIDE the headline: the contract holds, the tripwire names the path
    expected["report"]["leaderboard"] = {"method": "moved"}
    expected["full_report_sha256"] = "0" * 64
    files[B.EXPECTED_NAME] = json.dumps(expected).encode()
    result = B.verify_bundle(files)
    assert result["reproduced"] is True
    assert result["full_report_matches"] is False
    assert any(p.startswith("leaderboard") for p in result["full_report_diff"])
