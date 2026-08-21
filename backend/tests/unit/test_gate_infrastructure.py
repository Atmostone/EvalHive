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


# --- the classification has to survive the profile ------------------------------- #


def test_every_profile_assembler_carries_the_error_class():
    """Computing `error_class` inside an evaluator and dropping it while building
    the profile leaves exactly the state this ticket set out to fix: an
    infrastructure failure that reads as bare text, indistinguishable from a
    judge that broke on its own. E-02 carried it from the start; the other five
    assemblers rebuilt their `errors` list by hand and lost it."""
    import re
    from pathlib import Path

    assemblers = {
        "trajectory.py": "E-07",
        "failure_modes.py": "E-14",
        "calibration.py": "E-16",
        "hallucination.py": "E-15",
        "trace_evidence.py": "E-08",
    }
    root = Path(__file__).resolve().parents[2] / "app" / "quality"
    for fname, evaluator in assemblers.items():
        src = (root / fname).read_text()
        # every dict that carries an "error" key into a profile must classify it
        bare = re.findall(r'\{\s*"error":[^}]*\}', src)
        # Non-vacuous: a pattern that matches nothing would pass this silently,
        # which is the failure mode a source-scanning test invites.
        assert bare, f"{evaluator} ({fname}): the scan found no error dict to check"
        for match in bare:
            assert "error_class" in match, f"{evaluator} ({fname}) drops error_class: {match}"


async def test_e07_reports_a_non_compliant_provider_as_infrastructure(
    db_session, default_model, monkeypatch
):
    """The same contract, proven by running it rather than by reading the source:
    E-07 is the assembler that matters most, since the trajectory score is one of
    the two headline metrics."""
    from app.quality import trajectory as traj_mod

    async def _fake_trace(*_a, **_kw):
        return {
            "task": {"id": "t", "title": "T", "description": "D"},
            "steps": [{"seq": 0, "kind": "tool", "tool_name": "bash",
                       "arguments": {"cmd": "ls"}, "content": "a.txt"}],
            "stats": {}, "config": {},
        }

    llm = SimpleNamespace(
        model=SimpleNamespace(api_name="m", input_price_per_1m_usd=1, output_price_per_1m_usd=2),
        provider=SimpleNamespace(api_key="k", endpoint="http://x"),
    )

    async def _fake_resolve(*_a, **_kw):
        return llm

    monkeypatch.setattr(traj_mod, "build_cleaned_trace", _fake_trace)
    monkeypatch.setattr(traj_mod, "_resolve_judge_model", _fake_resolve)
    monkeypatch.setattr(traj_mod, "get_llm_provider", lambda: _NonCompliantProvider())

    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="done", model_used="m")
    db_session.add(task)
    await db_session.flush()
    profile = await traj_mod.evaluate_task_trajectory(
        db_session, task, commit=False, max_input_tokens=0
    )
    assert profile["status"] == "error"
    assert profile["errors"][0]["error_class"] == "infrastructure"
