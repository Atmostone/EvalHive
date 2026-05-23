"""Unit tests for the Quality Rubric Engine (E-02): resolution + judge assembly."""

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.models.quality_record import QualityRecord
from app.models.rubric import Rubric
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality import judge as judge_mod
from app.quality.rubric import resolve_rubric_for_task

pytestmark = pytest.mark.asyncio

WS = DEFAULT_WORKSPACE_ID


def _rubric(name, *, applies_to=None, is_default=False, dimensions=None):
    return Rubric(
        workspace_id=WS, name=name, applies_to=applies_to,
        is_default=is_default, dimensions=dimensions or [],
    )


def _dim(key, **kw):
    base = {"key": key, "name": key.title(), "description": "", "evaluator": "judge",
            "weight": 1.0, "threshold": 5, "critical": False}
    base.update(kw)
    return base


async def _flush(db, *objs):
    for o in objs:
        db.add(o)
    await db.flush()


# ---- LLM provider fake (mirrors litellm response shape) -------------------

def _resp(score, reasoning="ok", pt=10, ct=4):
    fn = MagicMock()
    fn.arguments = json.dumps({"score": score, "reasoning": reasoning})
    tc = MagicMock()
    tc.function = fn
    msg = MagicMock()
    msg.tool_calls = [tc]
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    def __init__(self, score=8, fail_contains=()):
        self.score = score
        self.fail_contains = tuple(fail_contains)
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        content = kwargs["messages"][1]["content"]
        if any(f in content for f in self.fail_contains):
            raise RuntimeError("boom")
        return _resp(self.score)


# ---- resolution precedence -------------------------------------------------

async def test_resolve_explicit_template_rubric(db_session):
    r = _rubric("Explicit", dimensions=[_dim("a")])
    await _flush(db_session, r)
    tpl = Template(name="T", description="d", soul_md="s", workspace_id=WS,
                   rubric_id=r.id, tags=["coding"])
    await _flush(db_session, tpl)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, template_id=tpl.id)
    await _flush(db_session, task)

    got = await resolve_rubric_for_task(db_session, task)
    assert got is not None and got.id == r.id


async def test_resolve_by_tag(db_session):
    await _flush(db_session, _rubric("Code", applies_to="coding"))
    tpl = Template(name="T", description="d", soul_md="s", workspace_id=WS, tags=["coding"])
    await _flush(db_session, tpl)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, template_id=tpl.id)
    await _flush(db_session, task)

    got = await resolve_rubric_for_task(db_session, task)
    assert got is not None and got.applies_to == "coding"


async def test_resolve_default_fallback(db_session):
    await _flush(db_session, _rubric("Default", is_default=True))
    tpl = Template(name="T", description="d", soul_md="s", workspace_id=WS, tags=["nomatch"])
    await _flush(db_session, tpl)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, template_id=tpl.id)
    await _flush(db_session, task)

    got = await resolve_rubric_for_task(db_session, task)
    assert got is not None and got.is_default is True


async def test_resolve_none_when_no_rubric(db_session):
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS)
    await _flush(db_session, task)
    assert await resolve_rubric_for_task(db_session, task) is None


# ---- judge assembly --------------------------------------------------------

async def test_profile_shape_and_slot_written(db_session, default_model, monkeypatch):
    rubric = _rubric("R", is_default=True, dimensions=[
        _dim("a", weight=0.5, threshold=6, critical=True),
        _dim("b", weight=0.5, threshold=6, critical=False),
        _dim("c", evaluator="human", weight=1, threshold=5),  # deferred (E-05)
    ])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="result", model_used="m")
    await _flush(db_session, task)

    fake = _FakeProvider(score=8)
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)

    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile is not None
    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["a"]["status"] == "scored" and dims["a"]["score"] == 8 and dims["a"]["passed"]
    assert dims["c"]["status"] == "deferred" and dims["c"]["score"] is None
    assert profile["gate"]["passed"] is True
    assert profile["weighted_score"] == 8.0
    assert fake.calls == 2  # only judge dimensions hit the LLM

    rec = (
        await db_session.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one()
    assert rec.quality_profile["rubric_name"] == "R"


async def test_gate_fails_when_critical_below_threshold(db_session, default_model, monkeypatch):
    rubric = _rubric("R", is_default=True, dimensions=[_dim("a", threshold=9, critical=True)])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, result_summary="r")
    await _flush(db_session, task)

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider(score=5))
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile["gate"]["passed"] is False
    assert "a" in profile["gate"]["failed_dimensions"]


async def test_dimension_error_does_not_block_others(db_session, default_model, monkeypatch):
    rubric = _rubric("R", is_default=True, dimensions=[_dim("good"), _dim("bad")])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, result_summary="r")
    await _flush(db_session, task)

    fake = _FakeProvider(score=8, fail_contains=["Dimension: Bad"])
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["good"]["status"] == "scored"
    assert dims["bad"]["status"] == "error"
    assert len(profile["errors"]) == 1


async def test_eval_skipped_without_judge_model(db_session, monkeypatch):
    # No system model configured on the workspace → evaluation is skipped.
    await _flush(db_session, _rubric("R", is_default=True, dimensions=[_dim("a")]))
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, result_summary="r")
    await _flush(db_session, task)

    fake = _FakeProvider()
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)
    assert await judge_mod.evaluate_task_quality(db_session, task, commit=False) is None
    assert fake.calls == 0
