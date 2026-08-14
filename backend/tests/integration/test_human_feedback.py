"""Integration tests for annotation collection (E-05) and its ledger (SPA-85).

Feedback round-trips through PUT/GET, auto-creates the quality record when none
exists, pairs human scores with judge scores from the profile, surfaces in the
calibration export, is workspace-scoped, and rejects out-of-range scores.

The ledger adds: two annotators on one run stay two independent rows, the same
annotator re-rating supersedes their own previous row, a machine annotation
never displaces the human slot, the judge's side is frozen so a re-judge cannot
move a past pair, and the write is role-gated while the read is not.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import database
from app.models.annotation import Annotation
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMember


async def _seed_judge_model(s, workspace_id):
    prov = Provider(workspace_id=workspace_id, name="p", api_key="k", endpoint="http://x/v1")
    s.add(prov)
    await s.flush()
    model = LLMModel(provider_id=prov.id, display_name="M", api_name="m",
                     input_price_per_1m_usd=1, output_price_per_1m_usd=2)
    s.add(model)
    await s.flush()
    ws = await s.get(Workspace, workspace_id)
    ws.quality_judge_model_id = model.id
    # Without this the whole seed rolls back on context exit and the judge
    # resolves to None, which silently skips evaluation.
    await s.commit()


async def _make_task(ws, **kw):
    kw.setdefault("result_summary", "result")
    async with database.async_session() as s:
        t = Task(title="t", status=TaskStatus.DONE.value, workspace_id=ws,
                 model_used="m", **kw)
        s.add(t)
        await s.commit()
        return str(t.id)


async def _join_workspace(client: AsyncClient, ws, role: str) -> dict:
    """Register a second user, add them to ``ws`` with ``role``, and return the
    headers that authenticate them against that workspace."""
    email = f"u-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/api/auth/register",
        json={"email": email, "password": "password1234", "display_name": "Other"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    async with database.async_session() as s:
        uid = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
        s.add(WorkspaceMember(user_id=uid, workspace_id=ws, role=role))
        await s.commit()
    return {"Authorization": f"Bearer {token}", "X-Workspace-Id": str(ws)}


@pytest.mark.asyncio
async def test_feedback_roundtrip_and_record_autocreate(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _make_task(ws)

    # no feedback yet
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.status_code == 200, r.text
    assert r.json()["human_feedback"] is None

    # submit — record is built on demand
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "verdict": "approve",
        "overall_comment": "solid",
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 9, "comment": "right"}],
    })
    assert r.status_code == 200, r.text
    fb = r.json()["human_feedback"]
    assert fb["verdict"] == "approve" and fb["overall_comment"] == "solid"
    d = fb["dimensions"][0]
    assert d["score"] == 9 and d["band"] == "good" and d["comment"] == "right"
    assert d["judge_score"] is None  # no profile yet
    assert fb["submitted_by"]  # the authed user's email

    # GET returns the stored feedback
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.json()["human_feedback"]["dimensions"][0]["score"] == 9

    # ...and the ledger carries it as one `human` row attributed to a real user
    r = await auth_client.get(f"/api/quality/records/{tid}/annotations")
    assert r.status_code == 200, r.text
    rows = r.json()["annotations"]
    assert len(rows) == 1
    assert rows[0]["annotator_type"] == "human"
    assert rows[0]["annotator_id"] is not None
    assert rows[0]["supersedes_id"] is None
    assert rows[0]["protocol_version"] == 1


@pytest.mark.asyncio
async def test_feedback_pairs_judge_score_and_exports(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])

    # default rubric with a pure-python reference dim → profile without an LLM
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Exact", "is_default": True, "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "exact", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
    tid = await _make_task(ws, result_summary="Paris", reference_answer="paris")

    # evaluate → profile with answer=10
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate")
    assert r.status_code == 200, r.text
    assert r.json()["quality_profile"]["dimensions"][0]["score"] == 10

    # human disagrees: rates the same dimension 4
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "answer", "name": "Answer", "score": 4}],
    })
    assert r.status_code == 200, r.text
    d = r.json()["human_feedback"]["dimensions"][0]
    assert d["score"] == 4 and d["band"] == "improve" and d["judge_score"] == 10

    # calibration export pairs them
    r = await auth_client.get("/api/quality/calibration")
    assert r.status_code == 200, r.text
    rows = [row for row in r.json() if row["task_id"] == tid]
    assert len(rows) == 1
    assert rows[0]["judge_score"] == 10 and rows[0]["human_score"] == 4
    assert rows[0]["dimension_key"] == "answer"
    assert rows[0]["judge_source"] == "frozen"


@pytest.mark.asyncio
async def test_feedback_rejects_out_of_range_score(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _make_task(ws)
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "a", "name": "A", "score": 11}],
    })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_feedback_cross_workspace_404(auth_client: AsyncClient):
    # a task in another workspace (the seeded default) is invisible to this client
    tid = await _make_task(DEFAULT_WORKSPACE_ID)

    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.status_code == 404
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={"dimensions": []})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# The ledger (SPA-85)
# --------------------------------------------------------------------------- #
async def _seed_judged_record(ws, *, tid=None):
    """A task whose record carries both judge profiles, so annotations have a
    judge side to freeze."""
    tid = tid or await _make_task(ws)
    async with database.async_session() as s:
        s.add(QualityRecord(
            task_id=uuid.UUID(tid), workspace_id=ws, model_used="m",
            final_status=TaskStatus.DONE.value,
            quality_profile={
                "judge_model": "judge-v1",
                "dimensions": [{"key": "correctness", "name": "Correctness", "score": 8}],
                "gate": {"passed": True},
            },
            trajectory_profile={
                "judge_model": "judge-v1",
                "axes": [{"key": "efficiency", "name": "Efficiency", "score": 3}],
            },
        ))
        await s.commit()
    return tid


@pytest.mark.asyncio
async def test_rejudge_cannot_move_a_past_pair(auth_client: AsyncClient):
    """The defect this closes: the trajectory side of a pair used to be re-read
    from the live profile, so re-running the E-07 judge rewrote a past
    calibration."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)

    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "verdict": "reject",
        "dimensions": [
            {"key": "correctness", "name": "Correctness", "score": 7},
            {"key": "efficiency", "name": "Efficiency", "score": 2},
        ],
    })
    assert r.status_code == 200, r.text
    stored = {d["key"]: d["judge_score"] for d in r.json()["human_feedback"]["dimensions"]}
    assert stored == {"correctness": 8, "efficiency": 3}

    # re-judge both axes with completely different scores
    async with database.async_session() as s:
        rec = (await s.execute(
            select(QualityRecord).where(QualityRecord.task_id == uuid.UUID(tid))
        )).scalar_one()
        rec.quality_profile = {
            "judge_model": "judge-v2",
            "dimensions": [{"key": "correctness", "name": "Correctness", "score": 1}],
            "gate": {"passed": False},
        }
        rec.trajectory_profile = {
            "judge_model": "judge-v2",
            "axes": [{"key": "efficiency", "name": "Efficiency", "score": 10}],
        }
        await s.commit()

    r = await auth_client.get("/api/quality/calibration")
    rows = {row["dimension_key"]: row for row in r.json() if row["task_id"] == tid}
    assert rows["correctness"]["judge_score"] == 8
    assert rows["efficiency"]["judge_score"] == 3
    assert {row["judge_source"] for row in rows.values()} == {"frozen"}
    # the gate verdict is frozen too, so the overall agreement cannot drift
    assert rows["correctness"]["judge_gate_passed"] is True


