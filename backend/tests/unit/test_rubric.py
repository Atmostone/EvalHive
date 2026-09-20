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

def _resp(score=None, reasoning="ok", pt=10, ct=4, applicable=None):
    args = {"reasoning": reasoning}
    if applicable is not None:
        args["applicable"] = applicable
    if score is not None:
        args["score"] = score
    fn = MagicMock()
    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.function = fn
    msg = MagicMock()
    msg.tool_calls = [tc]
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    def __init__(self, score=8, fail_contains=(), na_contains=()):
        self.score = score
        self.fail_contains = tuple(fail_contains)
        # Dimensions whose user-message contains any of these markers are answered
        # with applicable=false (and NO score) instead of a numeric score.
        self.na_contains = tuple(na_contains)
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        content = kwargs["messages"][1]["content"]
        if any(f in content for f in self.fail_contains):
            raise RuntimeError("boom")
        if any(f in content for f in self.na_contains):
            return _resp(applicable=False, reasoning="not applicable to this task")
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


async def test_rubric_override_takes_precedence(db_session, default_model, monkeypatch):
    # A stored default rubric exists, but the inline per-case rubric must win.
    await _flush(db_session, _rubric("Stored", is_default=True, dimensions=[_dim("stored")]))
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, result_summary="r")
    await _flush(db_session, task)

    fake = _FakeProvider(score=7)
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)
    profile = await judge_mod.evaluate_task_quality(
        db_session, task, commit=False,
        rubric_override={
            "name": "Case rubric",
            "dimensions": [
                {"key": "exact", "name": "Exact", "description": "", "evaluator": "judge",
                 "weight": 1.0, "threshold": 5, "critical": False},
            ],
        },
    )
    assert profile["rubric_name"] == "Case rubric"
    assert profile["rubric_id"] is None  # inline, not a stored rubric row
    assert [d["key"] for d in profile["dimensions"]] == ["exact"]
    assert profile["weighted_score"] == 7.0


async def test_rubric_override_without_dimensions_falls_back(db_session, default_model, monkeypatch):
    await _flush(db_session, _rubric("Stored", is_default=True, dimensions=[_dim("stored")]))
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, result_summary="r")
    await _flush(db_session, task)

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider(score=6))
    profile = await judge_mod.evaluate_task_quality(
        db_session, task, commit=False, rubric_override={"name": "broken"}
    )
    assert profile["rubric_name"] == "Stored"


async def test_deliverable_file_contents_reach_judge(db_session, default_model, monkeypatch):
    # Agents often save the deliverable to a file and only describe it in
    # result_summary — the judge must see the file contents (SPA-47).
    await _flush(db_session, _rubric("R", is_default=True, dimensions=[_dim("a")]))
    task = Task(
        title="write email", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="The email has been written and saved.",
        result_files=["results/tid/email.md", "results/tid/logo.png"],
    )
    await _flush(db_session, task)

    def fake_read(path, max_bytes=16_384):
        if path.endswith("email.md"):
            return "Dear team, the Q3 launch moves to Friday."
        return None  # binary

    import app.storage.minio_client as minio_mod
    monkeypatch.setattr(minio_mod, "read_result_file_text", fake_read)

    seen = {}

    class _Capture(_FakeProvider):
        async def acompletion(self, **kwargs):
            seen["context"] = kwargs["messages"][1]["content"]
            return await super().acompletion(**kwargs)

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _Capture(score=8))
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile is not None
    assert "Dear team, the Q3 launch moves to Friday." in seen["context"]
    assert "email.md" in seen["context"]
    assert "(binary file, content not shown)" in seen["context"]


def test_result_context_files_only_withholds_self_report():
    # SPA-70: judge-mode open-ended runs grade the artifacts, not the agent's
    # self-report — outcome_files_only drops result_summary from the judge
    # context so a flattering summary can't over-credit a thin/absent deliverable.
    task = Task(
        title="write report", status=TaskStatus.DONE.value, workspace_id=WS,
        description="produce the Q3 report",
        result_summary="I built a comprehensive, flawless report covering everything.",
        result_files=["results/tid/report.docx"],
    )
    with_summary = judge_mod._result_context(task)
    files_only = judge_mod._result_context(task, include_summary=False)
    # the self-report is present by default, withheld in files-only mode
    assert "comprehensive, flawless" in with_summary
    assert "comprehensive, flawless" not in files_only
    assert "withheld by design" in files_only
    # the task prompt and the file list survive either way
    assert "produce the Q3 report" in files_only
    assert "report.docx" in files_only


