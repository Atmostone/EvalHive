"""Ranking service — Aggregation Engine persistence + match sourcing (E-19).

Wraps the pure :mod:`app.quality.aggregation` engine with the workspace plumbing
the rest of the eval features share: it sources the matches, runs ``rank()``, and
persists an append-only, versioned leaderboard in ``ranking_reports`` (mirroring
the E-17 judge-calibration / E-18 bias-report services).

Where the matches come from is the interesting part. E-19's intended feed is the
pairwise comparison framework (E-21), which does not exist yet. Until it does,
:func:`derive_matches_from_records` bridges the gap by turning the **pointwise**
scores already in the Quality Data Lake (E-01/E-02, ``quality_profile.weighted_score``)
into head-to-head matches: within one benchmark case, the model/template with the
higher mean score "beats" the other. This makes the leaderboard rank real
competitors today; when E-21 lands it simply supplies real matches to the same
:func:`run_ranking`. Callers can also pass an explicit match list (the literal
``rank(pairwise_results, method)`` acceptance) to bypass the derivation entirely.

Reports are versioned per ``(workspace_id, ranking_key)`` where ``ranking_key`` is
``"{subject}:{method}"`` (e.g. ``"model:bt"``), so the BT and Elo leaderboards for
models and templates each keep their own history line.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.ranking_report import RankingReport
from app.quality.aggregation import rank
from app.utils.events import log_event

logger = logging.getLogger(__name__)

DEFAULT_TIE_EPSILON = 0.5
DEFAULT_RESAMPLES = 200
DEFAULT_SEED = 0


# --------------------------------------------------------------------------- #
# Match sourcing — derive pairwise matches from stored pointwise scores
# --------------------------------------------------------------------------- #
def _player_key(record: QualityRecord, subject: str) -> str | None:
    """The leaderboard identity of a record for the chosen ``subject`` axis.

    ``model`` → the denormalized ``model_used`` (an LLM ``api_name``); ``template``
    → the readable ``template_name`` falling back to the template id. ``None`` when
    the record can't be placed on that axis (so it's counted unmatched)."""
    if subject == "template":
        if record.template_name:
            return record.template_name
        return str(record.template_id) if record.template_id else None
    return record.model_used or None


def build_matches(
    scored: list[dict], *, subject: str = "model", epsilon: float = DEFAULT_TIE_EPSILON
) -> tuple[list[dict], dict]:
    """Pure pairing: turn scored records into head-to-head matches.

    ``scored`` is a list of ``{"case", "player", "score"}`` rows (already filtered
    to those that carry all three). Records are grouped by ``case`` — the
    controlled context, the same benchmark item run by different competitors — and
    each player is reduced to its mean score within a case; then one match is
    emitted per player-pair per case: higher mean wins, a gap within ``epsilon`` is
    a tie. A player alone in its case has no opponent and contributes to
    ``n_unmatched`` instead. Returns ``(matches, meta)``; ``meta`` does not yet
    include records that were dropped before scoring (the DB layer adds those)."""
    groups: dict[str, dict[str, list[float]]] = {}
    for row in scored:
        groups.setdefault(row["case"], {}).setdefault(row["player"], []).append(float(row["score"]))

    matches: list[dict] = []
    used_cases = 0
    records_used = 0
    records_unused = 0
    players_set: set[str] = set()
    for pmap in groups.values():
        n_records = sum(len(v) for v in pmap.values())
        if len(pmap) < 2:
            records_unused += n_records  # a lone competitor has no one to play
            continue
        used_cases += 1
        records_used += n_records
        means = {p: sum(v) / len(v) for p, v in pmap.items()}
        players_set.update(means)
        ordered = sorted(means)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                a, b = ordered[i], ordered[j]
                if abs(means[a] - means[b]) <= epsilon:
                    outcome = "tie"
                elif means[a] > means[b]:
                    outcome = "a"
                else:
                    outcome = "b"
                matches.append({"player_a": a, "player_b": b, "outcome": outcome, "weight": 1})

    meta = {
        "subject": subject,
        "n_cases": used_cases,
        "n_records_used": records_used,
        "n_unmatched": records_unused,
        "n_players": len(players_set),
        "epsilon": epsilon,
    }
    return matches, meta


async def derive_matches_from_records(
    db: AsyncSession,
    *,
    workspace_id,
    subject: str = "model",
    suite: str | None = None,
    epsilon: float = DEFAULT_TIE_EPSILON,
) -> tuple[list[dict], dict]:
    """Build head-to-head matches from pointwise ``quality_profile.weighted_score``.

    Loads scored records (optionally suite-scoped), extracts ``(case, player,
    score)`` for each, and hands the valid ones to :func:`build_matches`. Records
    missing a score, a ``benchmark_case_id``, or a player key on the chosen axis are
    counted toward ``n_unmatched`` rather than silently dropped. Returns
    ``(matches, meta)``."""
    q = select(QualityRecord).where(
        QualityRecord.workspace_id == workspace_id,
        QualityRecord.quality_profile.isnot(None),
    )
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    rows = (await db.execute(q)).scalars().all()

    scored: list[dict] = []
    missing = 0
    for r in rows:
        score = (r.quality_profile or {}).get("weighted_score")
        case = r.benchmark_case_id
        player = _player_key(r, subject)
        if score is None or not case or not player:
            missing += 1
            continue
        try:
            score_f = float(score)
        except (TypeError, ValueError):
            missing += 1
            continue
        scored.append({"case": case, "player": player, "score": score_f})

    matches, meta = build_matches(scored, subject=subject, epsilon=epsilon)
    meta["n_unmatched"] += missing
    return matches, meta


# --------------------------------------------------------------------------- #
# Persistence / public API
# --------------------------------------------------------------------------- #
def _serialize(row: RankingReport) -> dict:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "ranking_key": row.ranking_key,
        "subject": row.subject,
        "method": row.method,
        "version": row.version,
        "n_players": row.n_players,
        "n_matches": row.n_matches,
        "passed": row.passed,
        "filters": row.filters or {},
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "metrics": row.metrics or {},
    }


async def _setting_number(db, key: str, default, cast):
    from app.api.settings import get_setting

    try:
        return cast(await get_setting(db, key, default))
    except (TypeError, ValueError):
        return default


async def run_ranking(
    db: AsyncSession,
    *,
    workspace_id,
    subject: str = "model",
    method: str = "bt",
    suite: str | None = None,
    matches: list[dict] | None = None,
    created_by: str = "user",
    commit: bool = True,
) -> dict:
    """Compute a ranking report and persist it as the next version for this
    workspace's ``(subject, method)`` line. When ``matches`` is given it is ranked
    directly (``source="explicit"``); otherwise matches are derived from stored
    pointwise scores (``source="derived"``). Returns the serialized report row."""
    subject = "template" if subject == "template" else "model"
    method = "elo" if method == "elo" else "bt"

    epsilon = await _setting_number(db, "ranking_tie_epsilon", DEFAULT_TIE_EPSILON, float)
    resamples = await _setting_number(db, "ranking_bootstrap_resamples", DEFAULT_RESAMPLES, int)
    seed = await _setting_number(db, "ranking_seed", DEFAULT_SEED, int)

    derivation: dict | None = None
    if matches is not None:
        source = "explicit"
        match_list = matches
    else:
        source = "derived"
        match_list, derivation = await derive_matches_from_records(
            db, workspace_id=workspace_id, subject=subject, suite=suite, epsilon=epsilon
        )

    report = rank(match_list, method=method, n_resamples=resamples, seed=seed, tie_epsilon=epsilon)
    report["subject"] = subject
    report["source"] = source
    if derivation is not None:
        report["derivation"] = derivation

    ranking_key = f"{subject}:{method}"
    maxv = (
        await db.execute(
            select(func.max(RankingReport.version)).where(
                RankingReport.workspace_id == workspace_id,
                RankingReport.ranking_key == ranking_key,
            )
        )
    ).scalar()
    version = (maxv or 0) + 1

    passed = report["status"] == "ok"
    row = RankingReport(
        workspace_id=workspace_id,
        ranking_key=ranking_key,
        subject=subject,
        method=method,
        version=version,
        n_players=report["n_players"],
        n_matches=report["n_matches"],
        filters={"suite": suite, "source": source},
        metrics=report,
        passed=passed,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    await log_event(
        db,
        "ranking_run",
        "system",
        {
            "ranking_key": ranking_key,
            "version": version,
            "method": method,
            "subject": subject,
            "source": source,
            "n_players": report["n_players"],
            "n_matches": report["n_matches"],
            "status": report["status"],
        },
        workspace_id=workspace_id,
        commit=False,
    )
    if commit:
        await db.commit()
        await db.refresh(row)
    return _serialize(row)


async def get_ranking(
    db: AsyncSession, *, workspace_id, ranking_key: str | None = None
) -> dict | None:
    """The latest leaderboard for a ``ranking_key`` (or the most recent across all
    keys when none given), or ``None`` when the workspace has never ranked."""
    q = select(RankingReport).where(RankingReport.workspace_id == workspace_id)
    if ranking_key:
        q = q.where(RankingReport.ranking_key == ranking_key)
    q = q.order_by(RankingReport.created_at.desc(), RankingReport.version.desc()).limit(1)
    row = (await db.execute(q)).scalar_one_or_none()
    return _serialize(row) if row is not None else None


async def list_rankings(
    db: AsyncSession, *, workspace_id, ranking_key: str | None = None, limit: int = 50
) -> list[dict]:
    """Version history, newest first."""
    q = select(RankingReport).where(RankingReport.workspace_id == workspace_id)
    if ranking_key:
        q = q.where(RankingReport.ranking_key == ranking_key)
    q = q.order_by(RankingReport.created_at.desc(), RankingReport.version.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_serialize(r) for r in rows]


async def get_ranking_badge(db: AsyncSession, *, workspace_id) -> dict:
    """Compact badge data: 'leaderboard of N players, top = X'."""
    latest = await get_ranking(db, workspace_id=workspace_id)
    if latest is None:
        return {"ranked": False}
    metrics = latest.get("metrics") or {}
    players = metrics.get("players") or []
    top = players[0]["player"] if players else None
    return {
        "ranked": True,
        "ranking_key": latest.get("ranking_key"),
        "subject": latest.get("subject"),
        "method": latest.get("method"),
        "version": latest.get("version"),
        "n_players": latest.get("n_players", 0),
        "n_matches": latest.get("n_matches", 0),
        "status": metrics.get("status"),
        "top_player": top,
        "created_at": latest.get("created_at"),
    }
