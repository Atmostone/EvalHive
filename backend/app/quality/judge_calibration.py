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

from app.models.annotation import Annotation
from app.models.judge_calibration import JudgeCalibration
from app.models.quality_record import QualityRecord
from app.quality.feedback import HUMAN_TYPES, observed_reasoning, observed_scores
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
def _pair_row(
    ann: Annotation,
    *,
    gate_passed,
    judge_source: str,
    dimension_key=None,
    dimension_name=None,
    judge_score=None,
    human_score=None,
    band=None,
    judge_reasoning=None,
    human_comment=None,
) -> dict:
    """One exported pair. ``dimension_key`` is ``None`` for a verdict-only rating,
    which carries no per-dimension scores but is still a rating by a person."""
    return {
        "task_id": str(ann.task_id),
        "annotation_id": str(ann.id),
        "annotator_type": ann.annotator_type,
        "annotator_id": str(ann.annotator_id) if ann.annotator_id else None,
        "protocol_version": ann.protocol_version,
        "blind_to_model": ann.blind_to_model,
        "blind_to_judge": ann.blind_to_judge,
        "blind_to_peers": ann.blind_to_peers,
        "dimension_key": dimension_key,
        "dimension_name": dimension_name,
        "judge_score": judge_score,
        "judge_source": judge_source,
        "human_score": human_score,
        "band": band,
        "judge_reasoning": judge_reasoning,
        "human_comment": human_comment,
        "verdict": ann.verdict,
        "judge_gate_passed": gate_passed,
        "submitted_by": ann.annotator_label,
        "submitted_at": ann.created_at.isoformat() if ann.created_at else None,
    }


