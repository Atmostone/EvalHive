"""Judge Calibration Protocol (E-17).

An LLM-judge metric is meaningless until it is validated against humans — the
central source of doubt about eval (RQ1). E-17 answers "how far can the judge be
trusted" by comparing the judge's per-dimension scores (E-02,
``quality_profile.dimensions[]``) with human ratings on the same axes (E-05,
``human_feedback.dimensions[]``) over every record that carries both.

It makes **no LLM call** — it is pure statistics over already-stored scores:

- per dimension: Pearson + Spearman on the (judge, human) scores, Cohen's kappa on
  the categorical band projection (bad/improve/good), and the mean signed bias;
- overall: agreement between the judge gate (``quality_profile.gate.passed``) and
  the human verdict (approve/reject), as Cohen's kappa plus a raw agreement rate.

A dimension is ``reliable`` when its band kappa clears ``judge_calibration_min_kappa``
(default 0.6). The report is persisted append-only and versioned per
``(workspace, judge_config_key)`` (the judge model's api_name) in
``judge_calibrations`` — re-running after a judge/rubric change keeps the old
curves. ``suite``/``template_id`` filters scope the population (the loose mapping
of the acceptance's ``dataset_id``); they are recorded but do not fork the
version line.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.judge_calibration import JudgeCalibration
from app.models.quality_record import QualityRecord
from app.quality.judge import _resolve_judge_model
from app.quality.stats import (
    BANDS,
    MIN_SAMPLES,
    cohen_kappa,
    mean_bias,
    pearson,
    score_to_band,
    spearman,
)
from app.utils.events import log_event

logger = logging.getLogger(__name__)

VERDICT_LABELS = ["approve", "reject"]
DEFAULT_MIN_KAPPA = 0.6


# --------------------------------------------------------------------------- #
# Raw judge-vs-human pairs (shared with the GET /calibration export)
# --------------------------------------------------------------------------- #
async def collect_judge_human_pairs(
    db: AsyncSession,
    workspace_id,
    *,
    suite: str | None = None,
    template_id=None,
) -> list[dict]:
    """One row per rated dimension across records that carry human feedback.

    The source of truth for both the ``GET /api/quality/calibration`` export and
    the E-17 report. ``suite``/``template_id`` narrow the population. Each row
    pairs the human score with the judge's score for the same dimension key and
    carries the per-task ``verdict`` and ``judge_gate_passed`` so the report can
    build the overall verdict-agreement."""
    q = select(QualityRecord).where(
        QualityRecord.workspace_id == workspace_id,
        QualityRecord.human_feedback.isnot(None),
    )
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    if template_id is not None:
        q = q.where(QualityRecord.template_id == template_id)
    rows = (await db.execute(q)).scalars().all()

    out: list[dict] = []
    for r in rows:
        hf = r.human_feedback or {}
        profile = r.quality_profile or {}
        judge = {d.get("key"): d for d in (profile.get("dimensions") or [])}
        gate_passed = (profile.get("gate") or {}).get("passed")
        for d in hf.get("dimensions") or []:
            jd = judge.get(d.get("key")) or {}
            judge_score = d.get("judge_score")
            if judge_score is None:
                judge_score = jd.get("score")
            out.append(
                {
                    "task_id": str(r.task_id),
                    "dimension_key": d.get("key"),
                    "dimension_name": d.get("name"),
                    "judge_score": judge_score,
                    "human_score": d.get("score"),
                    "band": d.get("band"),
                    "judge_reasoning": jd.get("reasoning"),
                    "human_comment": d.get("comment"),
                    "verdict": hf.get("verdict"),
                    "judge_gate_passed": gate_passed,
                    "submitted_by": hf.get("submitted_by"),
                    "submitted_at": hf.get("submitted_at"),
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Report computation (pure)
# --------------------------------------------------------------------------- #
def _recommendation(dim: dict) -> str | None:
    """One-line per-dimension verdict, e.g. 'judge reliable for Efficiency
    (kappa=0.71, r=0.81)' / 'judge diverges on Tool Selection (kappa=0.31,
    r=0.42)'. ``None`` for dimensions without enough data."""
    if dim["status"] != "ok":
        return None
    k = dim["cohen_kappa"]
    r = dim["pearson"]
    k_s = "n/a" if k is None else f"{k:.2f}"
    r_s = "n/a" if r is None else f"{r:.2f}"
    if dim["reliable"]:
        return f"judge reliable for {dim['name']} (kappa={k_s}, r={r_s})"
    return f"judge diverges on {dim['name']} (kappa={k_s}, r={r_s})"