@pytest.mark.asyncio
async def test_two_annotators_are_two_rows(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    other = await _join_workspace(auth_client, ws, "admin")

    body = {"verdict": "approve",
            "dimensions": [{"key": "correctness", "name": "Correctness", "score": 9}]}
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json=body)).status_code == 200
    body2 = {"verdict": "reject",
             "dimensions": [{"key": "correctness", "name": "Correctness", "score": 2}]}
    r = await auth_client.put(
        f"/api/quality/records/{tid}/feedback", json=body2, headers=other
    )
    assert r.status_code == 200, r.text

    r = await auth_client.get(f"/api/quality/records/{tid}/annotations")
    rows = r.json()["annotations"]
    assert len(rows) == 2
    # neither supersedes the other — that is what makes them comparable
    assert [row["supersedes_id"] for row in rows] == [None, None]
    assert len({row["annotator_id"] for row in rows}) == 2

    # both are in the calibration population
    r = await auth_client.get("/api/quality/calibration")
    scores = sorted(row["human_score"] for row in r.json() if row["task_id"] == tid)
    assert scores == [2, 9]


@pytest.mark.asyncio
async def test_reannotation_supersedes_only_your_own(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    other = await _join_workspace(auth_client, ws, "admin")

    dim = {"key": "correctness", "name": "Correctness"}
    await auth_client.put(f"/api/quality/records/{tid}/feedback",
                          json={"dimensions": [{**dim, "score": 9}]})
    await auth_client.put(f"/api/quality/records/{tid}/feedback",
                          json={"dimensions": [{**dim, "score": 5}]}, headers=other)
    # the first annotator changes their mind
    await auth_client.put(f"/api/quality/records/{tid}/feedback",
                          json={"dimensions": [{**dim, "score": 6}]})

    rows = (await auth_client.get(f"/api/quality/records/{tid}/annotations")).json()["annotations"]
    assert len(rows) == 3                                   # nothing was destroyed
    superseded = [row["supersedes_id"] for row in rows if row["supersedes_id"]]
    assert superseded == [rows[0]["id"]]                    # only their own first row

    # the population is still two annotators, at their current scores
    r = await auth_client.get("/api/quality/calibration")
    scores = sorted(row["human_score"] for row in r.json() if row["task_id"] == tid)
    assert scores == [5, 6]

    # the materialised slot is the latest human rating
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.json()["human_feedback"]["dimensions"][0]["score"] == 6


@pytest.mark.asyncio
async def test_machine_annotation_is_kept_out_of_the_human_population(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)

    await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 9}],
    })
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "annotator_type": "llm_judge",
        "annotator_label": "annotator-model-x",
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 1}],
    })
    assert r.status_code == 200, r.text

    rows = (await auth_client.get(f"/api/quality/records/{tid}/annotations")).json()["annotations"]
    assert [row["annotator_type"] for row in rows] == ["human", "llm_judge"]
    # no user id behind a machine rating — counting it as a person is the
    # confusion the annotator type replaces
    assert rows[1]["annotator_id"] is None
    assert rows[1]["annotator_label"] == "annotator-model-x"

    # the human slot is untouched, and judge-vs-human sees only the human
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback")
    assert r.json()["human_feedback"]["dimensions"][0]["score"] == 9
    r = await auth_client.get("/api/quality/calibration")
    assert [row["human_score"] for row in r.json() if row["task_id"] == tid] == [9]