async def test_binary_deliverable_reaches_judge_as_markdown(
    db_session, default_model, monkeypatch
):
    # SPA-71: a binary deliverable (docx/pdf/xlsx) used to show "(binary file,
    # content not shown)"; now it is converted to Markdown and reaches the judge.
    await _flush(db_session, _rubric("R", is_default=True, dimensions=[_dim("a")]))
    task = Task(
        title="write report", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="Report saved.",
        result_files=["results/tid/report.docx", "results/tid/logo.png"],
    )
    await _flush(db_session, task)

    import app.storage.minio_client as minio_mod
    monkeypatch.setattr(minio_mod, "read_result_file_text", lambda path, max_bytes=16_384: None)

    import app.storage.artifact_markdown as md_mod

    def fake_md(path, max_bytes=md_mod._MAX_CONVERT_BYTES):
        if path.endswith("report.docx"):
            return "# Quarterly Report\nThe Q3 launch moves to Friday."
        return None  # logo.png is genuinely unconvertible

    monkeypatch.setattr(md_mod, "result_file_markdown", fake_md)

    seen = {}

    class _Capture(_FakeProvider):
        async def acompletion(self, **kwargs):
            seen["context"] = kwargs["messages"][1]["content"]
            return await super().acompletion(**kwargs)

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _Capture(score=8))
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile is not None
    assert "The Q3 launch moves to Friday." in seen["context"]
    assert "report.docx" in seen["context"]
    assert "(binary file, content not shown)" in seen["context"]  # logo.png


async def test_conversion_failure_does_not_break_eval(db_session, default_model, monkeypatch):
    # SPA-71: the converter raising must degrade to the note, not break the eval.
    await _flush(db_session, _rubric("R", is_default=True, dimensions=[_dim("a")]))
    task = Task(
        title="x", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="done", result_files=["results/tid/report.docx"],
    )
    await _flush(db_session, task)

    import app.storage.minio_client as minio_mod
    monkeypatch.setattr(minio_mod, "read_result_file_text", lambda path, max_bytes=16_384: None)

    import app.storage.artifact_markdown as md_mod

    def boom(path, max_bytes=md_mod._MAX_CONVERT_BYTES):
        raise RuntimeError("converter exploded")

    monkeypatch.setattr(md_mod, "result_file_markdown", boom)
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider(score=7))
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile is not None and profile["weighted_score"] == 7.0


async def test_storage_failure_does_not_break_eval(db_session, default_model, monkeypatch):
    await _flush(db_session, _rubric("R", is_default=True, dimensions=[_dim("a")]))
    task = Task(
        title="x", status=TaskStatus.DONE.value, workspace_id=WS,
        result_summary="done", result_files=["results/tid/gone.txt"],
    )
    await _flush(db_session, task)

    import app.storage.minio_client as minio_mod

    def boom(path, max_bytes=16_384):
        raise RuntimeError("minio down")

    monkeypatch.setattr(minio_mod, "read_result_file_text", boom)
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider(score=7))
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile is not None and profile["weighted_score"] == 7.0


async def test_eval_skipped_without_judge_model(db_session, monkeypatch):
    # No system model configured on the workspace → evaluation is skipped.
    await _flush(db_session, _rubric("R", is_default=True, dimensions=[_dim("a")]))
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS, result_summary="r")
    await _flush(db_session, task)

    fake = _FakeProvider()
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)
    assert await judge_mod.evaluate_task_quality(db_session, task, commit=False) is None
    assert fake.calls == 0


# ---- not_applicable (renormalization) --------------------------------------

async def test_not_applicable_dimension_excluded_and_renormalizes(
    db_session, default_model, monkeypatch
):
    # A dimension the judge marks applicable=false is EXCLUDED from the weighted
    # aggregate: the score renormalizes over the remaining scored axes, the N/A
    # axis is not scored 0 and does not enter the gate or failed_dimensions.
    rubric = _rubric("R", is_default=True, dimensions=[
        _dim("present", weight=0.5, threshold=6, critical=True),
        _dim("absent", weight=0.5, threshold=6, critical=True),  # → not_applicable
    ])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="r", model_used="m")
    await _flush(db_session, task)

    # 'absent' is answered applicable=false; 'present' scores 8.
    fake = _FakeProvider(score=8, na_contains=["Dimension: Absent"])
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["absent"]["status"] == "not_applicable"
    assert dims["absent"]["score"] is None
    assert dims["absent"]["passed"] is True            # not a critical failure
    assert dims["present"]["status"] == "scored" and dims["present"]["score"] == 8
    # weighted renormalizes over the single scored axis: 8*0.5 / 0.5 == 8.0
    assert profile["weighted_score"] == 8.0
    # the N/A critical axis must NOT fail the gate
    assert profile["gate"]["passed"] is True
    assert "absent" not in profile["gate"]["failed_dimensions"]