def _compute_report(pairs: list[dict], *, threshold_kappa: float) -> dict:
    """Group pairs by dimension and compute per-dimension reliability plus the
    overall verdict-agreement. Pure function over the rows from
    :func:`collect_judge_human_pairs`."""
    by_key: dict[str, list[dict]] = {}
    for p in pairs:
        key = p.get("dimension_key")
        if key:
            by_key.setdefault(key, []).append(p)

    dimensions: list[dict] = []
    for key in sorted(by_key):
        rows = by_key[key]
        name = next((r.get("dimension_name") for r in rows if r.get("dimension_name")), key)
        judge_scores: list[float] = []
        human_scores: list[float] = []
        judge_bands: list[str] = []
        human_bands: list[str] = []
        for r in rows:
            js, hs = r.get("judge_score"), r.get("human_score")
            if js is None or hs is None:
                continue
            jb = score_to_band(js)
            hb = r.get("band") or score_to_band(hs)
            if jb is None or hb is None:
                continue
            judge_scores.append(float(js))
            human_scores.append(float(hs))
            judge_bands.append(jb)
            human_bands.append(hb)

        n = len(judge_scores)
        kappa = cohen_kappa(judge_bands, human_bands, BANDS) if n >= MIN_SAMPLES else None
        dim = {
            "key": key,
            "name": name,
            "n": n,
            "pearson": pearson(judge_scores, human_scores),
            "spearman": spearman(judge_scores, human_scores),
            "cohen_kappa": kappa,
            "mean_bias": mean_bias(judge_scores, human_scores),
            "reliable": kappa is not None and kappa >= threshold_kappa,
            "status": "ok" if n >= MIN_SAMPLES else "insufficient_data",
        }
        dimensions.append(dim)

    # Overall verdict-agreement: one (judge_gate, human_verdict) pair per task.
    verdict_pairs: dict[str, tuple[str, str]] = {}
    for p in pairs:
        tid = p.get("task_id")
        verdict = p.get("verdict")
        gate = p.get("judge_gate_passed")
        if tid is None or verdict not in VERDICT_LABELS or gate is None:
            continue
        judge_verdict = "approve" if gate else "reject"
        verdict_pairs[tid] = (judge_verdict, verdict)
    j_v = [v[0] for v in verdict_pairs.values()]
    h_v = [v[1] for v in verdict_pairs.values()]
    n_v = len(j_v)
    overall_kappa = cohen_kappa(j_v, h_v, VERDICT_LABELS) if n_v >= MIN_SAMPLES else None
    agreement_pct = (
        round(sum(1 for a, b in zip(j_v, h_v) if a == b) / n_v, 4) if n_v else None
    )
    overall = {
        "n": n_v,
        "cohen_kappa": overall_kappa,
        "agreement_pct": agreement_pct,
        "reliable": overall_kappa is not None and overall_kappa >= threshold_kappa,
    }

    recommendations = [rec for d in dimensions if (rec := _recommendation(d))]
    n_humans = len({p.get("submitted_by") for p in pairs if p.get("submitted_by")})
    n_records = len({p.get("task_id") for p in pairs if p.get("task_id")})

    return {
        "threshold_kappa": threshold_kappa,
        "sample_size": len(pairs),
        "n_records": n_records,
        "n_humans": n_humans,
        "n_dimensions": len(dimensions),
        "dimensions": dimensions,
        "overall": overall,
        "recommendations": recommendations,
    }


# --------------------------------------------------------------------------- #
# Persistence / public API
# --------------------------------------------------------------------------- #
def _serialize(row: JudgeCalibration) -> dict:
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