@pytest.mark.asyncio
async def test_blind_session_is_served_no_judge_material(auth_client: AsyncClient):
    """The whole bundle is built in one place from the declared protocol, so a
    blind session physically cannot receive a judge score (SPA-85)."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    # a prior rating, so the bundle's `human_feedback` and ledger are non-empty
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 9}],
    })).status_code == 200

    r = await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["protocol"]["blind_to_judge"] is True
    assert b["protocol"]["blind_to_model"] is True
    # every part of the payload, not just the profile
    assert b["quality_profile"]["blinded"] is True
    assert b["quality_profile"]["gate"] is None
    assert b["quality_profile"]["dimensions"] == [
        {"key": "correctness", "name": "Correctness", "status": None}
    ]
    assert all(a.get("score") is None for a in b["trajectory_profile"]["axes"])
    assert all(d.get("judge_score") is None for d in b["human_feedback"]["dimensions"])
    assert all(a["judge_observation"] == {} for a in b["annotations"])
    assert b["model_used"] is None
    # ...while what is being rated is fully there
    assert b["review"]["result_summary"]


@pytest.mark.asyncio
async def test_the_session_decides_the_protocol_not_the_body(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    blind = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}
    )).json()

    # asking to be recorded as sighted does not make it so
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "session_id": blind["session_id"],
        "blind_to_judge": False,
        "blind_to_model": False,
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    })
    assert r.status_code == 200, r.text
    row = (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"][0]
    assert row["blind_to_judge"] is True and row["blind_to_model"] is True
    assert row["session_id"] == blind["session_id"]
    # the judge is still frozen onto the row: blindness is about what the
    # annotator was served, not about what we record
    assert row["judge_observation"]["outcome"]["scores"] == {"correctness": 8}


@pytest.mark.asyncio
async def test_a_sighted_session_cannot_claim_blind(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    sighted = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": False}
    )).json()
    assert sighted["quality_profile"]["dimensions"][0]["score"] == 8

    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "session_id": sighted["session_id"],
        "blind_to_judge": True,   # asserted, and ignored
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    })
    assert r.status_code == 200, r.text
    row = (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"][0]
    assert row["blind_to_judge"] is False


@pytest.mark.asyncio
async def test_no_session_means_no_claim(auth_client: AsyncClient):
    """Rating without a session — a script, or a client that skipped the flow —
    is recorded as sighted rather than believed."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)

    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "blind_to_judge": True,
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    })
    assert r.status_code == 200, r.text
    row = (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"][0]
    assert row["blind_to_judge"] is False and row["session_id"] is None


@pytest.mark.asyncio
async def test_a_session_vouches_for_one_rating_only(auth_client: AsyncClient):
    """Single-use: otherwise one blind bundle could vouch for ratings made much
    later, after the annotator had looked at the judge somewhere else. Rating
    again means opening a new session, which states its own protocol."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    blind = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}
    )).json()
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "session_id": blind["session_id"],
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    })).status_code == 200

    sighted = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": False}
    )).json()
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "session_id": sighted["session_id"],
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 7}],
    })).status_code == 200

    rows = (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"]
    assert [r["blind_to_judge"] for r in rows] == [True, False]
    assert rows[1]["supersedes_id"] == rows[0]["id"]


@pytest.mark.asyncio
async def test_a_session_cannot_vouch_for_another_annotator(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    other = await _join_workspace(auth_client, ws, "admin")
    blind = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}
    )).json()

    r = await auth_client.put(
        f"/api/quality/records/{tid}/feedback",
        json={
            "session_id": blind["session_id"],
            "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
        },
        headers=other,
    )
    # 409 rather than a sighted rating: recording a protocol the caller did not
    # ask for is the failure this design exists to remove
    assert r.status_code == 409
    assert (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"] == []


@pytest.mark.asyncio
async def test_a_session_never_hands_you_another_annotators_rating(auth_client: AsyncClient):
    """The independence κ is computed over. Serving the materialised
    `human_feedback` slot would hand the second annotator the first one's scores
    to edit, and agreement measured over that is agreement with a pre-filled
    form."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    other = await _join_workspace(auth_client, ws, "admin")

    # the first annotator rates
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "verdict": "approve", "overall_comment": "CANARY-OVERALL",
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 9,
                        "comment": "CANARY-DIM"}],
    })).status_code == 200

    # the second opens a session and is shown nothing of it
    b = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": False},
        headers=other,
    )).json()
    assert b["human_feedback"] is None                 # not the other's, and not "yours"
    peer = b["annotations"][0]
    assert peer["redacted"] is True
    assert peer["verdict"] is None and peer["overall_comment"] is None
    assert peer["dimensions"] == [{"key": "correctness", "name": "Correctness"}]
    # provenance survives — «someone rated this» is not an anchor
    assert peer["annotator_label"] and peer["created_at"]
    assert "CANARY" not in json.dumps(b)

    # rating does NOT unlock the peer's opinion: revealing it afterwards left
    # re-annotation open, and the collector would then take the dependent
    # re-rating as this annotator's current one
    assert (await auth_client.put(
        f"/api/quality/records/{tid}/feedback",
        json={
            "session_id": b["session_id"], "verdict": "reject",
            "dimensions": [{"key": "correctness", "name": "Correctness", "score": 2}],
        },
        headers=other,
    )).status_code == 200
    b2 = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": False},
        headers=other,
    )).json()
    peer2 = next(r for r in b2["annotations"] if r["annotator_label"] != peer["annotator_label"] or r.get("redacted"))
    assert peer2["redacted"] is True and peer2["verdict"] is None
    assert "CANARY" not in json.dumps(b2)
    # ...while reopening your own form shows YOUR rating, not the latest one
    assert b2["human_feedback"]["verdict"] == "reject"
    assert b2["human_feedback"]["dimensions"][0]["score"] == 2
    assert next(r for r in b2["annotations"] if not r.get("redacted"))["verdict"] == "reject"


