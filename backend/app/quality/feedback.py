"""Human feedback collection (E-05, outcome type O4).

Captures a structured human signal on a finished task — a 0-10 rating per quality
dimension (mirroring the E-02 axes), a free-text comment per dimension, an overall
comment and an optional approve/reject verdict — and stores it in the
``quality_records.human_feedback`` JSONB slot (the E-01 placeholder).

This is a *parallel* signal: it does NOT alter the automated judge gate or
weighted score. Pairing each human score with the judge score on the same
dimension is the raw material for judge calibration (E-17), exposed via the
calibration export. Scores are interpreted in bands — 1-3 incorrect / 4-7 needs
work / 8-10 correct — which feed the refinement loop (E-26); the band thresholds
are constants here and become rubric-configurable in E-26.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.judge import _MAX_SCALE
from app.utils.events import log_event

logger = logging.getLogger(__name__)

FEEDBACK_SCHEMA_VERSION = 1

# Band thresholds (inclusive upper bounds). Rubric-configurable in E-26.
BAND_BAD_MAX = 3       # 1-3  → incorrect, must fix
BAND_IMPROVE_MAX = 7   # 4-7  → acceptable but improvable
# 8-10 → correct, leave as is

_COMMENT_CAP = 2000
_OVERALL_CAP = 4000


def _band(score: int) -> str:
    """Map a 0-10 score to its quality band."""
    if score <= BAND_BAD_MAX:
        return "bad"
    if score <= BAND_IMPROVE_MAX:
        return "improve"
    return "good"


def _clean(text, cap: int):
    s = (text or "").strip()[:cap]
    return s or None


def build_human_feedback(payload: dict, profile: dict | None, submitted_by: str) -> dict:
    """Normalize a feedback payload into the stored ``human_feedback`` shape.

    ``profile`` is the task's quality profile (if any); each human dimension is
    paired with the judge's score on the same key for calibration convenience.
    """
    judge_by_key: dict = {}
    if profile:
        for d in profile.get("dimensions") or []:
            judge_by_key[d.get("key")] = d.get("score")

    dims: list[dict] = []
    for d in payload.get("dimensions") or []:
        score = max(0, min(_MAX_SCALE, int(d["score"])))
        key = d.get("key")
        dims.append(
            {
                "key": key,
                "name": d.get("name") or key,
                "score": score,
                "band": _band(score),
                "comment": _clean(d.get("comment"), _COMMENT_CAP),
                "judge_score": judge_by_key.get(key),
            }
        )

    verdict = payload.get("verdict")
    if verdict not in ("approve", "reject"):
        verdict = None

    return {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "verdict": verdict,
        "overall_comment": _clean(payload.get("overall_comment"), _OVERALL_CAP),
        "dimensions": dims,
        "submitted_by": submitted_by,
        "submitted_at": datetime.utcnow().isoformat(),
    }


async def get_human_feedback(db: AsyncSession, task: Task) -> dict | None:
    """Return the stored human feedback for a task, or ``None``."""
    record = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == task.id)
        )
    ).scalar_one_or_none()
    return record.human_feedback if record is not None else None


async def save_human_feedback(
    db: AsyncSession, task: Task, payload: dict, submitted_by: str, *, commit: bool = True
) -> dict:
    """Upsert the human feedback for a task into its quality record.

    Ensures the quality record exists (building it on demand, mirroring the
    judge), then writes the ``human_feedback`` slot. Returns the stored dict.
    """
    record = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == task.id)
        )
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)

    feedback = build_human_feedback(payload, getattr(record, "quality_profile", None), submitted_by)
    if record is not None:
        record.human_feedback = feedback

    await log_event(
        db,
        "human_feedback_submitted",
        "user",
        {
            "verdict": feedback["verdict"],
            "dimensions": len(feedback["dimensions"]),
            "submitted_by": submitted_by,
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return feedback
