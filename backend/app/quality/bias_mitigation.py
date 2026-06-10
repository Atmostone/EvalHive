"""Bias Mitigation Toolkit (E-18).

E-17 *measures* how far the LLM judge diverges from humans; E-18 *counteracts* the
four known systematic biases of LLM judges (§7.2): position, verbosity,
self-preference, and score clustering. The four mitigations are exposed as
``bias_mitigation_*`` settings consumed by the live judge (E-02, see
``app.quality.judge``); this module produces the **bias report** that proves the
effect.

The report is a **controlled A/B re-judge** of the calibration set: every task that
carries human feedback (E-05) is re-scored by the judge with the prompt-level
mitigations OFF and then ON, against an identical context. The two passes are run
through the very same agreement statistics as E-17 (``_compute_report``) so
"before" and "after" agreement-with-human are computed identically, then joined
with per-bias diagnostics.

Cost note: this is the ONLY part of E-18 that spends LLM calls — ``2 ×
(judge-dimensions with human feedback)`` completions. It is owner/admin-only and
on-demand (no scheduler hook); dimensions within a task are re-judged
concurrently, tasks sequentially, to bound provider rate-limit pressure. Reports
are append-only and versioned per ``(workspace, judge model)`` exactly like E-17.

- position bias does not apply to this *pointwise* report (no A/B order to swap), so
  it stays ``status: "n/a"`` here; the real position-bias mitigation lives in the
  pairwise judge (E-21, ``app.quality.comparison.judge_pair_llm``).
- self-preference cannot auto-swap models, so it is surfaced as a warning.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bias_report import BiasReport
from app.quality.judge import (
    _judge_dimension,
    _load_bias_mitigation_flags,
    _resolve_judge_model,
    _result_context,
)
from app.quality.judge_calibration import (
    DEFAULT_MIN_KAPPA,
    _compute_report,
    collect_judge_human_pairs,
)
from app.quality.model_identity import same_model_or_family
from app.quality.rubric import resolve_rubric_for_task
from app.quality.stats import MIN_SAMPLES, pearson, stdev
from app.models.task import Task
from app.utils.events import log_event

logger = logging.getLogger(__name__)

REPORT_SCHEMA_VERSION = 1
# Mitigations that actually change the judge prompt (the A/B knob).
PROMPT_TOGGLES = ("verbosity", "score_clustering")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gate_passed(scores_by_key: dict, dims_meta: dict) -> bool:
    """Recompute the judge gate over the re-judged (human-rated) judge dimensions:
    the gate fails only when a *critical* dimension misses its threshold (or fails
    to score). This is a judge-dims-only approximation of the production gate,
    applied identically to the OFF and ON passes so the before/after comparison is
    fair."""
    for key, score in scores_by_key.items():
        meta = dims_meta.get(key) or {}
        if not meta.get("critical"):
            continue
        if score is None:
            return False
        threshold = meta.get("threshold")
        if threshold is not None and score < threshold:
            return False
    return True


def _improved(before: float | None, after: float | None) -> bool:
    """True when ``after`` is defined and at least as good as ``before``."""
    return after is not None and (before is None or after >= before)


def _dimensions_delta(before: dict, after: dict) -> list[dict]:
    """Join the per-dimension agreement metrics of the two passes by key."""
    after_by_key = {d["key"]: d for d in after.get("dimensions", [])}
    out = []
    for b in before.get("dimensions", []):
        a = after_by_key.get(b["key"], {})
        out.append(
            {
                "key": b["key"],
                "name": b.get("name"),
                "cohen_kappa_before": b.get("cohen_kappa"),
                "cohen_kappa_after": a.get("cohen_kappa"),
                "pearson_before": b.get("pearson"),
                "pearson_after": a.get("pearson"),
                "mean_bias_before": b.get("mean_bias"),
                "mean_bias_after": a.get("mean_bias"),
                "improved": _improved(b.get("cohen_kappa"), a.get("cohen_kappa")),
            }
        )
    return out


def _verbosity_diagnostic(length_rows: list[tuple]) -> dict:
    """Length↔score correlation OFF vs ON vs the human baseline. A judge with
    verbosity bias scores longer answers higher; the mitigation is working when the
    judge's length-correlation moves toward the human's."""
    if len(length_rows) < MIN_SAMPLES:
        return {
            "judge_corr_off": None,
            "judge_corr_on": None,
            "human_corr": None,
            "improved": False,
            "status": "insufficient_data",
        }
    lengths = [r[0] for r in length_rows]
    off = [r[1] for r in length_rows]
    on = [r[2] for r in length_rows]
    human = [r[3] for r in length_rows]
    corr_off = pearson(lengths, off)
    corr_on = pearson(lengths, on)
    corr_human = pearson(lengths, human)
    improved = (
        corr_off is not None
        and corr_on is not None
        and corr_human is not None
        and abs(corr_on - corr_human) < abs(corr_off - corr_human)
    )
    return {
        "judge_corr_off": corr_off,
        "judge_corr_on": corr_on,
        "human_corr": corr_human,
        "improved": bool(improved),
        "status": "ok",
    }


def _clustering_diagnostic(off_scores: list[float], on_scores: list[float]) -> dict:
    """Score spread OFF vs ON. Score-clustering bias bunches scores at 7-8; the
    mitigation is working when the spread widens."""
    if len(off_scores) < MIN_SAMPLES:
        return {
            "spread_off": None,
            "spread_on": None,
            "pct_in_7_8_off": None,
            "pct_in_7_8_on": None,
            "clustered_off": None,
            "improved": False,
            "status": "insufficient_data",
        }

    def _pct_7_8(xs):
        return round(sum(1 for x in xs if 7 <= x <= 8) / len(xs), 4) if xs else None

    spread_off = stdev(off_scores)
    spread_on = stdev(on_scores)
    pct_off = _pct_7_8(off_scores)
    clustered_off = pct_off is not None and pct_off > 0.5
    improved = spread_off is not None and spread_on is not None and spread_on > spread_off
    return {
        "spread_off": spread_off,
        "spread_on": spread_on,
        "pct_in_7_8_off": pct_off,
        "pct_in_7_8_on": _pct_7_8(on_scores),
        "clustered_off": bool(clustered_off),
        "improved": bool(improved),
        "status": "ok",
    }


def _self_preference_diagnostic(judge_model: str | None, agent_models: list[str]) -> dict:
    """Flag tasks where the judge model is the same model/family as the agent model
    — its scores may be inflated. Cannot auto-swap; surfaces a warning."""
    distinct = sorted({m for m in agent_models if m})
    n_self = sum(1 for m in agent_models if same_model_or_family(judge_model, m)[0])
    flagged = n_self > 0
    warning = None
    if flagged:
        warning = (
            f"judge model {judge_model} is the same model/family as the agent model "
            f"on {n_self}/{len(agent_models)} tasks — scores may be inflated; "
            f"consider a different judge model"
        )
    return {
        "flagged": flagged,
        "judge_model": judge_model,
        "agent_models": distinct,
        "n_self_judged": n_self,
        "auto_swap": False,
        "warning": warning,
        "status": "ok",
    }


def _empty_report(threshold: float, toggles: dict, status: str) -> dict:
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "threshold_kappa": threshold,
        "n_records": 0,
        "sample_size": 0,
        "n_dimensions": 0,
        "toggles_requested": toggles,
        "before": None,
        "after": None,
        "dimensions_delta": [],
        "overall_delta": None,
        "diagnostics": {
            "verbosity": {"status": "insufficient_data"},
            "score_clustering": {"status": "insufficient_data"},
            "self_preference": {"status": "insufficient_data"},
            "position_bias": {
                "status": "n/a",
                "reason": "not applicable to pointwise judging; implemented for "
                "pairwise comparisons in E-21 (app.quality.comparison)",
            },
        },
        "task_errors": [],
    }


# --------------------------------------------------------------------------- #
# A/B re-judge orchestrator
# --------------------------------------------------------------------------- #
async def _resolve_threshold(db: AsyncSession) -> float:
    from app.api.settings import get_setting

    try:
        return float(await get_setting(db, "judge_calibration_min_kappa", DEFAULT_MIN_KAPPA))
    except (TypeError, ValueError):
        return DEFAULT_MIN_KAPPA


async def _resolve_toggles(db: AsyncSession, toggles: dict | None) -> dict:
    """Resolve the report's "after" configuration. A bias report with no
    prompt-affecting mitigation is pointless, so default to a full A/B."""
    if toggles is None:
        toggles = await _load_bias_mitigation_flags(db)
    resolved = {
        "verbosity": bool(toggles.get("verbosity")),
        "score_clustering": bool(toggles.get("score_clustering")),
        "self_preference": bool(toggles.get("self_preference")),
        "position": bool(toggles.get("position")),
    }
    if not any(resolved[k] for k in PROMPT_TOGGLES):
        resolved["verbosity"] = True
        resolved["score_clustering"] = True
    return resolved


async def run_bias_report(
    db: AsyncSession,
    *,
    workspace_id,
    suite: str | None = None,
    template_id=None,
    toggles: dict | None = None,
    created_by: str = "user",
    commit: bool = True,
) -> dict:
    """Re-judge the calibration set with mitigations OFF then ON and persist a
    versioned before/after report. Spends LLM calls (see module docstring)."""
    resolved = await _resolve_judge_model(db, workspace_id)
    judge_model = resolved.model.api_name if resolved is not None else None
    judge_config_key = judge_model or "unknown"
    threshold = await _resolve_threshold(db)
    toggles_requested = await _resolve_toggles(db, toggles)
    on_mit = {k: toggles_requested[k] for k in PROMPT_TOGGLES}

    pairs = await collect_judge_human_pairs(
        db, workspace_id, suite=suite, template_id=template_id
    )
    by_task: dict[str, list[dict]] = {}
    for p in pairs:
        by_task.setdefault(p["task_id"], []).append(p)

    if resolved is None or not by_task:
        status = "empty" if not by_task else "no_judge_model"
        report = _empty_report(threshold, toggles_requested, status)
        return await _persist(
            db,
            workspace_id=workspace_id,
            judge_config_key=judge_config_key,
            judge_model=judge_model,
            suite=suite,
            template_id=template_id,
            report=report,
            threshold=threshold,
            created_by=created_by,
            commit=commit,
        )

    off_pairs: list[dict] = []
    on_pairs: list[dict] = []
    length_rows: list[tuple] = []
    agent_models: list[str] = []
    task_errors: list[dict] = []

    for task_id, task_pairs in by_task.items():
        try:
            task = await db.get(Task, uuid.UUID(task_id))
            if task is None:
                task_errors.append({"task_id": task_id, "error": "task not found"})
                continue
            rubric = await resolve_rubric_for_task(db, task)
            if rubric is None:
                task_errors.append({"task_id": task_id, "error": "no rubric"})
                continue

            context = _result_context(task)
            result_len = len(task.result_summary or "")
            if task.model_used:
                agent_models.append(task.model_used)

            judge_dims = {
                d.get("key"): d
                for d in (rubric.dimensions or [])
                if d.get("evaluator", "judge") == "judge"
            }
            rated = [
                p
                for p in task_pairs
                if p.get("dimension_key") in judge_dims
                and p.get("human_score") is not None
            ]
            if not rated:
                continue

            # Re-judge concurrently within the task: OFF and ON for every rated dim.
            off_results = await asyncio.gather(
                *[
                    _judge_dimension(
                        judge_dims[p["dimension_key"]], context, resolved, mitigations=None
                    )
                    for p in rated
                ]
            )
            on_results = await asyncio.gather(
                *[
                    _judge_dimension(
                        judge_dims[p["dimension_key"]], context, resolved, mitigations=on_mit
                    )
                    for p in rated
                ]
            )

            task_off: list[dict] = []
            task_on: list[dict] = []
            off_by_key: dict[str, int | None] = {}
            on_by_key: dict[str, int | None] = {}
            for p, off_r, on_r in zip(rated, off_results, on_results):
                off_s = off_r.get("score") if off_r.get("status") == "scored" else None
                on_s = on_r.get("score") if on_r.get("status") == "scored" else None
                key = p["dimension_key"]
                off_by_key[key] = off_s
                on_by_key[key] = on_s
                task_off.append({**p, "judge_score": off_s})
                task_on.append({**p, "judge_score": on_s})
                if off_s is not None and on_s is not None:
                    length_rows.append((result_len, off_s, on_s, float(p["human_score"])))

            off_gate = _gate_passed(off_by_key, judge_dims)
            on_gate = _gate_passed(on_by_key, judge_dims)
            for pp in task_off:
                pp["judge_gate_passed"] = off_gate
            for pp in task_on:
                pp["judge_gate_passed"] = on_gate
            off_pairs.extend(task_off)
            on_pairs.extend(task_on)
        except Exception as e:  # noqa: BLE001 — one task must not break the run
            logger.warning(f"bias report: task {task_id} failed: {e}")
            task_errors.append({"task_id": task_id, "error": str(e)[:300]})

    before = _compute_report(off_pairs, threshold_kappa=threshold)
    after = _compute_report(on_pairs, threshold_kappa=threshold)
    overall_delta = {
        "cohen_kappa_before": before["overall"]["cohen_kappa"],
        "cohen_kappa_after": after["overall"]["cohen_kappa"],
        "agreement_pct_before": before["overall"]["agreement_pct"],
        "agreement_pct_after": after["overall"]["agreement_pct"],
        "improved": _improved(
            before["overall"]["cohen_kappa"], after["overall"]["cohen_kappa"]
        ),
    }

    off_scores = [r[1] for r in length_rows]
    on_scores = [r[2] for r in length_rows]
    status = "ok" if len(off_pairs) >= MIN_SAMPLES else "insufficient_data"
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "threshold_kappa": threshold,
        "n_records": len(by_task),
        "sample_size": len(off_pairs),
        "n_dimensions": before["n_dimensions"],
        "toggles_requested": toggles_requested,
        "before": before,
        "after": after,
        "dimensions_delta": _dimensions_delta(before, after),
        "overall_delta": overall_delta,
        "diagnostics": {
            "verbosity": _verbosity_diagnostic(length_rows),
            "score_clustering": _clustering_diagnostic(off_scores, on_scores),
            "self_preference": _self_preference_diagnostic(judge_model, agent_models),
            "position_bias": {
                "status": "n/a",
                "reason": "not applicable to pointwise judging; implemented for "
                "pairwise comparisons in E-21 (app.quality.comparison)",
            },
        },
        "task_errors": task_errors,
    }

    return await _persist(
        db,
        workspace_id=workspace_id,
        judge_config_key=judge_config_key,
        judge_model=judge_model,
        suite=suite,
        template_id=template_id,
        report=report,
        threshold=threshold,
        created_by=created_by,
        commit=commit,
    )


# --------------------------------------------------------------------------- #
# Persistence / public API (mirrors E-17)
# --------------------------------------------------------------------------- #
def _serialize(row: BiasReport) -> dict:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "judge_config_key": row.judge_config_key,
        "judge_model": row.judge_model,
        "version": row.version,
        "sample_size": row.sample_size,
        "n_dimensions": row.n_dimensions,
        "threshold_kappa": row.threshold_kappa,
        "passed": row.passed,
        "filters": row.filters or {},
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "metrics": row.metrics or {},
    }


async def _persist(
    db: AsyncSession,
    *,
    workspace_id,
    judge_config_key: str,
    judge_model: str | None,
    suite: str | None,
    template_id,
    report: dict,
    threshold: float,
    created_by: str,
    commit: bool,
) -> dict:
    maxv = (
        await db.execute(
            select(func.max(BiasReport.version)).where(
                BiasReport.workspace_id == workspace_id,
                BiasReport.judge_config_key == judge_config_key,
            )
        )
    ).scalar()
    version = (maxv or 0) + 1

    overall_delta = report.get("overall_delta") or {}
    passed = bool(overall_delta.get("improved")) and (
        overall_delta.get("cohen_kappa_after") is not None
    )

    row = BiasReport(
        workspace_id=workspace_id,
        judge_config_key=judge_config_key,
        judge_model=judge_model,
        version=version,
        sample_size=report.get("sample_size", 0),
        n_dimensions=report.get("n_dimensions", 0),
        filters={
            "suite": suite,
            "template_id": str(template_id) if template_id else None,
        },
        metrics=report,
        threshold_kappa=threshold,
        passed=passed,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    await log_event(
        db,
        "bias_report_run",
        "system",
        {
            "judge_config_key": judge_config_key,
            "version": version,
            "sample_size": report.get("sample_size", 0),
            "status": report.get("status"),
            "improved": passed,
        },
        workspace_id=workspace_id,
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(row)
    return _serialize(row)


async def get_bias_report(
    db: AsyncSession, *, workspace_id, judge_config_key: str | None = None
) -> dict | None:
    """The latest bias report for a judge_config_key (or the most recent across all
    keys), or ``None`` when the workspace has never run one."""
    q = select(BiasReport).where(BiasReport.workspace_id == workspace_id)
    if judge_config_key:
        q = q.where(BiasReport.judge_config_key == judge_config_key)
    q = q.order_by(BiasReport.created_at.desc(), BiasReport.version.desc()).limit(1)
    row = (await db.execute(q)).scalar_one_or_none()
    return _serialize(row) if row is not None else None


async def list_bias_reports(
    db: AsyncSession, *, workspace_id, judge_config_key: str | None = None, limit: int = 50
) -> list[dict]:
    """Version history, newest first."""
    q = select(BiasReport).where(BiasReport.workspace_id == workspace_id)
    if judge_config_key:
        q = q.where(BiasReport.judge_config_key == judge_config_key)
    q = q.order_by(BiasReport.created_at.desc(), BiasReport.version.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_serialize(r) for r in rows]
