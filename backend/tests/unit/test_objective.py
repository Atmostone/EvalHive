"""Unit tests for the Behavioral / objective evaluator (E-04).

Covers the score mapping, the real ruff/mypy probe runners, the
skipped/error/cache contract of ``evaluate_objective_dimension``, and its
integration into the E-02 profile.
"""

import pytest
from sqlalchemy import select

from app.models.quality_record import QualityRecord
from app.models.rubric import Rubric
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality import judge as judge_mod
from app.quality import objective as obj

pytestmark = pytest.mark.asyncio

WS = DEFAULT_WORKSPACE_ID

CLEAN = b"x = 1\n"
LINT_DIRTY = b"import os\nx = 1\n"  # F401: 'os' imported but unused
TYPE_DIRTY = b"def f(a: int) -> int:\n    return a\n\n\nf('not an int')\n"


def _obj_dim(key="code", *, probe="lint", weight=1.0, threshold=5, critical=False):
    return {"key": key, "name": key.title(), "description": "", "evaluator": "objective",
            "probe": probe, "weight": weight, "threshold": threshold, "critical": critical}


# ---- pure helpers ----------------------------------------------------------

def test_density_score_clean_is_max():
    assert obj._density_score(0, 100) == 10


def test_density_score_monotonic_and_floor():
    assert obj._density_score(5, 100) == 5          # 5 issues / 100 LOC → 5
    assert obj._density_score(10, 100) == 0         # at the zero-density cap
    assert obj._density_score(50, 100) == 0         # past the cap stays 0
    assert obj._density_score(1, 0) == 0            # guards against div-by-zero (loc→1)


def test_count_loc_ignores_blank_lines(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n\n   \ny = 2\n")
    assert obj._count_loc([str(p)]) == 2


# ---- real probe runners (ruff / mypy must be installed) --------------------

async def test_ruff_counts_findings(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_bytes(CLEAN)
    assert await obj._run_ruff([str(clean)], str(tmp_path)) == 0

    dirty = tmp_path / "dirty.py"
    dirty.write_bytes(LINT_DIRTY)
    assert await obj._run_ruff([str(dirty)], str(tmp_path)) >= 1


async def test_mypy_counts_errors(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_bytes(CLEAN)
    assert await obj._run_mypy([str(clean)], str(tmp_path)) == 0

    dirty = tmp_path / "dirty.py"
    dirty.write_bytes(TYPE_DIRTY)
    assert await obj._run_mypy([str(dirty)], str(tmp_path)) >= 1


# ---- evaluate_objective_dimension contract ---------------------------------

async def test_skipped_without_code_artifacts():
    task = Task(title="x", result_files=["results/t/report.md", "results/t/data.json"])
    out = await obj.evaluate_objective_dimension(_obj_dim(probe="lint"), task)
    assert out["status"] == "skipped" and out["score"] is None


async def test_skipped_without_any_files():
    task = Task(title="x", result_files=None)
    out = await obj.evaluate_objective_dimension(_obj_dim(), task)
    assert out["status"] == "skipped"


async def test_lint_clean_scores_max(monkeypatch):
    obj._CACHE.clear()
    monkeypatch.setattr(obj, "_read_artifact", lambda path: CLEAN)
    task = Task(title="x", result_files=["results/t/clean.py"])
    out = await obj.evaluate_objective_dimension(_obj_dim(probe="lint"), task)
    assert out["status"] == "scored" and out["score"] == 10
    assert out["input_tokens"] == 0 and out["output_tokens"] == 0


async def test_lint_dirty_scores_below_max(monkeypatch):
    obj._CACHE.clear()
    monkeypatch.setattr(obj, "_read_artifact", lambda path: LINT_DIRTY)
    task = Task(title="x", result_files=["results/t/dirty.py"])
    out = await obj.evaluate_objective_dimension(_obj_dim(probe="lint"), task)
    assert out["status"] == "scored" and 0 <= out["score"] < 10


async def test_unknown_probe_errors():
    task = Task(title="x", result_files=["results/t/x.py"])
    out = await obj.evaluate_objective_dimension(_obj_dim(probe="bogus"), task)
    assert out["status"] == "error" and out["score"] is None


async def test_dimension_never_raises(monkeypatch):
    def boom(path):
        raise RuntimeError("storage down")

    monkeypatch.setattr(obj, "_read_artifact", boom)
    task = Task(title="x", result_files=["results/t/x.py"])
    out = await obj.evaluate_objective_dimension(_obj_dim(), task)
    assert out["status"] == "error" and "storage down" in out["error"]


async def test_results_are_cached(monkeypatch):
    obj._CACHE.clear()
    monkeypatch.setattr(obj, "_read_artifact", lambda path: CLEAN)

    calls = {"n": 0}
    real_probe = obj._run_probe

    async def counting_probe(probe, files, cwd):
        calls["n"] += 1
        return await real_probe(probe, files, cwd)

    monkeypatch.setattr(obj, "_run_probe", counting_probe)
    task = Task(title="x", result_files=["results/t/clean.py"])

    first = await obj.evaluate_objective_dimension(_obj_dim(), task)
    second = await obj.evaluate_objective_dimension(_obj_dim(), task)
    assert first == second and calls["n"] == 1  # identical artifact → probe ran once


# ---- integration into the E-02 profile -------------------------------------

import json  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


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


async def test_objective_dim_folds_into_profile(db_session, default_model, monkeypatch):
    obj._CACHE.clear()
    rubric = Rubric(workspace_id=WS, name="R", is_default=True, dimensions=[
        {"key": "quality", "name": "Quality", "evaluator": "judge",
         "weight": 0.5, "threshold": 5, "critical": False},
        _obj_dim("code", probe="lint", weight=0.5, threshold=6, critical=True),
    ])
    db_session.add(rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="done", result_files=["results/t/clean.py"], model_used="m")
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider())
    monkeypatch.setattr(obj, "_read_artifact", lambda path: CLEAN)
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dims = {d["key"]: d for d in profile["dimensions"]}
    assert dims["code"]["evaluator"] == "objective" and dims["code"]["probe"] == "lint"
    assert dims["code"]["status"] == "scored" and dims["code"]["score"] == 10
    assert dims["code"]["passed"] is True
    # weighted over judge(8, w0.5) + objective(10, w0.5) = 9.0
    assert profile["weighted_score"] == 9.0
    assert profile["gate"]["passed"] is True
    assert profile["schema_version"] == 2


async def test_objective_dim_skipped_without_code(db_session, default_model, monkeypatch):
    obj._CACHE.clear()
    rubric = Rubric(workspace_id=WS, name="R", is_default=True, dimensions=[
        {"key": "quality", "name": "Quality", "evaluator": "judge",
         "weight": 1.0, "threshold": 5, "critical": False},
        _obj_dim("code", probe="lint", weight=1.0, threshold=6, critical=True),
    ])
    db_session.add(rubric)
    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=WS,
                result_summary="done", result_files=["results/t/report.md"], model_used="m")
    db_session.add(task)
    await db_session.flush()

    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _FakeProvider())
    profile = await judge_mod.evaluate_task_quality(db_session, task, commit=False)

    dims = {d["key"]: d for d in profile["dimensions"]}
    # no Python artifact → skipped; critical dim does not fail the gate
    assert dims["code"]["status"] == "skipped" and dims["code"]["score"] is None
    assert profile["weighted_score"] == 8.0
    assert profile["gate"]["passed"] is True
    assert profile["errors"] == []

    rec = (
        await db_session.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one()
    assert rec.quality_profile["schema_version"] == 2
