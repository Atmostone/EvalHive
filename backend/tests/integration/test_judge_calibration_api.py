"""Integration tests for the Judge Calibration Protocol endpoints (E-17).

POST /api/quality/judge-calibration/run validates the LLM judge (E-02) against
human feedback (E-05) over stored scores — no LLM call — and persists a versioned
report. GET /api/quality/judge-calibration reads the latest (and, with
history=true, the version history); GET /api/quality/judge-calibration/badge
returns the compact "calibrated against N humans, kappa=X.X" summary. The run
endpoint is owner/admin-only. Also guards the refactored GET /api/quality/calibration
export against regression.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from app import database
from app.models.annotation import Annotation
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.models.workspace import Workspace
from app.quality.stats import score_to_band

# correctness: judge tracks human → reliable. tool_selection: judge anti-correlated
# with human → not reliable. Eight records, two dimensions each.
_CORRECTNESS = [(9, 9), (8, 8), (7, 8), (3, 2), (2, 3), (9, 9), (6, 7), (8, 8)]
_TOOL = [(9, 2), (2, 9), (8, 3), (3, 8), (9, 4), (1, 7), (8, 2), (2, 8)]
# Verdicts mostly agree between judge gate and human verdict.
_VERDICTS = [
    (True, "approve"), (True, "approve"), (True, "approve"), (False, "reject"),
    (False, "reject"), (True, "approve"), (False, "reject"), (True, "approve"),
]
_SUBMITTERS = ["alice@x.com", "bob@x.com"]


async def _seed_judge_model(ws, *, api_name="judge-m"):
    """Seed a provider+model and make it the workspace quality judge so the report
    keys on a real api_name rather than 'unknown'."""
    async with database.async_session() as s:
        prov = Provider(workspace_id=ws, name="p", api_key="k", endpoint="http://x/v1")
        s.add(prov)
        await s.flush()
        model = LLMModel(
            provider_id=prov.id, display_name="J", api_name=api_name,
            input_price_per_1m_usd=1, output_price_per_1m_usd=2,
        )
        s.add(model)
        await s.flush()
        wsrow = await s.get(Workspace, ws)
        wsrow.quality_judge_model_id = model.id
        await s.commit()


async def _seed_annotators(s) -> list:
    """Two real people behind the ratings, so `n_humans` can mean people rather
    than distinct account strings (SPA-85)."""
    ids = []
    for email in _SUBMITTERS:
        u = User(email=email, display_name=email.split("@")[0])
        s.add(u)
        await s.flush()
        ids.append(u.id)
    return ids


def _dims(ch, th):
    return [
        {"key": "correctness", "name": "Correctness", "score": ch,
         "band": score_to_band(ch)},
        {"key": "tool_selection", "name": "Tool Selection", "score": th,
         "band": score_to_band(th)},
    ]


async def _seed_records(ws):
    async with database.async_session() as s:
        annotators = await _seed_annotators(s)
        for i in range(len(_CORRECTNESS)):
            cj, ch = _CORRECTNESS[i]
            tj, th = _TOOL[i]
            gate_passed, verdict = _VERDICTS[i]
            t = Task(title=f"t{i}", status=TaskStatus.DONE.value, workspace_id=ws,
                     result_summary="answer", model_used="doer-m")
            s.add(t)
            await s.flush()
            record = QualityRecord(
                task_id=t.id, workspace_id=ws, model_used="doer-m",
                final_status=TaskStatus.DONE.value,
                quality_profile={
                    "dimensions": [
                        {"key": "correctness", "name": "Correctness", "score": cj,
                         "reasoning": "j"},
                        {"key": "tool_selection", "name": "Tool Selection", "score": tj,
                         "reasoning": "j"},
                    ],
                    "gate": {"passed": gate_passed},
                },
                human_feedback={
                    "schema_version": 1,
                    "verdict": verdict,
                    "dimensions": _dims(ch, th),
                    "submitted_by": _SUBMITTERS[i % 2],
                    "submitted_at": "2026-05-30T00:00:00",
                },
            )
            s.add(record)
            await s.flush()
            # Each record is rated by one of the two people; nobody rates the
            # same run twice, so there is no inter-annotator overlap here.
            s.add(Annotation(
                quality_record_id=record.id, task_id=t.id, workspace_id=ws,
                annotator_type="human", annotator_id=annotators[i % 2],
                annotator_label=_SUBMITTERS[i % 2], verdict=verdict,
                blind_to_peers=True,
                dimensions=_dims(ch, th),
                judge_observation={
                    "outcome": {
                        "judge_model": "judge-m",
                        "gate_passed": gate_passed,
                        "scores": {"correctness": cj, "tool_selection": tj},
                        "reasoning": {"correctness": "j", "tool_selection": "j"},
                    }
                },
            ))
        await s.commit()


async def test_run_get_history_and_badge(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    await _seed_records(ws)

    # run #1
    r = await auth_client.post("/api/quality/judge-calibration/run")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["version"] == 1
    assert report["judge_config_key"] == "judge-m"
    assert report["sample_size"] == 16  # 8 records × 2 dims
    metrics = report["metrics"]
    dims = {d["key"]: d for d in metrics["dimensions"]}
    assert dims["correctness"]["reliable"] is True
    assert dims["correctness"]["status"] == "ok"
    assert dims["correctness"]["n"] == 8
    assert dims["tool_selection"]["reliable"] is False
    assert metrics["n_humans"] == 2
    # people, annotations and provenance are reported separately (SPA-85)
    assert metrics["n_annotators"] == 2
    assert metrics["n_annotations"] == 8
    assert metrics["n_legacy"] == 0
    assert metrics["judge_frozen_pct"] == 1.0
    # nobody rated the same run twice, so there is no agreement to report
    assert metrics["inter_annotator"]["available"] is False
    recs = " ".join(metrics["recommendations"]).lower()
    assert "correctness" in recs and "tool selection" in recs

    # run #2 → next version
    r = await auth_client.post("/api/quality/judge-calibration/run")
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # latest
    r = await auth_client.get("/api/quality/judge-calibration")
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # history (newest first)
    r = await auth_client.get("/api/quality/judge-calibration?history=true")
    assert r.status_code == 200
    body = r.json()
    assert body["latest"]["version"] == 2
    assert [h["version"] for h in body["history"]] == [2, 1]

    # badge
    r = await auth_client.get("/api/quality/judge-calibration/badge")
    assert r.status_code == 200
    badge = r.json()
    assert badge["calibrated"] is True
    assert badge["n_humans"] == 2
    assert badge["n_legacy"] == 0
    assert badge["judge_frozen_pct"] == 1.0
    assert badge["judge_config_key"] == "judge-m"
    assert badge["overall_kappa"] is not None


async def test_badge_uncalibrated_when_never_run(auth_client: AsyncClient):
    r = await auth_client.get("/api/quality/judge-calibration/badge")
    assert r.status_code == 200
    assert r.json()["calibrated"] is False

    r = await auth_client.get("/api/quality/judge-calibration")
    assert r.status_code == 200
    assert r.json() is None


async def test_run_requires_admin(auth_client: AsyncClient):
    ws = auth_client.headers["X-Workspace-Id"]
    # demote the caller to a plain member
    async with database.async_session() as s:
        await s.execute(text(
            "UPDATE workspace_members SET role='member' WHERE workspace_id=:w"
        ), {"w": ws})
        await s.commit()

    r = await auth_client.post("/api/quality/judge-calibration/run")
    assert r.status_code == 403


async def test_calibration_export_regression(auth_client: AsyncClient):
    """The refactored GET /calibration still returns the per-dimension judge-vs-human
    rows with their core fields."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_records(ws)

    r = await auth_client.get("/api/quality/calibration")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 16
    row = rows[0]
    for field in ("task_id", "dimension_key", "dimension_name", "judge_score",
                  "human_score", "band", "verdict"):
        assert field in row
    # judge_score falls back to the quality_profile dimension score
    corr = [x for x in rows if x["dimension_key"] == "correctness"]
    assert all(x["judge_score"] is not None for x in corr)
    # provenance of each pair's judge side (SPA-85)
    assert {x["judge_source"] for x in rows} == {"frozen"}
    assert {x["annotator_type"] for x in rows} == {"human"}