@pytest.mark.asyncio
async def test_retry_with_a_used_session_is_idempotent(auth_client: AsyncClient):
    """A lost response must not become a second rating under a silently
    different protocol."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    sid = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}
    )).json()["session_id"]
    body = {
        "session_id": sid,
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    }
    first = await auth_client.put(f"/api/quality/records/{tid}/feedback", json=body)
    assert first.status_code == 200, first.text
    again = await auth_client.put(f"/api/quality/records/{tid}/feedback", json=body)
    assert again.status_code == 200, again.text
    assert again.json()["human_feedback"] == first.json()["human_feedback"]

    rows = (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"]
    assert len(rows) == 1                       # no second row
    assert rows[0]["blind_to_judge"] is True    # and the protocol did not change


@pytest.mark.asyncio
async def test_an_unusable_session_is_a_conflict_not_a_downgrade(auth_client: AsyncClient):
    """Silently recording a sighted rating for a caller who asked for a blind one
    is the failure mode this replaces."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    other = await _join_workspace(auth_client, ws, "admin")
    body = {"dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}]}

    # unknown session
    r = await auth_client.put(
        f"/api/quality/records/{tid}/feedback",
        json={**body, "session_id": str(uuid.uuid4())},
    )
    assert r.status_code == 409

    # someone else's session
    sid = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True},
        headers=other,
    )).json()["session_id"]
    r = await auth_client.put(
        f"/api/quality/records/{tid}/feedback", json={**body, "session_id": sid}
    )
    assert r.status_code == 409

    # a session for a different run
    tid2 = await _seed_judged_record(ws)
    sid2 = (await auth_client.post(
        f"/api/quality/records/{tid2}/annotation-session", json={"blind": True}
    )).json()["session_id"]
    r = await auth_client.put(
        f"/api/quality/records/{tid}/feedback", json={**body, "session_id": sid2}
    )
    assert r.status_code == 409

    assert (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"] == []


@pytest.mark.asyncio
async def test_a_used_session_id_never_returns_someone_elses_rating(auth_client: AsyncClient):
    """The idempotent lookup must check identity BEFORE it fetches a rating —
    resolving by session id alone answers a replay of somebody else's id with
    their feedback, across workspaces."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    other = await _join_workspace(auth_client, ws, "admin")

    sid = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True},
        headers=other,
    )).json()["session_id"]
    assert (await auth_client.put(
        f"/api/quality/records/{tid}/feedback",
        json={"session_id": sid, "overall_comment": "PRIVATE",
              "dimensions": [{"key": "correctness", "name": "Correctness", "score": 1}]},
        headers=other,
    )).status_code == 200

    # replaying their consumed session id must not hand over their rating
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "session_id": sid,
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 8}],
    })
    assert r.status_code == 409
    assert "PRIVATE" not in r.text


@pytest.mark.asyncio
async def test_inter_annotator_kappa_ignores_ratings_that_saw_peers(auth_client: AsyncClient):
    """A rating collected without a session makes no independence claim, so it
    cannot be paired into an agreement number that asserts one."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    other = await _join_workspace(auth_client, ws, "admin")
    for i in range(3):
        tid = await _seed_judged_record(ws)
        for headers in ({}, other):
            # no session → blind_to_peers false
            assert (await auth_client.put(
                f"/api/quality/records/{tid}/feedback",
                json={"verdict": "approve",
                      "dimensions": [{"key": "correctness", "name": "Correctness",
                                      "score": 9 - i}]},
                headers=headers,
            )).status_code == 200

    rows = (await auth_client.get("/api/quality/calibration")).json()
    assert rows and all(r["blind_to_peers"] is False for r in rows)
    r = await auth_client.post("/api/quality/judge-calibration/run")
    inter = r.json()["metrics"]["inter_annotator"]
    # three runs each rated by both people — and still no agreement claimed
    assert inter["available"] is False and inter["n_records"] == 0


@pytest.mark.asyncio
async def test_one_session_cannot_produce_two_ratings(auth_client: AsyncClient):
    """The database is the arbiter of single-use, not a read-then-write."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    sid = (await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}
    )).json()["session_id"]
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "session_id": sid,
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    })).status_code == 200

    # a genuinely different rating against the same session is not a retry
    async with database.async_session() as s:
        rows = (await s.execute(
            select(Annotation).where(Annotation.task_id == uuid.UUID(tid))
        )).scalars().all()
        assert len(rows) == 1
        s.add(Annotation(
            quality_record_id=rows[0].quality_record_id, task_id=uuid.UUID(tid),
            workspace_id=ws, annotator_type="human", annotator_label="x@y.z",
            dimensions=[], session_id=uuid.UUID(sid),
        ))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_annotation_session_requires_admin(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    member = await _join_workspace(auth_client, ws, "member")
    r = await auth_client.post(
        f"/api/quality/records/{tid}/annotation-session", json={"blind": True}, headers=member
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_blind_queue_withholds_model_and_judge_score(auth_client: AsyncClient):
    """A convenience for a blind campaign: the queue is the first thing an
    annotator sees. What a rating records still comes from its session."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    async with database.async_session() as s:
        rec = (await s.execute(
            select(QualityRecord).where(QualityRecord.task_id == uuid.UUID(tid))
        )).scalar_one()
        rec.quality_profile = {**rec.quality_profile, "weighted_score": 8.0}
        await s.commit()

    r = await auth_client.get("/api/quality/calibration/queue?status=all&blind=true")
    assert r.status_code == 200, r.text
    item = next(i for i in r.json()["items"] if i["task_id"] == tid)
    # the queue is the first thing an annotator sees; leaving these on the row
    # would break the blind before any run is opened
    assert item["model_used"] is None and item["weighted_score"] is None
    assert item["title"]  # everything not judge-derived is still there

    r = await auth_client.get("/api/quality/calibration/queue?status=all")
    item = next(i for i in r.json()["items"] if i["task_id"] == tid)
    assert item["model_used"] == "m" and item["weighted_score"] == 8.0


@pytest.mark.asyncio
async def test_a_later_judge_run_cannot_fill_an_absent_pair(auth_client: AsyncClient):
    """The other half of «a re-judge cannot alter a past calibration»: an axis the
    judge had NOT scored when the human rated it must stay unpaired, or the pair
    materialises retroactively out of a profile written afterwards."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _make_task(ws)

    # rated before any judge ran
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 6}],
    })
    assert r.status_code == 200, r.text

    async with database.async_session() as s:
        rec = (await s.execute(
            select(QualityRecord).where(QualityRecord.task_id == uuid.UUID(tid))
        )).scalar_one()
        rec.quality_profile = {
            "judge_model": "judge-v1",
            "dimensions": [{"key": "correctness", "name": "Correctness", "score": 10}],
            "gate": {"passed": True},
        }
        await s.commit()

    rows = [r for r in (await auth_client.get("/api/quality/calibration")).json()
            if r["task_id"] == tid]
    assert len(rows) == 1
    assert rows[0]["judge_score"] is None
    assert rows[0]["judge_source"] == "unscored"
    assert rows[0]["judge_gate_passed"] is None


@pytest.mark.asyncio
async def test_verdict_only_annotation_stays_in_the_population(auth_client: AsyncClient):
    """A rating with no per-dimension scores is still a rating by a person — it
    used to vanish from n_humans, n_annotations and the verdict agreement it
    exists to feed."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)

    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "verdict": "reject", "overall_comment": "wrong answer", "dimensions": [],
    })
    assert r.status_code == 200, r.text

    rows = [r for r in (await auth_client.get("/api/quality/calibration")).json()
            if r["task_id"] == tid]
    assert len(rows) == 1
    assert rows[0]["dimension_key"] is None
    assert rows[0]["verdict"] == "reject"
    assert rows[0]["judge_gate_passed"] is True

    r = await auth_client.post("/api/quality/judge-calibration/run")
    metrics = r.json()["metrics"]
    assert metrics["n_humans"] == 1
    assert metrics["n_annotations"] == 1
    assert metrics["overall"]["n"] == 1        # the verdict pair is there
    assert metrics["n_dimensions"] == 0        # and it invents no dimension


