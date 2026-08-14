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

from app.models.annotation import Annotation, AnnotationSession
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
            # An id and a name cannot prove the conditions — a rubric is editable
            # in place and the prompt changes with the E-18 mitigations. These are
            # null for profiles written before PROFILE_SCHEMA_VERSION 3, which is
            # the honest answer for them.
            "rubric_fingerprint": profile.get("rubric_fingerprint"),
            "prompt_fingerprint": profile.get("prompt_fingerprint"),
            "bias_mitigation": profile.get("bias_mitigation"),
            "files_only": profile.get("files_only"),
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
            "prompt_fingerprint": trajectory.get("prompt_fingerprint"),
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


# --------------------------------------------------------------------------- #
# Blind protocol — a property of the session, enforced server-side (SPA-85)
# --------------------------------------------------------------------------- #
# A blindness flag the client asserts is worth nothing, and tracking every judge
# score a user has ever been shown cannot be made complete — scores also reach a
# client from the on-demand evaluate endpoints, the whole experiment-results
# payload and the analytical surfaces. A partial version of that guarantee is
# worse than none, because it reads as a guarantee.
#
# So the claim is narrowed to one the server can prove. An annotation session is
# opened before anything is fetched, declares its protocol, and is served ONE
# bundle sanitized to match; the rating is submitted against that session and the
# stored flags come from the session row. `blind_to_judge` means «this rating was
# produced through a session that was served no judge scores» — a fact about what
# was served, not a claim about the annotator's whole browsing history.
#
# The sanitizers below build that bundle. Dimension keys and names survive: the
# annotator has to know WHICH axes to rate, only not what the judge said.
def blind_quality_profile(profile: dict | None) -> dict | None:
    """The outcome profile with every judge score, reasoning and gate removed."""
    if not profile:
        return profile
    out = {
        k: v
        for k, v in profile.items()
        if k not in ("dimensions", "gate", "weighted_score", "bias_mitigation")
    }
    out["weighted_score"] = None
    out["gate"] = None
    out["dimensions"] = [
        {"key": d.get("key"), "name": d.get("name"), "status": d.get("status")}
        for d in profile.get("dimensions") or []
    ]
    out["blinded"] = True
    return out


def blind_trajectory_profile(profile: dict | None) -> dict | None:
    """The process profile with every axis score, reason and summary removed."""
    if not profile:
        return profile
    out = {
        k: v
        for k, v in profile.items()
        if k not in ("axes", "overall_score", "summary", "loop_detected", "loop_analysis")
    }
    out["overall_score"] = None
    out["summary"] = ""
    out["axes"] = [
        {"key": a.get("key"), "name": a.get("name"), "status": a.get("status")}
        for a in profile.get("axes") or []
    ]
    out["blinded"] = True
    return out


def blind_human_feedback(feedback: dict | None) -> dict | None:
    """A stored rating with the paired judge scores removed — re-opening your own
    previous annotation must not be a way around the blind."""
    if not feedback:
        return feedback
    out = dict(feedback)
    out["dimensions"] = [
        {k: v for k, v in d.items() if k != "judge_score"}
        for d in feedback.get("dimensions") or []
    ]
    out["blinded"] = True
    return out


def blind_annotation(row: dict) -> dict:
    """A ledger row with the frozen judge observation and the paired judge scores
    removed."""
    out = blind_human_feedback(row) or {}
    out["judge_observation"] = {}
    return out


# --------------------------------------------------------------------------- #
# Annotation sessions
# --------------------------------------------------------------------------- #
async def open_annotation_session(
    db: AsyncSession,
    task: Task,
    *,
    user_id,
    blind: bool,
    commit: bool = True,
) -> AnnotationSession:
    """Start a session and record the protocol it will be served under."""
    session = AnnotationSession(
        task_id=task.id,
        workspace_id=task.workspace_id,
        user_id=user_id,
        protocol_version=PROTOCOL_VERSION,
        blind_to_judge=blind,
        blind_to_model=blind,
        # Not a choice: every session redacts the other annotators' ratings, so
        # a rating produced through one was made without sight of them.
        blind_to_peers=True,
    )
    db.add(session)
    await db.flush()
    if commit:
        await db.commit()
        await db.refresh(session)
    return session