# --------------------------------------------------------------------------- #
# Inter-annotator agreement (SPA-85)
# --------------------------------------------------------------------------- #
# Four runs, each rated by BOTH people. Correctness bands agree on three and
# disagree on the fourth; verdicts agree on three of four.
_PAIRED = [
    #  alice, bob, alice verdict, bob verdict
    (9, 8, "approve", "approve"),
    (2, 3, "reject", "reject"),
    (5, 6, "approve", "approve"),
    (9, 2, "reject", "approve"),
]


async def _seed_paired_records(ws):
    async with database.async_session() as s:
        annotators = await _seed_annotators(s)
        for i, (a_score, b_score, a_verdict, b_verdict) in enumerate(_PAIRED):
            t = Task(title=f"p{i}", status=TaskStatus.DONE.value, workspace_id=ws,
                     result_summary="answer", model_used="doer-m")
            s.add(t)
            await s.flush()
            record = QualityRecord(
                task_id=t.id, workspace_id=ws, model_used="doer-m",
                final_status=TaskStatus.DONE.value,
                quality_profile={
                    "dimensions": [
                        {"key": "correctness", "name": "Correctness", "score": 7},
                    ],
                    "gate": {"passed": True},
                },
            )
            s.add(record)
            await s.flush()
            observation = {
                "outcome": {"gate_passed": True, "scores": {"correctness": 7}}
            }
            for who, score, verdict in (
                (0, a_score, a_verdict),
                (1, b_score, b_verdict),
            ):
                s.add(Annotation(
                    quality_record_id=record.id, task_id=t.id, workspace_id=ws,
                    annotator_type="human", annotator_id=annotators[who],
                    annotator_label=_SUBMITTERS[who], verdict=verdict,
                    # collected independently — which is what lets these rows
                    # enter the inter-annotator agreement at all (SPA-85)
                    blind_to_peers=True,
                    dimensions=[{"key": "correctness", "name": "Correctness",
                                 "score": score, "band": score_to_band(score)}],
                    judge_observation=observation,
                ))
        await s.commit()