async def run_judge_calibration(
    db: AsyncSession,
    *,
    workspace_id,
    suite: str | None = None,
    template_id=None,
    created_by: str = "user",
    commit: bool = True,
) -> dict:
    """Compute a fresh judge-calibration report from stored judge/human scores and
    persist it as the next version for this workspace's judge model. Returns the
    serialized report row."""
    from app.api.settings import get_setting

    resolved = await _resolve_judge_model(db, workspace_id)
    judge_model = resolved.model.api_name if resolved is not None else None
    judge_config_key = judge_model or "unknown"

    try:
        threshold = float(await get_setting(db, "judge_calibration_min_kappa", DEFAULT_MIN_KAPPA))
    except (TypeError, ValueError):
        threshold = DEFAULT_MIN_KAPPA

    pairs = await collect_judge_human_pairs(
        db, workspace_id, suite=suite, template_id=template_id
    )
    report = _compute_report(pairs, threshold_kappa=threshold)

    maxv = (
        await db.execute(
            select(func.max(JudgeCalibration.version)).where(
                JudgeCalibration.workspace_id == workspace_id,
                JudgeCalibration.judge_config_key == judge_config_key,
            )
        )
    ).scalar()
    version = (maxv or 0) + 1

    row = JudgeCalibration(
        workspace_id=workspace_id,
        judge_config_key=judge_config_key,
        judge_model=judge_model,
        version=version,
        sample_size=report["sample_size"],
        n_dimensions=report["n_dimensions"],
        filters={
            "suite": suite,
            "template_id": str(template_id) if template_id else None,
        },
        metrics=report,
        threshold_kappa=threshold,
        passed=bool(report["overall"]["reliable"]),
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    await log_event(
        db,
        "judge_calibration_run",
        "system",
        {
            "judge_config_key": judge_config_key,
            "version": version,
            "sample_size": report["sample_size"],
            "n_humans": report["n_humans"],
            "overall_kappa": report["overall"]["cohen_kappa"],
            "passed": row.passed,
        },
        workspace_id=workspace_id,
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(row)
    return _serialize(row)


async def get_judge_calibration(
    db: AsyncSession, *, workspace_id, judge_config_key: str | None = None
) -> dict | None:
    """The latest report for a judge_config_key (or the most recent across all
    keys when none given), or ``None`` when the workspace has never calibrated."""
    q = select(JudgeCalibration).where(JudgeCalibration.workspace_id == workspace_id)
    if judge_config_key:
        q = q.where(JudgeCalibration.judge_config_key == judge_config_key)
    q = q.order_by(JudgeCalibration.created_at.desc(), JudgeCalibration.version.desc()).limit(1)
    row = (await db.execute(q)).scalar_one_or_none()
    return _serialize(row) if row is not None else None


async def list_judge_calibrations(
    db: AsyncSession, *, workspace_id, judge_config_key: str | None = None, limit: int = 50
) -> list[dict]:
    """Version history, newest first."""
    q = select(JudgeCalibration).where(JudgeCalibration.workspace_id == workspace_id)
    if judge_config_key:
        q = q.where(JudgeCalibration.judge_config_key == judge_config_key)
    q = q.order_by(JudgeCalibration.created_at.desc(), JudgeCalibration.version.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_serialize(r) for r in rows]


async def get_judge_calibration_badge(db: AsyncSession, *, workspace_id) -> dict:
    """Compact badge data: 'judge calibrated against N humans, kappa=X.X'."""
    latest = await get_judge_calibration(db, workspace_id=workspace_id)
    if latest is None:
        return {"calibrated": False}
    metrics = latest.get("metrics") or {}
    overall = metrics.get("overall") or {}
    return {
        "calibrated": True,
        "n_humans": metrics.get("n_humans", 0),
        "sample_size": latest.get("sample_size", 0),
        "overall_kappa": overall.get("cohen_kappa"),
        "judge_config_key": latest.get("judge_config_key"),
        "version": latest.get("version"),
        "passed": latest.get("passed"),
        "created_at": latest.get("created_at"),
    }
