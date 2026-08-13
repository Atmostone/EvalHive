"""Annotation collection (E-05, outcome type O4).

Captures a structured rating of a finished task — a 0-10 score per quality
dimension (mirroring the E-02 axes and the E-07 trajectory axes), a free-text
comment per dimension, an overall comment and an optional approve/reject verdict.

Every rating is an **append-only** row in ``annotations`` (SPA-85). Two
annotators rating the same run produce two independent rows, which is what makes
inter-annotator agreement computable; the same annotator rating it again
produces a new row pointing at the one it replaces, so a re-rating never
destroys the previous verdict and never inflates the population. Each row
records *who* rated (``annotator_type`` — a person, or a model deciding
unattended), the protocol it was collected under, and a frozen snapshot of what
the judge had said at that moment, so a later re-judge cannot rewrite a past
calibration.

``quality_records.human_feedback`` is kept as a materialised «latest human
rating» so every existing reader keeps working, but it is a projection of the
ledger rather than the source of truth.

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

from app.models.annotation import Annotation
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.judge import _MAX_SCALE
from app.utils.events import log_event

logger = logging.getLogger(__name__)

FEEDBACK_SCHEMA_VERSION = 1

# Who produced the rating. The distinction that matters is whether a person
# decided or a model decided unattended — what tools a person used while
# annotating is deliberately NOT recorded: someone working with an assistant is
# simply `human`. `legacy` is every row collected before the ledger existed.
ANNOTATOR_TYPES = ("human", "llm_judge", "synthetic", "legacy")
# Types that project into the `human_feedback` slot (the materialised «latest
# human rating»). A machine annotation is recorded but must not displace it.
HUMAN_TYPES = ("human", "legacy")

# Version of the collection protocol. Bump when the questions asked of the
# annotator change in a way that makes ratings non-comparable.
PROTOCOL_VERSION = 1

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


# --------------------------------------------------------------------------- #
# Frozen judge observation
# --------------------------------------------------------------------------- #
def freeze_judge_observation(record) -> dict:
    """Snapshot what the judge had said about this run at annotation time.

    Both judges already stamp their own identity into their profile (E-02
    ``judge.py``, E-07 ``trajectory.py``); this copies that identity plus the
    per-key scores and the gate verdict into the annotation. Without it a
    calibration pair is rebuilt from the *current* profile, so re-running a
    judge silently moves a past κ.
    """
    out: dict = {}
    profile = getattr(record, "quality_profile", None) or {}
    if profile:
        out["outcome"] = {
            "judge_model": profile.get("judge_model"),
            "rubric_id": profile.get("rubric_id"),
            "rubric_name": profile.get("rubric_name"),
            "schema_version": profile.get("schema_version"),
            "evaluated_at": profile.get("evaluated_at"),
            "gate_passed": (profile.get("gate") or {}).get("passed"),
            "scores": {
                d.get("key"): d.get("score")
                for d in profile.get("dimensions") or []
                if d.get("key")
            },
            "reasoning": {
                d.get("key"): d.get("reasoning")
                for d in profile.get("dimensions") or []
                if d.get("key") and d.get("reasoning")
            },
        }
    trajectory = getattr(record, "trajectory_profile", None) or {}
    if trajectory:
        out["trajectory"] = {
            "judge_model": trajectory.get("judge_model"),
            "schema_version": trajectory.get("schema_version"),
            "evaluated_at": trajectory.get("evaluated_at"),
            "scores": {
                a.get("key"): a.get("score")
                for a in trajectory.get("axes") or []
                if a.get("key")
            },
            "reasoning": {
                a.get("key"): a.get("reason")
                for a in trajectory.get("axes") or []
                if a.get("key") and a.get("reason")
            },
        }
    return out


def observed_scores(observation: dict | None) -> dict:
    """Flatten a frozen observation to ``{dimension_key: judge score}``.

    Outcome axes win over trajectory axes on a key collision, matching the order
    the calibration collector resolves them in.
    """
    out: dict = {}
    for side in ("outcome", "trajectory"):
        for key, score in ((observation or {}).get(side, {}).get("scores") or {}).items():
            out.setdefault(key, score)
    return out


def observed_reasoning(observation: dict | None) -> dict:
    """Flatten a frozen observation to ``{dimension_key: judge reasoning}``."""
    out: dict = {}
    for side in ("outcome", "trajectory"):
        for key, text in ((observation or {}).get(side, {}).get("reasoning") or {}).items():
            out.setdefault(key, text)
    return out


# --------------------------------------------------------------------------- #
# Payload normalization
# --------------------------------------------------------------------------- #
def build_human_feedback(
    payload: dict, observation: dict | None, submitted_by: str
) -> dict:
    """Normalize a rating payload into the stored ``human_feedback`` shape.

    ``observation`` is the frozen judge observation (see
    :func:`freeze_judge_observation`); each rated dimension is paired with the
    judge's score on the same key for calibration convenience. It covers the
    trajectory axes as well as the outcome ones — the form solicits both, and
    pairing only the outcome axes was what left the trajectory side of a
    calibration pair to be re-read from a mutable profile.
    """
    judge_by_key = observed_scores(observation)

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


def serialize_annotation(row: Annotation) -> dict:
    """One ledger row, as returned by the annotations endpoint."""
    return {
        "id": str(row.id),
        "task_id": str(row.task_id),
        "annotator_type": row.annotator_type,
        "annotator_id": str(row.annotator_id) if row.annotator_id else None,
        "annotator_label": row.annotator_label,
        "protocol_version": row.protocol_version,
        "blind_to_model": row.blind_to_model,
        "blind_to_judge": row.blind_to_judge,
        "verdict": row.verdict,
        "overall_comment": row.overall_comment,
        "dimensions": row.dimensions or [],
        "judge_observation": row.judge_observation or {},
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# --------------------------------------------------------------------------- #
# Read / write
# --------------------------------------------------------------------------- #
async def _record_for(db: AsyncSession, task: Task) -> QualityRecord | None:
    return (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()


async def get_human_feedback(db: AsyncSession, task: Task) -> dict | None:
    """The materialised latest human rating for a task, or ``None``."""
    record = await _record_for(db, task)
    return record.human_feedback if record is not None else None


async def list_annotations(db: AsyncSession, task: Task) -> list[dict]:
    """The full append-only ledger for a task, oldest first."""
    rows = (
        await db.execute(
            select(Annotation)
            .where(Annotation.task_id == task.id)
            .order_by(Annotation.created_at, Annotation.id)
        )
    ).scalars().all()
    return [serialize_annotation(r) for r in rows]


async def save_annotation(
    db: AsyncSession,
    task: Task,
    payload: dict,
    *,
    annotator_type: str = "human",
    annotator_id=None,
    annotator_label: str,
    blind_to_model: bool = False,
    blind_to_judge: bool = False,
    commit: bool = True,
) -> dict:
    """Append one rating of ``task`` to the annotation ledger.

    Ensures the quality record exists (building it on demand, mirroring the
    judge), freezes the judge's side, appends the row — superseding this
    annotator's own previous rating rather than the record's — and refreshes the
    materialised ``human_feedback`` slot when the rating came from a human.
    Returns the stored feedback dict.
    """
    if annotator_type not in ANNOTATOR_TYPES:
        raise ValueError(f"unknown annotator_type: {annotator_type}")

    record = await _record_for(db, task)
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)
    if record is None:
        raise ValueError(f"cannot build a quality record for task {task.id}")

    # Serialize concurrent saves on the same record: two annotators (or two tabs)
    # would otherwise both read the same predecessor and fork the chain.
    await db.execute(
        select(QualityRecord.id).where(QualityRecord.id == record.id).with_for_update()
    )

    observation = freeze_judge_observation(record)
    feedback = build_human_feedback(payload, observation, annotator_label)

    # Supersede this annotator's own latest row — theirs alone. A different
    # annotator's rating stays current, which is exactly what makes the two
    # comparable. Identity is the user id when there is one, else the label.
    previous = select(Annotation.id).where(Annotation.quality_record_id == record.id)
    if annotator_id is not None:
        previous = previous.where(Annotation.annotator_id == annotator_id)
    else:
        previous = previous.where(
            Annotation.annotator_id.is_(None),
            Annotation.annotator_type == annotator_type,
            Annotation.annotator_label == annotator_label,
        )
    supersedes_id = await db.scalar(
        previous.order_by(Annotation.created_at.desc(), Annotation.id.desc()).limit(1)
    )

    db.add(
        Annotation(
            quality_record_id=record.id,
            task_id=task.id,
            workspace_id=task.workspace_id,
            annotator_type=annotator_type,
            annotator_id=annotator_id,
            annotator_label=annotator_label,
            protocol_version=PROTOCOL_VERSION,
            blind_to_model=blind_to_model,
            blind_to_judge=blind_to_judge,
            verdict=feedback["verdict"],
            overall_comment=feedback["overall_comment"],
            dimensions=feedback["dimensions"],
            judge_observation=observation,
            supersedes_id=supersedes_id,
        )
    )
    if annotator_type in HUMAN_TYPES:
        record.human_feedback = feedback

    await log_event(
        db,
        "human_feedback_submitted",
        "user",
        {
            "verdict": feedback["verdict"],
            "dimensions": len(feedback["dimensions"]),
            "submitted_by": annotator_label,
            "annotator_type": annotator_type,
            "protocol_version": PROTOCOL_VERSION,
            "blind_to_judge": blind_to_judge,
            "supersedes": str(supersedes_id) if supersedes_id else None,
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return feedback
