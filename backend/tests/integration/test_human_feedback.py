"""Integration tests for annotation collection (E-05) and its ledger (SPA-85).

Feedback round-trips through PUT/GET, auto-creates the quality record when none
exists, pairs human scores with judge scores from the profile, surfaces in the
calibration export, is workspace-scoped, and rejects out-of-range scores.

The ledger adds: two annotators on one run stay two independent rows, the same
annotator re-rating supersedes their own previous row, a machine annotation
never displaces the human slot, the judge's side is frozen so a re-judge cannot
move a past pair, and the write is role-gated while the read is not.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app import database
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
async def test_blind_protocol_is_recorded(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_judged_record(ws)

    r = await auth_client.put(f"/api/quality/records/{tid}/feedback", json={
        "blind_to_judge": True,
        "blind_to_model": True,
        "dimensions": [{"key": "correctness", "name": "Correctness", "score": 4}],
    })
    assert r.status_code == 200, r.text
    row = (await auth_client.get(f"/api/quality/records/{tid}/annotations")).json()["annotations"][0]
    assert row["blind_to_judge"] is True and row["blind_to_model"] is True
    # the judge is still frozen for calibration — blindness is about what the
    # annotator saw, not about what we record
    assert row["judge_observation"]["outcome"]["scores"] == {"correctness": 8}


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
