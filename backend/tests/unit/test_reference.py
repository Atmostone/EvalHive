"""Unit tests for the Reference-based Judge (E-03).

Covers the matching modes, the skipped/error contract of
``evaluate_reference_dimension``, and its integration into the E-02 profile.
"""

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models.quality_record import QualityRecord
from app.models.rubric import Rubric
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality import judge as judge_mod
from app.quality import reference as ref

pytestmark = pytest.mark.asyncio

WS = DEFAULT_WORKSPACE_ID


def _judge_llm():
    llm = MagicMock()
    llm.model.api_name = "m"
    llm.model.input_price_per_1m_usd = 1
    llm.model.output_price_per_1m_usd = 2
    llm.provider.api_key = "k"
    llm.provider.endpoint = "http://x/v1"
    return llm


def _ref_dim(key="answer", *, mode="exact", weight=1.0, threshold=5, critical=False):
    return {"key": key, "name": key.title(), "description": "", "evaluator": "reference",
            "reference_mode": mode, "weight": weight, "threshold": threshold, "critical": critical}


# ---- pure matchers ---------------------------------------------------------

def test_exact_match_normalizes():
    assert ref.exact_match("  Paris ", "paris") == 1.0
    assert ref.exact_match("Paris", "London") == 0.0


def test_fuzzy_match_ratio():
    assert ref.fuzzy_match("abc", "abc") == 1.0
    assert ref.fuzzy_match("abc", "xyz") == 0.0
    assert 0.0 < ref.fuzzy_match("the quick brown fox", "the quick brown box") < 1.0


def test_cosine():
    assert ref._cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)
    assert ref._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert ref._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector → 0, no div error


async def test_semantic_match_uses_embeddings(monkeypatch):
    async def fake_embed(texts):
        assert len(texts) == 2
        return [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]]

    monkeypatch.setattr("app.knowledge.rag.get_embeddings", fake_embed)
    assert await ref.semantic_match("a", "b") == pytest.approx(1.0)


# ---- evaluate_reference_dimension contract ---------------------------------

async def test_skipped_without_reference_answer():
    task = Task(title="x", result_summary="anything", reference_answer=None)
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="exact"), task, _judge_llm())
    assert out["status"] == "skipped" and out["score"] is None


async def test_exact_mode_scored():
    task = Task(title="x", result_summary="Paris", reference_answer="paris")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="exact"), task, None)
    assert out["status"] == "scored" and out["score"] == 10

    task2 = Task(title="x", result_summary="London", reference_answer="paris")
    out2 = await ref.evaluate_reference_dimension(_ref_dim(mode="exact"), task2, None)
    assert out2["status"] == "scored" and out2["score"] == 0


async def test_fuzzy_mode_scored():
    task = Task(title="x", result_summary="the quick brown fox",
                reference_answer="the quick brown box")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="fuzzy"), task, None)
    assert out["status"] == "scored" and 0 < out["score"] < 10


async def test_semantic_mode_scored(monkeypatch):
    async def fake_embed(texts):
        return [[0.0, 1.0], [1.0, 0.0]]  # orthogonal → cosine 0 → score 0

    monkeypatch.setattr("app.knowledge.rag.get_embeddings", fake_embed)
    task = Task(title="x", result_summary="a", reference_answer="b")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="semantic"), task, None)
    assert out["status"] == "scored" and out["score"] == 0


async def test_pointwise_mode_calls_llm(monkeypatch):
    fn = MagicMock()
    fn.arguments = json.dumps({"score": 7, "reasoning": "close"})
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[MagicMock(function=fn)]))]
    resp.usage = {"prompt_tokens": 12, "completion_tokens": 3}

    provider = MagicMock()

    async def acompletion(**kwargs):
        return resp

    provider.acompletion = acompletion
    monkeypatch.setattr(ref, "get_llm_provider", lambda: provider)

    task = Task(title="x", result_summary="Paris is the capital", reference_answer="Paris")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="pointwise"), task, _judge_llm())
    assert out["status"] == "scored" and out["score"] == 7
    assert out["input_tokens"] == 12 and out["output_tokens"] == 3


async def test_pointwise_without_model_errors():
    task = Task(title="x", result_summary="r", reference_answer="ref")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="pointwise"), task, None)
    assert out["status"] == "error" and out["score"] is None


async def test_unknown_mode_errors():
    task = Task(title="x", result_summary="r", reference_answer="ref")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="bogus"), task, _judge_llm())
    assert out["status"] == "error"


async def test_dimension_never_raises(monkeypatch):
    async def boom(texts):
        raise RuntimeError("embeddings down")

    monkeypatch.setattr("app.knowledge.rag.get_embeddings", boom)
    task = Task(title="x", result_summary="r", reference_answer="ref")
    out = await ref.evaluate_reference_dimension(_ref_dim(mode="semantic"), task, None)
    assert out["status"] == "error" and "embeddings down" in out["error"]


# ---- integration into the E-02 profile -------------------------------------

def _resp(score):
    fn = MagicMock()
    fn.arguments = json.dumps({"score": score, "reasoning": "ok"})
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[MagicMock(function=fn)]))]
    resp.usage = {"prompt_tokens": 10, "completion_tokens": 4}
    return resp


class _FakeProvider:
    async def acompletion(self, **kwargs):
        return _resp(8)


async def test_reference_dim_folds_into_profile(db_session, default_model, monkeypatch):
    rubric = Rubric(workspace_id=WS, name="R", is_default=True, dimensions=[
        {"key": "quality", "name": "Quality", "evaluator": "judge",
         "weight": 0.5, "threshold": 5, "critical": False},
        _ref_dim("answer", mode="exact", weight=0.5, threshold=6, critical=True),
    ])
    db_session.add(rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="Paris", reference_answer="paris", model_used="m")
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["answer"]["evaluator"] == "reference"
    assert dims["answer"]["reference_mode"] == "exact"
    assert dims["answer"]["status"] == "scored" and dims["answer"]["score"] == 10
    assert dims["answer"]["passed"] is True
    # weighted over judge(8, w0.5) + reference(10, w0.5) = 9.0
    assert profile["weighted_score"] == 9.0
    assert profile["gate"]["passed"] is True


async def test_reference_dim_skipped_without_reference(db_session, default_model, monkeypatch):
    rubric = Rubric(workspace_id=WS, name="R", is_default=True, dimensions=[
        {"key": "quality", "name": "Quality", "evaluator": "judge",
         "weight": 1.0, "threshold": 5, "critical": False},
        _ref_dim("answer", mode="exact", weight=1.0, threshold=6, critical=True),
    ])
    db_session.add(rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="Paris", reference_answer=None, model_used="m")
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["answer"]["status"] == "skipped" and dims["answer"]["score"] is None
    # skipped reference does not enter weighted_score nor fail the gate
    assert profile["weighted_score"] == 8.0
    assert profile["gate"]["passed"] is True
    assert profile["errors"] == []

    rec = (
        await db_session.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one()
    assert rec.quality_profile["schema_version"] == 2
