"""A gate failure the deliverable did not earn (SPA-111).

Every judge call forces a tool choice by name. A provider that treats that as
advisory answers in plain text instead, the dimension cannot be scored, and
SPA-51 fails a critical dimension CLOSED — correctly, because nothing was
certified. What was wrong is that the resulting verdict was indistinguishable
from one the work actually failed, so an unusable endpoint read as a bad model.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.rubric import Rubric
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality import judge as judge_mod

pytestmark = pytest.mark.asyncio

WS = DEFAULT_WORKSPACE_ID


def _dim(key, *, critical):
    return {
        "key": key,
        "name": key.title(),
        "description": "",
        "evaluator": "judge",
        "weight": 1.0,
        "threshold": 5,
        "critical": critical,
    }


def _message(*, tool_args=None, content=None):
    return SimpleNamespace(
        tool_calls=(
            [SimpleNamespace(function=SimpleNamespace(arguments=tool_args))]
            if tool_args is not None
            else None
        ),
        content=content,
    )


class _NonCompliantProvider:
    """Answers in prose, never with the tool it was told to call."""

    async def acompletion(self, **_kwargs):
        resp = MagicMock()
        resp.choices = [
            MagicMock(message=_message(content="Sure! The work looks good to me."))
        ]
        resp.usage = {"prompt_tokens": 10, "completion_tokens": 4}
        return resp


class _TalkativeButCompliantProvider:
    """Ignores the tool call but does emit the JSON — recoverable, not a fault."""

    async def acompletion(self, **_kwargs):
        resp = MagicMock()
        resp.choices = [
            MagicMock(
                message=_message(
                    content='Here you go: {"score": 8, "reasoning": "solid"}'
                )
            )
        ]
        resp.usage = {"prompt_tokens": 10, "completion_tokens": 4}
        return resp


async def test_provider_non_compliance_is_labelled_infrastructure(
    db_session, default_model, monkeypatch
):
    rubric = Rubric(
        workspace_id=WS, name="R", is_default=True, dimensions=[_dim("quality", critical=True)]
    )
    db_session.add(rubric)
    task = Task(
        title="x", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="a deliverable", model_used="m",
    )
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _NonCompliantProvider())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dim = profile["dimensions"][0]
    assert dim["status"] == "error"
    assert dim["error_class"] == "infrastructure"
    # The verdict is UNCHANGED — SPA-51 still fails an uncertifiable critical
    # dimension closed. What is new is that the reason travels with it.
    assert profile["gate"]["passed"] is False
    assert profile["gate"]["failed_dimensions"] == ["quality"]
    assert profile["gate"]["uncertifiable_dimensions"] == ["quality"]
    assert profile["errors"][0]["error_class"] == "infrastructure"


async def test_an_ordinary_evaluator_failure_is_not_called_infrastructure(
    db_session, default_model, monkeypatch
):
    """The distinction has to cut both ways, or it says nothing: a judge that
    errored on its own is still the platform's problem to fix, not the
    provider's refusal to answer."""

    class _Exploding:
        async def acompletion(self, **_kwargs):
            raise RuntimeError("boom")

    rubric = Rubric(
        workspace_id=WS, name="R", is_default=True, dimensions=[_dim("quality", critical=True)]
    )
    db_session.add(rubric)
    task = Task(
        title="x", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="a deliverable", model_used="m",
    )
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _Exploding())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    assert profile["dimensions"][0]["error_class"] == "evaluation"
    assert profile["gate"]["passed"] is False
    assert profile["gate"]["failed_dimensions"] == ["quality"]
    assert profile["gate"]["uncertifiable_dimensions"] == []


async def test_a_recovered_answer_is_scored_normally(
    db_session, default_model, monkeypatch
):
    """Ignoring the forced tool call is only a fault when nothing usable comes
    back. A provider that answers in prose but includes the object is simply
    read — that is the whole point of the lenient path."""
    rubric = Rubric(
        workspace_id=WS, name="R", is_default=True, dimensions=[_dim("quality", critical=True)]
    )
    db_session.add(rubric)
    task = Task(
        title="x", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="a deliverable", model_used="m",
    )
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(
        judge_mod, "get_llm_provider", lambda: _TalkativeButCompliantProvider()
    )
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    assert profile["dimensions"][0]["status"] == "scored"
    assert profile["dimensions"][0]["score"] == 8
    assert profile["gate"]["passed"] is True
    assert profile["gate"]["uncertifiable_dimensions"] == []