async def test_all_dimensions_not_applicable_yields_no_score(
    db_session, default_model, monkeypatch
):
    # If every dimension is N/A, weighted_den is 0 → weighted_score is None
    # (consistent with the skipped-all behavior), and the gate still passes.
    rubric = _rubric("R", is_default=True, dimensions=[
        _dim("a", weight=0.5, critical=True),
        _dim("b", weight=0.5),
    ])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="r", model_used="m")
    await _flush(db_session, task)

    fake = _FakeProvider(na_contains=["Dimension: A", "Dimension: B"])
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: fake)
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)
    assert profile["weighted_score"] is None
    assert profile["gate"]["passed"] is True
    assert profile["gate"]["failed_dimensions"] == []


def test_judge_tool_requires_a_score():
    # The contract, pinned: `score` must stay REQUIRED in the tool schema. While
    # it was optional ("omit when applicable=false"), a judge that reads the
    # schema literally replied with `reasoning` alone on every call — measured
    # 3/3 on glm-5.3-flash — and every dimension errored. The trajectory judge
    # has always required its score; these two must not drift apart again.
    required = judge_mod.JUDGE_TOOL[0]["function"]["parameters"]["required"]
    assert "score" in required and "reasoning" in required


async def test_missing_score_errors_by_name_instead_of_keyerror(
    db_session, default_model, monkeypatch
):
    # A reply with no score is still refused — a missing score is not a zero, and
    # inventing one would put a fabricated number into a weighted aggregate. What
    # changes is that the failure says what happened: it used to surface as the
    # bare string "'score'" (a KeyError's repr), which names nothing.
    rubric = _rubric("R", is_default=True, dimensions=[
        _dim("a", weight=1.0, threshold=6, critical=True),
    ])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="r", model_used="m")
    await _flush(db_session, task)

    class _NoScore:
        calls = 0

        async def acompletion(self, **kwargs):
            _NoScore.calls += 1
            return _resp(score=None, reasoning="thought about it")  # no score, no applicable

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _NoScore())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dim = profile["dimensions"][0]
    assert dim["status"] == "error" and dim["score"] is None
    assert "score" in dim["error"] and dim["error"] != "'score'"
    # A critical dimension that could not be certified fails the gate (SPA-51).
    assert profile["gate"]["passed"] is False


async def test_not_applicable_wins_over_a_supplied_score(
    db_session, default_model, monkeypatch
):
    # Now that `score` is required, a model marking a dimension inapplicable
    # sends BOTH — glm-5.3-flash answers `applicable=false, score=0`. The zero
    # must not reach the aggregate: `applicable` decides exclusion, the number
    # is ignored. Without this the axis would score 0 and drag the mean down.
    rubric = _rubric("R", is_default=True, dimensions=[
        _dim("keep", weight=0.5, threshold=6, critical=True),
        _dim("drop", weight=0.5, threshold=6, critical=True),
    ])
    await _flush(db_session, rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="r", model_used="m")
    await _flush(db_session, task)

    class _NAWithZero:
        async def acompletion(self, **kwargs):
            if "Dimension: Drop" in kwargs["messages"][1]["content"]:
                return _resp(score=0, applicable=False, reasoning="does not apply")
            return _resp(score=8)

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _NAWithZero())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["drop"]["status"] == "not_applicable" and dims["drop"]["score"] is None
    # Renormalized over the one scored axis — NOT (8 + 0) / 2.
    assert profile["weighted_score"] == 8.0
    assert profile["gate"]["passed"] is True


def test_toolathlon_rubric_in_defaults():
    # The Toolathlon tool-use/data rubric is an additive 6th default that flows
    # through iter_default_rubrics(); its weights sum to 1.0.
    from app.quality.rubric import DEFAULT_RUBRICS, iter_default_rubrics

    by_name = {r["name"]: r for r in DEFAULT_RUBRICS}
    assert "Tool Use / Data Task" in by_name
    tool = by_name["Tool Use / Data Task"]
    assert tool["applies_to"] == "toolathlon"
    assert tool["is_default"] is False
    keys = [d["key"] for d in tool["dimensions"]]
    assert keys == [
        "task_completion", "output_accuracy", "instruction_following",
        "format_compliance", "presentation_clarity",
    ]
    assert round(sum(d["weight"] for d in tool["dimensions"]), 6) == 1.0
    # the original 5 rubrics are untouched and it reaches iter_default_rubrics()
    seeded = {name for name, _ in iter_default_rubrics()}
    assert "Tool Use / Data Task" in seeded
    assert {"Analytical Report", "Code", "Content", "Design", "Data Analysis"} <= seeded