@pytest.mark.asyncio
async def test_frozen_observation_records_the_conditions(auth_client: AsyncClient):
    """Model, rubric id and name cannot prove the conditions: a rubric is editable
    in place and the prompt changes with the E-18 mitigations."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    r = await auth_client.post("/api/quality/rubrics", json={
        "name": "Exact", "is_default": True, "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "exact", "weight": 1.0, "threshold": 6, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text
    async with database.async_session() as s:
        await _seed_judge_model(s, ws)
    tid = await _make_task(ws, result_summary="Paris", reference_answer="paris")
    assert (await auth_client.post(f"/api/quality/records/{tid}/evaluate")).status_code == 200
    assert (await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "dimensions": [{"key": "answer", "name": "Answer", "score": 4}],
    })).status_code == 200

    outcome = (await auth_client.get(
        f"/api/quality/records/{tid}/annotations"
    )).json()["annotations"][0]["judge_observation"]["outcome"]
    assert len(outcome["rubric_fingerprint"]) == 64
    assert len(outcome["prompt_fingerprint"]) == 64
    assert outcome["files_only"] is False

    # editing the rubric in place keeps its id and changes the fingerprint, so a
    # later verdict is distinguishable from this one
    before = outcome["rubric_fingerprint"]
    r = await auth_client.patch(f"/api/quality/rubrics/{outcome['rubric_id']}", json={
        "dimensions": [
            {"key": "answer", "name": "Answer", "evaluator": "reference",
             "reference_mode": "exact", "weight": 1.0, "threshold": 9, "critical": True},
        ],
    })
    assert r.status_code == 200, r.text
    tid2 = await _make_task(ws, result_summary="Paris", reference_answer="paris")
    assert (await auth_client.post(f"/api/quality/records/{tid2}/evaluate")).status_code == 200
    profile = (await auth_client.get(f"/api/quality/records/{tid2}/profile")).json()["quality_profile"]
    assert profile["rubric_id"] == outcome["rubric_id"]
    assert profile["rubric_fingerprint"] != before


@pytest.mark.asyncio
async def test_write_is_role_gated_but_read_is_not(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)
    member = await _join_workspace(auth_client, ws, "member")

    body = {"dimensions": [{"key": "correctness", "name": "Correctness", "score": 1}]}
    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json=body, headers=member)
    assert r.status_code == 403

    # ...but a member can still see the annotation
    assert (await auth_client.put(
        f"/api/quality/records/{tid}/feedback",
        json={"dimensions": [{"key": "correctness", "name": "Correctness", "score": 7}]},
    )).status_code == 200
    r = await auth_client.get(f"/api/quality/records/{tid}/feedback", headers=member)
    assert r.status_code == 200
    assert r.json()["human_feedback"]["dimensions"][0]["score"] == 7
    r = await auth_client.get(f"/api/quality/records/{tid}/annotations", headers=member)
    assert r.status_code == 200 and len(r.json()["annotations"]) == 1