async def collect_judge_human_pairs(
    db: AsyncSession,
    workspace_id,
    *,
    suite: str | None = None,
    template_id=None,
    task_ids=None,
    annotator_types: tuple[str, ...] = HUMAN_TYPES,
) -> list[dict]:
    """One row per rated dimension across the *current* annotations (SPA-85).

    The source of truth for both the ``GET /api/quality/calibration`` export and
    the E-17 report. ``suite``/``template_id`` narrow the population; ``task_ids``
    (a collection of task UUIDs) scopes calibration to one experiment's runs;
    ``annotator_types`` defaults to the human ones, so an unattended machine
    annotation never silently enters a judge-vs-human comparison.

    Superseded rows are excluded: a re-rating replaces its predecessor rather
    than adding a second sample from the same annotator. Two *different*
    annotators on one run stay as two rows — that is the population
    inter-annotator agreement is computed over.

    The judge's side comes from the observation frozen into the annotation, so
    re-running a judge cannot move a past pair. ``judge_source`` says which:
    ``frozen`` (a score was recorded at annotation time), ``unscored`` (the judge
    had NOT scored that axis when the human rated it — the absence is frozen too,
    and a later judge run must not fill it in), or ``live`` (re-read from the
    current profile — reachable only for pre-ledger ``legacy`` rows, which froze
    nothing and never can)."""
    superseded = select(Annotation.supersedes_id).where(
        Annotation.supersedes_id.isnot(None)
    )
    q = (
        select(Annotation, QualityRecord)
        .join(QualityRecord, Annotation.quality_record_id == QualityRecord.id)
        .where(
            Annotation.workspace_id == workspace_id,
            Annotation.annotator_type.in_(annotator_types),
            Annotation.id.notin_(superseded),
        )
    )
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    if template_id is not None:
        q = q.where(QualityRecord.template_id == template_id)
    if task_ids is not None:
        ids = list(task_ids)
        if not ids:
            return []
        q = q.where(Annotation.task_id.in_(ids))
    rows = (await db.execute(q)).all()

    out: list[dict] = []
    for ann, record in rows:
        # Only a pre-ledger row may be completed from a live profile. For every
        # annotation that froze its own observation, a judge score that was
        # ABSENT at annotation time stays absent: letting a later judge run fill
        # the gap is exactly the retroactive change the ledger exists to prevent
        # (the axis was not scored when the human rated it, so there is no pair).
        legacy = ann.annotator_type == "legacy"
        frozen_scores = observed_scores(ann.judge_observation)
        frozen_reasoning = observed_reasoning(ann.judge_observation)
        profile = record.quality_profile or {}
        live: dict = {}
        if legacy:
            live = {d.get("key"): d for d in (profile.get("dimensions") or [])}
            # Process/trajectory (E-07) axes are calibratable judge dimensions too.
            for a in (record.trajectory_profile or {}).get("axes") or []:
                live.setdefault(a.get("key"), a)
        gate_passed = (ann.judge_observation or {}).get("outcome", {}).get("gate_passed")
        gate_source = "frozen"
        if gate_passed is None:
            if legacy:
                gate_passed = (profile.get("gate") or {}).get("passed")
                gate_source = "live"
            else:
                gate_source = "unscored"

        if not (ann.dimensions or []):
            # A verdict-only rating (the form allows one, and on a verifiable
            # bench it is the whole human signal). Emitting nothing would drop the
            # annotation out of n_humans, n_annotations AND the verdict agreement
            # it exists to feed, so it gets one dimension-less row: the
            # per-dimension grouping skips it, everything keyed on the annotator
            # counts it.
            out.append(
                _pair_row(ann, gate_passed=gate_passed, judge_source=gate_source)
            )
            continue

        for d in ann.dimensions:
            key = d.get("key")
            judge_score = frozen_scores.get(key)
            if judge_score is None:
                # Captured alongside the rating, so still frozen in time.
                judge_score = d.get("judge_score")
            judge_source = "frozen"
            if judge_score is None:
                if legacy:
                    judge_score = (live.get(key) or {}).get("score")
                    judge_source = "live"
                else:
                    judge_source = "unscored"
            out.append(
                _pair_row(
                    ann,
                    gate_passed=gate_passed,
                    judge_source=judge_source,
                    dimension_key=key,
                    dimension_name=d.get("name"),
                    judge_score=judge_score,
                    human_score=d.get("score"),
                    band=d.get("band"),
                    judge_reasoning=(
                        frozen_reasoning.get(key) or (live.get(key) or {}).get("reasoning")
                    ),
                    human_comment=d.get("comment"),
                )
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


def _annotator_key(p: dict) -> str | None:
    """Stable identity of the annotator behind a pair — the user id when there
    is one, else the display label (legacy rows carry no user)."""
    return p.get("annotator_id") or p.get("submitted_by")


def _pooled_kappa(
    by_unit: dict[str, dict[str, str]], labels: list[str]
) -> tuple[float | None, float | None, int]:
    """Cohen's κ pooled over every unordered pair of annotators who rated the
    same unit. ``by_unit`` maps unit → annotator → label; a unit rated by k
    annotators contributes k(k-1)/2 observations. Pooling is the honest
    approximation while the corpus is small: with two annotators it *is* Cohen's
    κ, and with more it averages the pairwise agreement rather than pretending
    to a single fixed pair."""
    a: list[str] = []
    b: list[str] = []
    for raters in by_unit.values():
        who = sorted(raters)
        for i in range(len(who)):
            for j in range(i + 1, len(who)):
                a.append(raters[who[i]])
                b.append(raters[who[j]])
    n = len(a)
    if not n:
        return None, None, 0
    kappa = cohen_kappa(a, b, labels) if n >= MIN_SAMPLES else None
    agreement = round(sum(1 for x, y in zip(a, b) if x == y) / n, 4)
    return kappa, agreement, n


def _inter_annotator(pairs: list[dict], *, threshold_kappa: float) -> dict:
    """Agreement BETWEEN annotators on the same run.

    The number a single overwritable feedback slot made impossible to compute:
    before the ledger a run could only ever carry one rating. It answers a
    different question from the judge-vs-human κ above — how reproducible the
    human gold itself is — and it bounds what the judge can be asked to match.

    Computed **only** over ratings collected without sight of the other
    annotators' (`blind_to_peers`). Two ratings seeded from each other agree by
    construction, so including them would inflate exactly the number that is
    supposed to say whether humans agree."""
    pairs = [p for p in pairs if p.get("blind_to_peers")]
    by_dim: dict[str, dict[str, dict[str, str]]] = {}   # dim → task → annotator → band
    by_verdict: dict[str, dict[str, str]] = {}          # task → annotator → verdict
    names: dict[str, str] = {}
    raters_by_task: dict[str, set] = {}

    for p in pairs:
        who = _annotator_key(p)
        task = p.get("task_id")
        if not who or not task:
            continue
        raters_by_task.setdefault(task, set()).add(who)
        key = p.get("dimension_key")
        band = p.get("band") or score_to_band(p.get("human_score"))
        if key and band:
            names.setdefault(key, p.get("dimension_name") or key)
            by_dim.setdefault(key, {}).setdefault(task, {})[who] = band
        if p.get("verdict") in VERDICT_LABELS:
            by_verdict.setdefault(task, {})[who] = p["verdict"]

    dimensions: list[dict] = []
    for key in sorted(by_dim):
        kappa, agreement, n = _pooled_kappa(by_dim[key], BANDS)
        if not n:
            continue
        dimensions.append(
            {
                "key": key,
                "name": names.get(key, key),
                "n": n,
                "cohen_kappa": kappa,
                "agreement_pct": agreement,
                "reliable": kappa is not None and kappa >= threshold_kappa,
                "status": "ok" if n >= MIN_SAMPLES else "insufficient_data",
            }
        )

    o_kappa, o_agreement, o_n = _pooled_kappa(by_verdict, VERDICT_LABELS)
    n_records = sum(1 for who in raters_by_task.values() if len(who) > 1)
    return {
        # False when no run in the population carries a second annotator — the
        # normal state of a corpus collected by one person.
        "available": n_records > 0,
        "n_records": n_records,
        "n_annotators": len({w for who in raters_by_task.values() for w in who}),
        "dimensions": dimensions,
        "overall": {
            "n": o_n,
            "cohen_kappa": o_kappa,
            "agreement_pct": o_agreement,
            "reliable": o_kappa is not None and o_kappa >= threshold_kappa,
        },
    }


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
            "judge_mean": round(sum(judge_scores) / n, 2) if n else None,
            "human_mean": round(sum(human_scores) / n, 2) if n else None,
            "reliable": kappa is not None and kappa >= threshold_kappa,
            "status": "ok" if n >= MIN_SAMPLES else "insufficient_data",
        }
        dimensions.append(dim)

    # Overall verdict-agreement: one (judge_gate, human_verdict) pair per task
    # AND annotator. Keying on the task alone was safe only while a run could
    # carry a single rating — with two annotators it silently kept whichever one
    # the iteration happened to reach last (SPA-85).
    verdict_pairs: dict[tuple, tuple[str, str]] = {}
    for p in pairs:
        tid = p.get("task_id")
        verdict = p.get("verdict")
        gate = p.get("judge_gate_passed")
        if tid is None or verdict not in VERDICT_LABELS or gate is None:
            continue
        judge_verdict = "approve" if gate else "reject"
        verdict_pairs[(tid, _annotator_key(p))] = (judge_verdict, verdict)
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
    # People, not account strings (SPA-85). `legacy` rows — everything collected
    # before the ledger — carry no user id and are counted separately rather
    # than folded in, because there is no honest way to attribute them.
    n_humans = len(
        {
            p.get("annotator_id")
            for p in pairs
            if p.get("annotator_type") == "human" and p.get("annotator_id")
        }
    )
    n_records = len({p.get("task_id") for p in pairs if p.get("task_id")})
    n_annotations = len({p.get("annotation_id") for p in pairs if p.get("annotation_id")})
    n_legacy = len(
        {
            p.get("annotation_id")
            for p in pairs
            if p.get("annotator_type") == "legacy" and p.get("annotation_id")
        }
    )
    n_annotators = len({k for p in pairs if (k := _annotator_key(p))})
    # Share of pairs whose judge side cannot move under a re-judge — either a
    # score was frozen onto the annotation or its absence was. Below 1.0 means
    # some pair is still being rebuilt from a live profile (`legacy` rows only).
    fixed = sum(1 for p in pairs if p.get("judge_source") != "live")
    judge_frozen_pct = round(fixed / len(pairs), 4) if pairs else None

    return {
        "threshold_kappa": threshold_kappa,
        "sample_size": len(pairs),
        "n_records": n_records,
        "n_humans": n_humans,
        "n_annotators": n_annotators,
        "n_annotations": n_annotations,
        "n_legacy": n_legacy,
        "judge_frozen_pct": judge_frozen_pct,
        "n_dimensions": len(dimensions),
        "dimensions": dimensions,
        "overall": overall,
        "inter_annotator": _inter_annotator(pairs, threshold_kappa=threshold_kappa),
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
            "n_legacy": report["n_legacy"],
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
    inter = metrics.get("inter_annotator") or {}
    return {
        "calibrated": True,
        "n_humans": metrics.get("n_humans", 0),
        # Pre-ledger ratings, which cannot be attributed to a person (SPA-85).
        "n_legacy": metrics.get("n_legacy", 0),
        "judge_frozen_pct": metrics.get("judge_frozen_pct"),
        "inter_annotator_kappa": (inter.get("overall") or {}).get("cohen_kappa"),
        "inter_annotator_records": inter.get("n_records", 0),
        "sample_size": latest.get("sample_size", 0),
        "overall_kappa": overall.get("cohen_kappa"),
        "judge_config_key": latest.get("judge_config_key"),
        "version": latest.get("version"),
        "passed": latest.get("passed"),
        "created_at": latest.get("created_at"),
    }