async def resolve_annotation_session(
    db: AsyncSession, session_id, *, task_id, workspace_id, user_id
) -> tuple[AnnotationSession | None, Annotation | None]:
    """Look up the session a rating is being submitted against.

    Returns ``(session, existing_rating)``. ``session`` is ``None`` when the id
    is unknown or does not belong to this caller, this run and this workspace —
    the identity checks happen **before** any rating is fetched, because looking
    the rating up first and returning it would answer a replay of someone else's
    session id with their feedback, across workspaces.

    The row is locked: «single-use» has to be decided by the database, not by a
    read-then-write, or two concurrent submissions both see an unconsumed session
    and one bundle vouches for two ratings.
    """
    if not session_id:
        return None, None
    session = (
        await db.execute(
            select(AnnotationSession)
            .where(AnnotationSession.id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        return None, None
    if (
        session.user_id != user_id
        or session.task_id != task_id
        or session.workspace_id != workspace_id
    ):
        return None, None
    existing = (
        await db.execute(
            select(Annotation).where(Annotation.session_id == session.id)
        )
    ).scalar_one_or_none()
    return session, existing


def peer_redacted_annotation(row: dict) -> dict:
    """Another annotator's ledger row with their opinion removed.

    Who rated, when, and under which protocol stay: that is provenance, and the
    «two people rated this» count has to remain visible. Their scores, verdict
    and comments do not — an annotator who can read them is not producing an
    independent rating, and inter-annotator agreement over anchored ratings
    measures nothing."""
    out = dict(row)
    out["dimensions"] = [
        {"key": d.get("key"), "name": d.get("name")} for d in row.get("dimensions") or []
    ]
    out["verdict"] = None
    out["overall_comment"] = None
    out["judge_observation"] = {}
    out["redacted"] = True
    return out


def annotation_bundle(
    *,
    review: dict,
    quality_profile: dict | None,
    trajectory_profile: dict | None,
    own_feedback: dict | None,
    annotations: list[dict],
    annotator_id,
    model_used: str | None,
    blind: bool,
) -> dict:
    """Everything an annotator needs to rate one run, in one payload.

    One endpoint rather than five means the protocol cannot be half-applied — a
    blind session physically cannot receive a judge score, because the same
    branch builds every part of what it is given.

    ``own_feedback`` is **this** annotator's own latest rating, never the
    materialised `human_feedback` slot: that slot holds whoever rated last, so
    serving it would hand the second annotator the first one's scores to edit,
    and the κ computed over the result would be measuring agreement with a
    pre-filled form.

    Other annotators' opinions are redacted **always**, not «until you have
    rated». Revealing them afterwards left re-annotation open: an annotator who
    had seen their peers could rate again, and since the collector takes each
    annotator's *current* row, the independent first rating was dropped as
    superseded and the dependent one silently took its place in the κ. A session
    is for producing an independent rating; reading what everyone else thought is
    a different activity, served by `GET …/annotations` and the calibration
    export.
    """
    rows = [
        r
        if r.get("annotator_id") and str(r["annotator_id"]) == str(annotator_id)
        else peer_redacted_annotation(r)
        for r in annotations
    ]
    if not blind:
        return {
            "review": review,
            "quality_profile": quality_profile,
            "trajectory_profile": trajectory_profile,
            "human_feedback": own_feedback,
            "annotations": rows,
            "model_used": model_used,
        }
    return {
        "review": review,
        "quality_profile": blind_quality_profile(quality_profile),
        "trajectory_profile": blind_trajectory_profile(trajectory_profile),
        "human_feedback": blind_human_feedback(own_feedback),
        "annotations": [blind_annotation(r) for r in rows],
        "model_used": None,
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
        "blind_to_peers": row.blind_to_peers,
        "verdict": row.verdict,
        "overall_comment": row.overall_comment,
        "dimensions": row.dimensions or [],
        "judge_observation": row.judge_observation or {},
        "supersedes_id": str(row.supersedes_id) if row.supersedes_id else None,
        # The evidence behind the blindness flags: which served bundle this
        # rating came from.
        "session_id": str(row.session_id) if row.session_id else None,
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


def feedback_from_annotation(row: Annotation) -> dict:
    """A stored rating back in the ``human_feedback`` shape — what this annotator
    submitted, for their own form to reopen and for an idempotent retry."""
    return {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "verdict": row.verdict,
        "overall_comment": row.overall_comment,
        "dimensions": row.dimensions or [],
        "submitted_by": row.annotator_label,
        "submitted_at": row.created_at.isoformat() if row.created_at else None,
    }


async def own_annotation(db: AsyncSession, task: Task, annotator_id) -> Annotation | None:
    """This annotator's current (not superseded) rating of this run, if any."""
    if annotator_id is None:
        return None
    superseded = select(Annotation.supersedes_id).where(
        Annotation.supersedes_id.isnot(None)
    )
    return (
        await db.execute(
            select(Annotation)
            .where(
                Annotation.task_id == task.id,
                Annotation.annotator_id == annotator_id,
                Annotation.id.notin_(superseded),
            )
            .order_by(Annotation.created_at.desc(), Annotation.id.desc())
            .limit(1)
        )
    ).scalars().first()


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
    blind_to_peers: bool = False,
    session: AnnotationSession | None = None,
    commit: bool = True,
) -> dict:
    """Append one rating of ``task`` to the annotation ledger.

    Ensures the quality record exists (building it on demand, mirroring the
    judge), freezes the judge's side, appends the row — superseding this
    annotator's own previous rating rather than the record's — and refreshes the
    materialised ``human_feedback`` slot when the rating came from a human.
    Returns the stored feedback dict.

    ``session`` is the annotation session this rating was produced through; when
    given it is the authority on the protocol (and is consumed here), because it
    records what the server actually served. Without one the rating is recorded
    as sighted — no session, no claim.
    """
    if annotator_type not in ANNOTATOR_TYPES:
        raise ValueError(f"unknown annotator_type: {annotator_type}")
    if session is not None:
        blind_to_judge = session.blind_to_judge
        blind_to_model = session.blind_to_model
        blind_to_peers = session.blind_to_peers
        protocol_version = session.protocol_version
        session.consumed_at = datetime.utcnow()
    else:
        protocol_version = PROTOCOL_VERSION

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

    annotation = Annotation(
            quality_record_id=record.id,
            task_id=task.id,
            workspace_id=task.workspace_id,
            annotator_type=annotator_type,
            annotator_id=annotator_id,
            annotator_label=annotator_label,
            protocol_version=protocol_version,
            blind_to_model=blind_to_model,
            blind_to_judge=blind_to_judge,
            blind_to_peers=blind_to_peers,
            verdict=feedback["verdict"],
            overall_comment=feedback["overall_comment"],
            dimensions=feedback["dimensions"],
            judge_observation=observation,
            supersedes_id=supersedes_id,
            session_id=session.id if session is not None else None,
    )
    db.add(annotation)
    await db.flush()
    # The row's own timestamp is the authority. Without this the response and a
    # later read of the same rating disagree about when it happened — including
    # an idempotent retry, which rebuilds its answer from the row.
    if annotation.created_at is not None:
        feedback["submitted_at"] = annotation.created_at.isoformat()
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
