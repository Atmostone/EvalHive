"""Integration tests for the Confidence Calibration endpoints (E-16).

POST /api/quality/records/{task_id}/evaluate-calibration runs a single post-hoc
self-probe (mocked LLM) on the task's deliverable to elicit a predicted
confidence, pairs it with the E-02 correctness derived from a pre-seeded
quality_profile, and writes (predicted_confidence, actual_correct, brier_term)
to quality_records.calibration_profile. GET .../calibration reads it back; GET
/api/quality/calibration/aggregate returns ECE / Brier / by-model /
recommendations. Skipped when there is no resolvable model, no deliverable, or
no correctness signal; workspace-scoped.
"""

import json
import uuid
from unittest.mock import MagicMock

from httpx import AsyncClient

from app import database
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace
from app.quality import calibration as cal_mod

_RESULT = "The capital of France is Madrid, which is clearly the correct answer."


def _resp(confidence=0.8, reasoning="I am fairly sure.", pt=120, ct=20):
    fn = MagicMock()
    fn.arguments = json.dumps({"confidence": confidence, "reasoning": reasoning})
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    def __init__(self, confidence=0.8):
        self._c = confidence

    async def acompletion(self, **kw):
        return _resp(confidence=self._c)


async def _seed_doer_model(s, workspace_id, *, api_name="m"):
    prov = Provider(workspace_id=workspace_id, name="p", api_key="k", endpoint="http://x/v1")
    s.add(prov)
    await s.flush()
    model = LLMModel(
        provider_id=prov.id, display_name="M", api_name=api_name,
        input_price_per_1m_usd=1, output_price_per_1m_usd=2,
    )
    s.add(model)
    await s.flush()
    return model


async def _seed_task(ws, *, result=_RESULT, model_used="m", weighted_score=4.0,
                     with_profile=True):
    """A DONE task with a deliverable and (optionally) a pre-scored E-02 profile.

    ``weighted_score`` below the 7.0 threshold makes the answer "incorrect", so a
    high probe confidence yields a large Brier term — the overconfident case.
    """
    async with database.async_session() as s:
        t = Task(title="trivia", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary=result, model_used=model_used)
        s.add(t)
        await s.flush()
        if with_profile:
            s.add(QualityRecord(
                task_id=t.id, workspace_id=ws, model_used=model_used,
                final_status=TaskStatus.DONE.value,
                quality_profile={"weighted_score": weighted_score, "gate": {"passed": False}},
            ))
        await s.commit()
        return str(t.id)


async def test_evaluate_calibration_scored(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_doer_model(s, ws)
        await s.commit()
    tid = await _seed_task(ws, weighted_score=4.0)  # below threshold → incorrect

    monkeypatch.setattr(cal_mod, "get_llm_provider", lambda: _FakeProvider(confidence=0.8))

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-calibration")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["calibration_profile"]
    assert prof["status"] == "scored"
    assert prof["predicted_confidence"] == 0.8
    assert prof["actual_correct"] is False
    assert prof["outcome_signal"] == "judge"
    # (0.8 - 0)^2 = 0.64
    assert prof["brier_term"] == 0.64
    assert prof["probe_model"] == "m"
    assert prof["used_outcome_profile"] is True

    # readable back via GET calibration
    r = await auth_client.get(f"/api/quality/records/{tid}/calibration")
    assert r.status_code == 200
    assert r.json()["calibration_profile"]["predicted_confidence"] == 0.8

    # aggregate reports ECE / Brier / by-model / recommendations
    r = await auth_client.get("/api/quality/calibration/aggregate")
    assert r.status_code == 200
    agg = r.json()
    assert agg["overall"]["count"] >= 1
    assert agg["overall"]["ece"] is not None
    assert agg["overall"]["brier"] is not None
    assert "m" in agg["by_model"]
    assert isinstance(agg["recommendations"], list)


async def test_evaluate_calibration_skipped_without_model(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        wsrow = await s.get(Workspace, ws)
        wsrow.orchestrator_model_id = None
        wsrow.chat_model_id = None
        wsrow.memory_extractor_model_id = None
        wsrow.quality_judge_model_id = None
        await s.commit()
    # model_used that matches no provider model → doer resolve fails, judge fallback
    # is also unconfigured → skip.
    tid = await _seed_task(ws, model_used="ghost")

    monkeypatch.setattr(cal_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-calibration")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["calibration_profile"] is None


async def test_evaluate_calibration_skipped_no_deliverable(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_doer_model(s, ws)
        await s.commit()
    tid = await _seed_task(ws, result="")

    monkeypatch.setattr(cal_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-calibration")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


async def test_evaluate_calibration_skipped_no_signal(auth_client: AsyncClient, monkeypatch):
    """No correctness signal in the E-02 profile (no reference dim, no weighted
    score) → there is nothing to calibrate against, so the run is skipped."""
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    async with database.async_session() as s:
        await _seed_doer_model(s, ws)
        await s.commit()
    async with database.async_session() as s:
        t = Task(title="x", status=TaskStatus.DONE.value, workspace_id=ws,
                 result_summary=_RESULT, model_used="m")
        s.add(t)
        await s.flush()
        s.add(QualityRecord(
            task_id=t.id, workspace_id=ws, model_used="m",
            quality_profile={"dimensions": []},  # no weighted_score, no reference dim
        ))
        await s.commit()
        tid = str(t.id)

    monkeypatch.setattr(cal_mod, "get_llm_provider", lambda: _FakeProvider())
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-calibration")
    assert r.status_code == 200
    assert r.json()["skipped"] is True


async def test_evaluate_calibration_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task(DEFAULT_WORKSPACE_ID)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-calibration")
    assert r.status_code == 404