async def test_inter_annotator_agreement_falls_out_of_the_data(auth_client: AsyncClient):
    """The number a single overwritable feedback slot made uncomputable: two
    people rate the same run, and κ between them is just there."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    await _seed_paired_records(ws)

    r = await auth_client.post("/api/quality/judge-calibration/run")
    assert r.status_code == 200, r.text
    metrics = r.json()["metrics"]

    assert metrics["n_humans"] == 2
    assert metrics["n_annotations"] == 8          # 4 runs × 2 people
    inter = metrics["inter_annotator"]
    assert inter["available"] is True
    assert inter["n_records"] == 4 and inter["n_annotators"] == 2
    dim = {d["key"]: d for d in inter["dimensions"]}["correctness"]
    assert dim["n"] == 4                          # one annotator pair per run
    assert dim["agreement_pct"] == 0.75           # three bands out of four
    assert 0 < dim["cohen_kappa"] < 1
    assert inter["overall"]["n"] == 4
    assert inter["overall"]["agreement_pct"] == 0.75

    # judge-vs-human counts BOTH annotators' verdicts, not whichever the loop
    # reached last: 4 runs × 2 people, judge gate passed everywhere
    assert metrics["overall"]["n"] == 8

    r = await auth_client.get("/api/quality/judge-calibration/badge")
    assert r.json()["inter_annotator_records"] == 4
    assert r.json()["inter_annotator_kappa"] is not None


async def test_legacy_rows_are_labelled_not_counted_as_people(auth_client: AsyncClient):
    """Ratings collected before the ledger carry no user and no frozen judge —
    they stay in the population, but they are not people and their judge side is
    honestly reported as re-read from the live profile."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    async with database.async_session() as s:
        t = Task(title="old", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary="answer", model_used="doer-m")
        s.add(t)
        await s.flush()
        record = QualityRecord(
            task_id=t.id, workspace_id=ws, model_used="doer-m",
            final_status=TaskStatus.DONE.value,
            quality_profile={
                "dimensions": [{"key": "correctness", "name": "Correctness", "score": 7}],
                "gate": {"passed": True},
            },
        )
        s.add(record)
        await s.flush()
        s.add(Annotation(
            quality_record_id=record.id, task_id=t.id, workspace_id=ws,
            annotator_type="legacy", annotator_id=None,
            annotator_label="alice@x.com", verdict="approve",
            dimensions=[{"key": "correctness", "name": "Correctness",
                         "score": 6, "band": score_to_band(6)}],
            judge_observation=None,
        ))
        await s.commit()

    r = await auth_client.get("/api/quality/calibration")
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["annotator_type"] == "legacy"
    assert rows[0]["annotator_id"] is None
    assert rows[0]["judge_score"] == 7 and rows[0]["judge_source"] == "live"

    r = await auth_client.post("/api/quality/judge-calibration/run")
    metrics = r.json()["metrics"]
    assert metrics["n_humans"] == 0        # nobody attributable
    assert metrics["n_legacy"] == 1
    assert metrics["n_annotators"] == 1    # the label still identifies a rater
    assert metrics["judge_frozen_pct"] == 0.0
