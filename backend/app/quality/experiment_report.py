"""Experiment report assembly (SPA-40).

Turns the settled matrix of an experiment into the report views: per-config
summary, quality-profile heatmap (configs × rubric dimensions), Pareto
frontier (quality ↑ × cost ↓ × time ↓), outcome × trajectory scatter, a
pairwise leaderboard derived from pointwise scores (E-19 ``build_matches`` +
``rank``), statistical significance per config pair (Welch primary,
Mann-Whitney as the non-parametric check), failure-mode breakdown, and the
orchestrator on/off comparison.

``build_report`` is pure given pre-loaded rows; ``compute_report`` is the
DB-bound convenience that loads them. The API caches the result into
``experiments.report`` once the experiment is terminal.
"""

from __future__ import annotations

import statistics
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.experiment import (
    Experiment,
    ExperimentAttempt,
    ExperimentRun,
    ExperimentRunStatus,
)
from app.models.quality_record import QualityRecord
from app.quality.aggregation import rank
from app.quality.experiments import (
    LIVE_CELL,
    _agent_image_ids,
    _resolve_config_state,
    experiment_input_fingerprint,
    live_configs,
)
from app.quality.ranking import build_matches
from app.quality.stats import MIN_SAMPLES, mann_whitney_u, rank_auc, welch_t_test
from app.quality.trajectory import AXES as TRAJECTORY_AXES
from app.utils.failures import is_contaminated

SCHEMA_VERSION = 16  # v16: the judge↔checker headline no longer depends on a
# threshold (judge_discrimination: score distributions split by the checker's
# verdict + AUC); the 2×2 stays as the over-credit illustration, computed at a
# threshold the experiment PRE-REGISTERED; runs killed by infrastructure are
# excluded from the aggregates and counted in `exclusions` (SPA-87)
# v15: config_drift — configurations whose frozen resolution
# (model api_name, template content hash, agent image id) no longer matches reality
# v14: the report records the experiment revision + input
# fingerprint it was computed from, so a cached report can be matched against its
# input instead of trusted for existing (SPA-84); explicit run-selection policy
# v13: reliability traffic light on the OUTCOME rubric axes too
# (outcome_axis_reliability), + rank-aware 'directional' zone: κ below the bar but
# Spearman ρ≥0.5 = scale-shifted judge — ordering trustworthy, absolute scores not
# v12: checker↔human agreement (Cohen's κ + raw) — the executable
# checker vs the human gold verdict, surfaced beside judge↔human calibration
# v11: retire the unreliable judge loop_detection axis — drop it
# from the displayed E-07 axes AND from the trajectory aggregate (a quarantined axis
# must not be weighed into conclusions, SPA-76); deterministic counter (SPA-75) stays
# v10: confound-controlled effort (SPA-77) — token/$ effort,
# difficulty-normalized per case; wall-clock demoted to a caveated secondary
# v9: per-axis reliability gate (SPA-76) — E-17/loop-anchor κ badge
# v8: loop anchor directional split (judge-only/counter-only) + Cohen's κ
# v7: deterministic loop anchor (structural_loop_rate + judge↔counted agreement)
# v6: trace_stats (E-06) + longitudinal (E-22 across run_index)
# v5: loop_detection + quality_gate per config, failure reasons,
# quality-heatmap dimension_labels
# v4: human_feedback (E-05 per-config aggregate) + cost_breakdown
# v3: external (executable pass-rate) + rq2 (verdict × judge 2×2)
# Which executions of a cell a report counts (SPA-84). Before the attempt ledger
# existed there was only ever one survivor per cell, so this was not a choice
# anyone could make — it was whatever the last retry happened to leave behind.
SELECTION_LATEST_VALID = "latest_valid"  # current state of each live cell (default)
SELECTION_ALL_ATTEMPTS = "all_attempts"  # every execution, retired cells included
SELECTION_FIRST_ATTEMPT = "first_attempt"  # each cell's first execution only
SELECTION_POLICIES = (
    SELECTION_LATEST_VALID,
    SELECTION_ALL_ATTEMPTS,
    SELECTION_FIRST_ATTEMPT,
)

SIGNIFICANCE_ALPHA = 0.05
# Outcome-judge threshold splitting "high" vs "low" in the RQ2 verdict×judge 2×2 —
# the FALLBACK, used only when an experiment pre-registered none. An experiment
# that means to report a 2×2 sets `eval_config.judge_threshold` at creation, and
# because eval_config is write-once and inside the revision fingerprint, that
# choice is evidence it was made before the results were seen. The pilot counted
# over-credit at ≥6 and this constant said ≥5; nothing recorded the change, and
# nothing could have, because it lived in the code.
RQ2_JUDGE_THRESHOLD = 5.0
# Neighbouring cut-offs reported next to the pre-registered one. Sensitivity is
# shown so the reader can see how much the illustration moves — explicitly
# EXPLORATORY, never the headline. The headline is `judge_discrimination`, which
# has no threshold to move.
RQ2_SENSITIVITY_THRESHOLDS = (4.0, 5.0, 6.0, 7.0)

_SETTLED = {
    ExperimentRunStatus.SUCCESS.value,
    ExperimentRunStatus.FAILED.value,
    ExperimentRunStatus.SKIPPED.value,
}


def _mean(values: list[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return round(statistics.fmean(vals), 4) if vals else None


def _std(values: list[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return None
    return round(statistics.pstdev(vals), 4)


def _median(values) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return round(statistics.median(vals), 4) if vals else None


# --- SPA-77 effort accessors (confound-controlled) --------------------------- #
# Effort is measured by LLM tokens (deterministic) — NOT wall-clock, which is
# polluted by provider throttling + sleep/wait. Tokens live on the QualityRecord
# (denormalized from task.token_usage), not on ExperimentRun; cost is sparse
# ($0 for un-metered providers — ~74% of runs), so tokens are the primary signal
# and cost is the priced secondary.
def _run_effort_tokens(rec) -> Optional[float]:
    """Total LLM tokens (input + output) for a run, or None when unrecorded."""
    if rec is None:
        return None
    it = getattr(rec, "input_tokens", None)
    ot = getattr(rec, "output_tokens", None)
    if it is None and ot is None:
        return None
    return float((it or 0) + (ot or 0))


def _run_steps(rec) -> Optional[float]:
    """Agent step count — tool_call_count (near-100% populated), else the E-06
    trace steps_total fallback."""
    if rec is None:
        return None
    tcc = getattr(rec, "tool_call_count", None)
    if tcc is not None:
        return float(tcc)
    ts = (getattr(rec, "trajectory_profile", None) or {}).get("trace_stats") or {}
    st = ts.get("steps_total")
    return float(st) if st is not None else None


# The judge loop_detection axis is retired from the trajectory aggregate (v11): it is
# unreliable vs humans (κ≈0, SPA-76) and SPA-76 promises quarantined axes are "not
# weighed in conclusions" — yet the stored overall_score averaged it in (trajectory.py
# overall = mean of all 6 axes). The deterministic loop counter (SPA-75) carries the
# loop signal instead.
_AGG_EXCLUDED_AXES = {"loop_detection"}


def _traj_score(rec, stored: Optional[float]) -> Optional[float]:
    """Trajectory aggregate EXCLUDING the quarantined loop_detection axis (v11).
    Recompute the mean from the stored per-axis scores; fall back to the stored
    6-axis overall when the per-axis breakdown is unavailable."""
    axes = (getattr(rec, "trajectory_profile", None) or {}).get("axes") if rec is not None else None
    if axes:
        vals = [
            float(a["score"])
            for a in axes
            if a.get("key") not in _AGG_EXCLUDED_AXES and a.get("score") is not None
        ]
        if vals:
            return sum(vals) / len(vals)
    return stored


def _binary_kappa(both_yes: int, a_only: int, b_only: int, both_no: int) -> Optional[float]:
    """Cohen's κ for two binary raters on a 2×2 (chance-corrected agreement). None
    when undefined — no data, or perfect-by-base-rate where p_e == 1 (e.g. every
    run agrees and all-negative): κ is 0/0 there, which a raw agreement % hides."""
    n = both_yes + a_only + b_only + both_no
    if n == 0:
        return None
    po = (both_yes + both_no) / n
    p_a_yes = (both_yes + a_only) / n
    p_b_yes = (both_yes + b_only) / n
    pe = p_a_yes * p_b_yes + (1 - p_a_yes) * (1 - p_b_yes)
    if pe >= 1.0:
        return None
    return round((po - pe) / (1 - pe), 4)


# --- SPA-76 reliability gate ------------------------------------------------ #
# κ here is the chance-corrected agreement between the LLM process-judge (E-07)
# and a ground-truth-ish reference for that axis: a human (E-17 judge↔human), or
# — for the loop axis only — the deterministic SPA-75 counter (judge↔counter).
# Above the bar the judge can drive a conclusion; below it the axis is
# quarantined (shown for completeness, not weighed). Never fabricated.
RELIABILITY_RELIABLE_KAPPA = 0.6     # judge agrees with the reference → trust it
RELIABILITY_DIRECTIONAL_KAPPA = 0.4  # weak-but-directional floor
# A judge can be systematically scale-shifted yet order runs correctly: band κ
# collapses while rank correlation stays high. Such an axis is usable for
# COMPARING runs (never for absolute scores), so strong ranks rescue it into
# 'directional' instead of 'unreliable'. The κ thresholds above stay untouched.
RELIABILITY_RANK_RHO = 0.5


def _classify_reliability(
    kappa: Optional[float], n: int, *, has_source: bool, rho: Optional[float] = None
) -> str:
    """Bucket a judged axis. No calibration source → 'not_calibrated'
    (unknown, not known-bad). A live source with too little data or an undefined
    κ → 'directional' (a hint, not a verdict). Otherwise threshold on κ, with one
    rescue: κ below the bar but ranks agreeing (Spearman ρ ≥ RELIABILITY_RANK_RHO)
    → 'directional' — a scale-shifted judge, trustworthy for ordering only."""
    if not has_source:
        return "not_calibrated"
    if kappa is None or n < MIN_SAMPLES:
        return "directional"
    if kappa >= RELIABILITY_RELIABLE_KAPPA:
        return "reliable"
    if kappa >= RELIABILITY_DIRECTIONAL_KAPPA:
        return "directional"
    if rho is not None and rho >= RELIABILITY_RANK_RHO:
        return "directional"
    return "unreliable"


def _axis_reliability(
    calibration: Optional[dict],
    loop_detection: dict,
    axis_labels: dict[str, str],
) -> dict:
    """Per-axis reliability badge for the six E-07 trajectory axes, from REAL
    calibration only:
      • judge↔human (E-17) per-axis Cohen's κ — the gold standard, used whenever a
        human rated that axis on these runs (n ≥ MIN_SAMPLES);
      • judge↔counter (SPA-75) structural κ — the loop axis only, available on
        every trajectory-scored run with no humans needed.
    Human wins when it has enough data; the loop axis falls back to the structural
    anchor; everything else with no source is an honest 'not_calibrated'. (A future
    hook: back off to the workspace-global E-17 calibration when the per-experiment
    human sample is thin — skipped in v1 as the current global snapshot predates the
    trajectory-axis fold-in.)"""
    human_dims: dict[str, dict] = {}
    if isinstance(calibration, dict) and calibration.get("available"):
        for d in calibration.get("dimensions") or []:
            if d.get("key"):
                human_dims[d["key"]] = d

    struct_kappa = None
    struct_n = 0
    if isinstance(loop_detection, dict) and loop_detection.get("structural_available"):
        struct_kappa = loop_detection.get("kappa")
        struct_n = int(loop_detection.get("n_structural") or 0)

    axes_out: dict[str, dict] = {}
    any_source = False
    for key, name, _desc in TRAJECTORY_AXES:
        if key in _AGG_EXCLUDED_AXES:
            continue  # v11: loop axis retired — its κ no longer badges a displayed axis
        label = axis_labels.get(key) or name
        hd = human_dims.get(key)
        h_n = int(hd.get("n") or 0) if hd else 0
        h_kappa = hd.get("cohen_kappa") if hd else None
        struct_ok = key == "loop_detection" and struct_n > 0

        if hd is not None and h_n >= MIN_SAMPLES:
            source, kappa, n = "human", h_kappa, h_n
        elif struct_ok and struct_n >= MIN_SAMPLES:
            source, kappa, n = "structural", struct_kappa, struct_n
        elif hd is not None:  # human source exists but too few pairs
            source, kappa, n = "human", h_kappa, h_n
        elif struct_ok:  # structural ran but very few runs
            source, kappa, n = "structural", struct_kappa, struct_n
        else:
            source, kappa, n = "none", None, 0

        # Rank correlation only exists for the human source (the structural loop
        # anchor is binary — no meaningful per-run ordering to correlate).
        rho = hd.get("spearman") if source == "human" and hd is not None else None
        has_source = source != "none"
        any_source = any_source or has_source
        axes_out[key] = {
            "key": key,
            "name": label,
            "source": source,
            "kappa": kappa,
            "rho": rho,
            "n": n,
            "status": _classify_reliability(kappa, n, has_source=has_source, rho=rho),
        }

    return {
        "available": any_source,
        "reliable_kappa": RELIABILITY_RELIABLE_KAPPA,
        "directional_kappa": RELIABILITY_DIRECTIONAL_KAPPA,
        "rank_rho": RELIABILITY_RANK_RHO,
        "min_samples": MIN_SAMPLES,
        "axes": axes_out,
    }


def _outcome_axis_reliability(calibration: Optional[dict]) -> dict:
    """The same reliability traffic light, applied to the OUTCOME rubric
    dimensions. Unlike the fixed six trajectory axes, the outcome axis list is
    rubric-dependent — so it is whatever dimensions the humans actually rated on
    these runs (trajectory keys excluded; they are badged by _axis_reliability).
    Source is always the human: the executable checker verifies the verdict, not
    per-dimension scores, and on open tasks it does not exist at all."""
    traj_keys = {key for key, _name, _desc in TRAJECTORY_AXES}
    axes_out: dict[str, dict] = {}
    any_source = False
    if isinstance(calibration, dict) and calibration.get("available"):
        for d in calibration.get("dimensions") or []:
            key = d.get("key")
            if not key or key in traj_keys:
                continue
            kappa = d.get("cohen_kappa")
            rho = d.get("spearman")
            n = int(d.get("n") or 0)
            any_source = True
            axes_out[key] = {
                "key": key,
                "name": d.get("name") or key,
                "source": "human",
                "kappa": kappa,
                "rho": rho,
                "n": n,
                "status": _classify_reliability(kappa, n, has_source=True, rho=rho),
            }

    return {
        "available": any_source,
        "reliable_kappa": RELIABILITY_RELIABLE_KAPPA,
        "directional_kappa": RELIABILITY_DIRECTIONAL_KAPPA,
        "rank_rho": RELIABILITY_RANK_RHO,
        "min_samples": MIN_SAMPLES,
        "axes": axes_out,
    }


def pareto_frontier(points: list[dict]) -> list[str]:
    """Config keys on the non-dominated frontier.

    ``points``: ``[{config_key, quality, cost, effort}]`` — quality higher-better,
    cost/effort lower-better. ``effort`` is token-based (SPA-77), not wall-clock.
    A point dominates another iff it is at least as good on all three and strictly
    better on one. Points without a quality value are excluded (nothing to trade
    off)."""
    valid = [p for p in points if p.get("quality") is not None]
    frontier: list[str] = []
    for p in valid:
        pq, pc, pt = p["quality"], p.get("cost") or 0.0, p.get("effort") or 0.0
        dominated = False
        for q in valid:
            if q is p:
                continue
            qq, qc, qt = q["quality"], q.get("cost") or 0.0, q.get("effort") or 0.0
            if qq >= pq and qc <= pc and qt <= pt and (qq > pq or qc < pc or qt < pt):
                dominated = True
                break
        if not dominated:
            frontier.append(p["config_key"])
    return frontier


def significance_matrix(
    samples_by_config: dict[str, dict[str, list[float]]],
) -> list[dict]:
    """Welch + Mann-Whitney for every config pair × metric with enough data.

    ``significant`` is judged on the Welch p (exact); Mann-Whitney rides along
    as the non-parametric cross-check (``approx: True``). Pairs/metrics where
    neither test can run are omitted entirely."""
    out: list[dict] = []
    keys = sorted(samples_by_config)
    metrics = sorted({m for v in samples_by_config.values() for m in v})
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a_key, b_key = keys[i], keys[j]
            for metric in metrics:
                a = samples_by_config[a_key].get(metric) or []
                b = samples_by_config[b_key].get(metric) or []
                welch = welch_t_test(a, b)
                mw = mann_whitney_u(a, b)
                if welch is None and mw is None:
                    continue
                p = welch["p"] if welch is not None else mw["p"]
                out.append(
                    {
                        "a": a_key,
                        "b": b_key,
                        "metric": metric,
                        "welch": welch,
                        "mann_whitney": mw,
                        "p": p,
                        "significant": p < SIGNIFICANCE_ALPHA,
                    }
                )
    return out


def _group_means(
    runs: list[ExperimentRun],
    records_by_task: dict,
) -> dict:
    settled = [
        r
        for r in runs
        if r.status
        in (ExperimentRunStatus.SUCCESS.value, ExperimentRunStatus.FAILED.value)
    ]
    success = [r for r in runs if r.status == ExperimentRunStatus.SUCCESS.value]
    # SPA-77: token effort (primary) + steps; cost stays (sparse) and wall-clock
    # (duration) is retained only as a caveated secondary in the UI.
    tokens = [
        t
        for r in settled
        if (t := _run_effort_tokens(records_by_task.get(r.task_id))) is not None
    ]
    steps = [
        s
        for r in settled
        if (s := _run_steps(records_by_task.get(r.task_id))) is not None
    ]
    return {
        "n_runs": len(settled),
        "success_rate": round(len(success) / len(settled), 3) if settled else None,
        "quality_mean": _mean([r.weighted_score for r in success]),
        "trajectory_mean": _mean(
            [_traj_score(records_by_task.get(r.task_id), r.trajectory_score) for r in success]
        ),
        "cost_mean": _mean([float(r.cost_usd or 0) for r in settled]),
        "duration_mean": _mean([r.duration_seconds for r in settled]),
        "tokens_mean": _mean(tokens),
        "n_tokens": len(tokens),
        "steps_mean": _mean(steps),
    }


def build_report(
    exp: Experiment,
    runs: list[ExperimentRun],
    records_by_task: dict,
    *,
    method: str = "bt",
    partial: bool = False,
    calibration: dict | None = None,
    selection: str = SELECTION_LATEST_VALID,
) -> dict:
    """Assemble the full report from pre-loaded rows (pure). ``calibration`` is the
    per-experiment judge↔human agreement (E-17) scoped to this experiment's tasks,
    computed by the async caller (this function stays pure)."""
    # A retired configuration left the matrix, so it must not produce a row —
    # otherwise the report describes a different population than serialize(),
    # /results and /export. all_attempts deliberately reaches back into the
    # retired lineage, so its rows belong there (SPA-84).
    configs = {
        c["config_key"]: c
        for c in (exp.configurations or [])
        if selection == SELECTION_ALL_ATTEMPTS or not c.get("retired_at")
    }
    labels = {k: c.get("label") or k for k, c in configs.items()}

    # Runs infrastructure decided the outcome of are not measurements of a model,
    # so they leave the population before anything is averaged (SPA-87). This was
    # free for as long as such a run died with a NULL score and every aggregate
    # skipped it; after the cap-hit fix (SPA-70) it is scored like any other
    # non-verifiable run, and a five-hour provider quota reads as a weak model.
    # Excluding evidence is a strong move, so it is also a reported one — the
    # counts below are part of the report, and they change what `success_rate`
    # means (see `summary.success_rate_basis`).
    excluded_runs = [r for r in runs if is_contaminated(r.failure_type)]
    if excluded_runs:
        runs = [r for r in runs if not is_contaminated(r.failure_type)]
    by_type: dict[str, int] = {}
    for r in excluded_runs:
        by_type[r.failure_type] = by_type.get(r.failure_type, 0) + 1
    excluded_by_config: dict[str, int] = {}
    for r in excluded_runs:
        excluded_by_config[r.config_key] = excluded_by_config.get(r.config_key, 0) + 1
    exclusions = {
        "contaminated": len(excluded_runs),
        "by_type": dict(sorted(by_type.items())),
        "by_config": [
            {
                "config_key": key,
                "label": labels.get(key, key),
                "contaminated": excluded_by_config.get(key, 0),
            }
            for key in sorted(configs)
            if excluded_by_config.get(key)
        ],
    }

    by_config: dict[str, list[ExperimentRun]] = {k: [] for k in configs}
    for r in runs:
        by_config.setdefault(r.config_key, []).append(r)

    n_terminal = sum(1 for r in runs if r.status in _SETTLED)
    success_runs = [
        r for r in runs if r.status == ExperimentRunStatus.SUCCESS.value
    ]

    # --- summary -------------------------------------------------------------
    per_config = []
    for key in sorted(by_config):
        group = by_config[key]
        stats = _group_means(group, records_by_task)
        per_config.append({"config_key": key, "label": labels.get(key, key), **stats})
    summary = {
        "total_runs": len(runs),
        # Excluded runs are gone from every number above, so the denominator has
        # changed meaning: success_rate now answers «of the runs that measured the
        # model, how many succeeded», not «if you attempt this task with this
        # model, how often does it work». Different quantity, said out loud.
        "excluded_contaminated": len(excluded_runs),
        "success_rate_basis": (
            "settled_non_contaminated" if excluded_runs else "settled"
        ),
        "success": len(success_runs),
        "failed": sum(
            1 for r in runs if r.status == ExperimentRunStatus.FAILED.value
        ),
        "skipped": sum(
            1 for r in runs if r.status == ExperimentRunStatus.SKIPPED.value
        ),
        "accumulated_cost_usd": float(exp.accumulated_cost_usd or 0),
        "budget_limit_usd": float(exp.budget_limit_usd)
        if exp.budget_limit_usd is not None
        else None,
        "per_config": per_config,
    }

    # --- effort (SPA-77): confound-controlled efficiency ----------------------
    # Wall-clock (duration_seconds) is polluted — provider throttling + sleep/wait
    # inflate it for reasons unrelated to agent skill — so "config A is more
    # efficient" from time mixes infra noise into a quality claim. Instead the
    # PRIMARY effort metric is TOKENS (deterministic), with $ as a priced secondary
    # (sparse: $0 for un-metered providers) and steps as a third. We also
    # DIFFICULTY-NORMALISE: each run's tokens ÷ the per-CASE median across configs,
    # so harder cases don't make a config look inefficient — rel_effort ≈ 1.0 means
    # "typical effort for the cases it ran", >1 heavier, <1 lighter.
    _SETTLED_OK = (ExperimentRunStatus.SUCCESS.value, ExperimentRunStatus.FAILED.value)
    case_tokens: dict[str, list[float]] = {}
    for r in runs:
        if r.status in _SETTLED_OK:
            t = _run_effort_tokens(records_by_task.get(r.task_id))
            if t is not None:
                case_tokens.setdefault(r.case_key, []).append(t)
    case_median = {ck: statistics.median(v) for ck, v in case_tokens.items() if v}
    effort_per_config = []
    any_tokens = any_cost = False
    for entry in per_config:
        key = entry["config_key"]
        ratios = [
            t / m
            for r in by_config.get(key, [])
            if r.status in _SETTLED_OK
            and (t := _run_effort_tokens(records_by_task.get(r.task_id))) is not None
            and (m := case_median.get(r.case_key))
        ]
        rel = _mean(ratios)
        entry["rel_effort"] = rel  # surface in the Summary table too
        if entry.get("tokens_mean") is not None:
            any_tokens = True
        if (entry.get("cost_mean") or 0) > 0:
            any_cost = True
        effort_per_config.append(
            {
                "config_key": key,
                "label": entry["label"],
                "tokens_mean": entry.get("tokens_mean"),
                "steps_mean": entry.get("steps_mean"),
                "cost_mean": entry.get("cost_mean"),
                "duration_mean": entry.get("duration_mean"),  # caveated secondary
                "rel_effort": rel,
                "n": entry.get("n_tokens", 0),
            }
        )
    effort = {
        "available": any_tokens,
        "cost_available": any_cost,
        "primary": "tokens",
        "per_config": effort_per_config,
    }

    # --- heatmap: configs × rubric dimensions ---------------------------------
    dim_order: list[str] = []
    dim_labels: dict[str, str] = {}
    dim_samples: dict[str, dict[str, list[float]]] = {k: {} for k in configs}
    for r in success_runs:
        rec = records_by_task.get(r.task_id)
        profile = (rec.quality_profile or {}) if rec is not None else {}
        for dim in profile.get("dimensions") or []:
            key, score = dim.get("key"), dim.get("score")
            if key is None or score is None:
                continue
            if key not in dim_order:
                dim_order.append(key)
                dim_labels[key] = dim.get("name") or key
            dim_samples.setdefault(r.config_key, {}).setdefault(key, []).append(
                float(score)
            )
    heatmap_rows = []
    for key in sorted(configs):
        cells = {}
        for dim_key in dim_order:
            vals = dim_samples.get(key, {}).get(dim_key) or []
            cells[dim_key] = {
                "mean": _mean(vals),
                "std": _std(vals),
                "n": len(vals),
            }
        # Success-only, to match the per-dimension cells above (built from
        # success_runs) AND the Summary "quality" column (_group_means → success).
        # Averaging weighted over ALL settled runs while the dimension cells use
        # success-only made the row self-contradictory (e.g. all dims 6-8 but
        # weighted 1.6 for a low-success-rate config). Reliability is shown
        # separately via success_rate.
        scores = [
            r.weighted_score
            for r in by_config[key]
            if r.status == ExperimentRunStatus.SUCCESS.value
            and r.weighted_score is not None
        ]
        heatmap_rows.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "cells": cells,
                "weighted_score": {"mean": _mean(scores), "n": len(scores)},
            }
        )
    heatmap = {
        "dimensions": dim_order,
        "dimension_labels": dim_labels,
        "rows": heatmap_rows,
    }

    # --- quality gate (E-02 critical-threshold pass-rate) per config ----------
    # Every E-02 run carries quality_profile.gate = {passed, failed_dimensions}
    # — the outcome judge's verdict on whether the result cleared its CRITICAL
    # rubric thresholds. build_report never aggregated it; surfaced here as a
    # per-config pass-rate + the dimensions that most often fail the gate. Over
    # all runs that were outcome-scored (success or failed — a failed run can
    # still carry a gate verdict), since the gate is about the result, not the
    # run's terminal status. Hidden by the frontend on verifiable benches (E-02
    # is the audited subject there, not the evaluator).
    gate_per_config = []
    any_gate = False
    for key in sorted(configs):
        n_gated = 0
        n_pass = 0
        gate_failed_dims: dict[str, int] = {}
        for r in by_config[key]:
            rec = records_by_task.get(r.task_id)
            qprof = (rec.quality_profile or {}) if rec is not None else {}
            gate = qprof.get("gate")
            if not isinstance(gate, dict):
                continue
            n_gated += 1
            any_gate = True
            if gate.get("passed"):
                n_pass += 1
            for d in gate.get("failed_dimensions") or []:
                gate_failed_dims[d] = gate_failed_dims.get(d, 0) + 1
        gate_per_config.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "n": n_gated,
                "n_pass": n_pass,
                "pass_rate": round(n_pass / n_gated, 4) if n_gated else None,
                "failed_dimensions": gate_failed_dims,
            }
        )
    quality_gate = {"available": any_gate, "per_config": gate_per_config}

    # --- trajectory heatmap: configs × E-07 axes ------------------------------
    # The process-judging analogue of the quality heatmap: per-config mean of each
    # of the six trajectory axes (efficiency / tool_selection / parameter_quality /
    # error_recovery / goal_alignment / loop_detection), privileging trajectory as
    # a first-class A/B comparison rather than a single scatter axis.
    axis_order: list[str] = []
    axis_labels: dict[str, str] = {}
    axis_samples: dict[str, dict[str, list[float]]] = {}
    for r in success_runs:
        rec = records_by_task.get(r.task_id)
        tprof = (rec.trajectory_profile or {}) if rec is not None else {}
        for ax in tprof.get("axes") or []:
            key, score = ax.get("key"), ax.get("score")
            if key is None or score is None:
                continue
            if key in _AGG_EXCLUDED_AXES:
                continue  # v11: judge loop axis retired from the heatmap/radar (SPA-76)
            if key not in axis_order:
                axis_order.append(key)
                axis_labels[key] = ax.get("name") or key
            axis_samples.setdefault(r.config_key, {}).setdefault(key, []).append(float(score))
    trajectory_heatmap_rows = []
    for key in sorted(configs):
        cells = {}
        for ax_key in axis_order:
            vals = axis_samples.get(key, {}).get(ax_key) or []
            cells[ax_key] = {"mean": _mean(vals), "std": _std(vals), "n": len(vals)}
        # Success-only, consistent with the per-axis cells (success_runs) and the
        # Summary "trajectory" column — see the weighted_score note above. v11: the
        # aggregate excludes the retired loop axis (_traj_score).
        overall = [
            _traj_score(records_by_task.get(r.task_id), r.trajectory_score)
            for r in by_config[key]
            if r.status == ExperimentRunStatus.SUCCESS.value
            and r.trajectory_score is not None
        ]
        trajectory_heatmap_rows.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "cells": cells,
                "overall_score": {"mean": _mean(overall), "n": len(overall)},
            }
        )
    trajectory_heatmap = {
        "axes": axis_order,
        "axis_labels": axis_labels,
        "rows": trajectory_heatmap_rows,
    }

    # --- loop-detection rate (E-07 judge + deterministic anchor) per config ----
    # Two loop signals side by side, over all trajectory-scored runs (success OR
    # failed — looping is often exactly what *causes* a failure):
    #   • loop_rate — the LLM judge's loop_detected (loop_detection axis < 5),
    #     scored over the budget-TRIMMED trace, holistically (reasoning + tools).
    #   • structural_loop_rate — the deterministic detector (SPA-75,
    #     trajectory_profile.loop_analysis): COUNTS repeated tool-calls over the
    #     FULL, untrimmed trace. LLM-free, reproducible — a precision-oriented
    #     structural lower bound (may under-count semantic loops).
    # The two see DIFFERENT inputs (trimmed vs full) and DIFFERENT scopes (holistic
    # vs tool-only), so their gap is part definitional/input divergence and part
    # judge error — NOT pure miscalibration. We therefore surface the DIRECTIONAL
    # split, not just a symmetric %: n_judge_only (judge flagged, counter didn't)
    # vs n_counter_only (counter found a repetition the judge missed — often in the
    # trimmed-away middle steps), plus Cohen's κ (chance-corrected) so a high
    # base-rate agreement doesn't masquerade as concordance.
    loop_per_config = []
    any_loop = False
    any_structural = False
    tot_both_loop = tot_judge_only = tot_counter_only = tot_both_clean = 0
    for key in sorted(configs):
        n_scored = 0
        n_loop = 0
        n_struct = 0
        n_struct_loop = 0
        both_loop = judge_only = counter_only = both_clean = 0
        for r in by_config[key]:
            rec = records_by_task.get(r.task_id)
            tprof = (rec.trajectory_profile or {}) if rec is not None else {}
            if tprof.get("status") != "scored":
                continue
            n_scored += 1
            any_loop = True
            llm_loop = bool(tprof.get("loop_detected"))
            if llm_loop:
                n_loop += 1
            la = tprof.get("loop_analysis")
            if isinstance(la, dict):
                any_structural = True
                n_struct += 1
                struct_loop = bool(la.get("loop_detected"))
                if struct_loop:
                    n_struct_loop += 1
                if struct_loop and llm_loop:
                    both_loop += 1
                elif llm_loop:
                    judge_only += 1
                elif struct_loop:
                    counter_only += 1
                else:
                    both_clean += 1
        tot_both_loop += both_loop
        tot_judge_only += judge_only
        tot_counter_only += counter_only
        tot_both_clean += both_clean
        n_agree = both_loop + both_clean
        loop_per_config.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "n_scored": n_scored,
                "n_loop": n_loop,
                "loop_rate": round(n_loop / n_scored, 4) if n_scored else None,
                "n_structural": n_struct,
                "n_structural_loop": n_struct_loop,
                "structural_loop_rate": round(n_struct_loop / n_struct, 4) if n_struct else None,
                "n_judge_only": judge_only,
                "n_counter_only": counter_only,
                "agreement": round(n_agree / n_struct, 4) if n_struct else None,
                "kappa": _binary_kappa(both_loop, judge_only, counter_only, both_clean),
            }
        )
    tot_struct = tot_both_loop + tot_judge_only + tot_counter_only + tot_both_clean
    loop_detection = {
        "available": any_loop,
        "structural_available": any_structural,
        "agreement": round((tot_both_loop + tot_both_clean) / tot_struct, 4) if tot_struct else None,
        "kappa": _binary_kappa(tot_both_loop, tot_judge_only, tot_counter_only, tot_both_clean),
        "n_judge_only": tot_judge_only,
        "n_counter_only": tot_counter_only,
        "n_structural": tot_struct,
        "per_config": loop_per_config,
    }
    # SPA-76: per-axis reliability gate — badge each E-07 trajectory axis by how far
    # the judge can be trusted (E-17 human κ, or the loop anchor for the loop axis).
    axis_reliability = _axis_reliability(calibration, loop_detection, axis_labels)
    outcome_axis_reliability = _outcome_axis_reliability(calibration)

    # --- cleaned-trace stats (E-06) per config --------------------------------
    # trajectory_profile.trace_stats = {original_tokens, cleaned_tokens, steps_total}
    # — the trace cleaner's output, present on every trajectory-scored run but never
    # aggregated. Per config: mean steps the agent took + how far the trace
    # compressed (cleaned/original tokens). Over trajectory-scored runs (success or
    # failed); a verbose, low-compression, many-step trace is a process smell.
    def _trace_row(group: list[ExperimentRun]) -> dict:
        steps: list[float] = []
        cleaned: list[float] = []
        original: list[float] = []
        for r in group:
            rec = records_by_task.get(r.task_id)
            tprof = (rec.trajectory_profile or {}) if rec is not None else {}
            ts = tprof.get("trace_stats") or {}
            if ts.get("steps_total") is not None:
                steps.append(float(ts["steps_total"]))
            if ts.get("cleaned_tokens") is not None:
                cleaned.append(float(ts["cleaned_tokens"]))
            if ts.get("original_tokens") is not None:
                original.append(float(ts["original_tokens"]))
        comp = (
            round(sum(cleaned) / sum(original), 4)
            if cleaned and original and sum(original) > 0
            else None
        )
        return {
            "n": len(steps),
            "steps_mean": _mean(steps),
            "cleaned_tokens_mean": _mean(cleaned),
            "original_tokens_mean": _mean(original),
            "compression": comp,
        }

    any_trace = False
    trace_per_config = []
    for key in sorted(configs):
        row = _trace_row(by_config[key])
        if row["n"] > 0:
            any_trace = True
        trace_per_config.append(
            {"config_key": key, "label": labels.get(key, key), **row}
        )
    trace_stats = {"available": any_trace, "per_config": trace_per_config}

    # --- longitudinal: quality / cost across the repetition index (E-22) -------
    # Do later repetitions of a cell drift from earlier ones (caching, rate-limit
    # degradation, non-determinism)? Aggregate every settled run by its run_index
    # (0-based repetition) across all configs/cases — a coarse reproducibility
    # trend. Quality/trajectory are success-only (unscored failures carry no score);
    # cost is over all settled runs.
    by_index: dict[int, list[ExperimentRun]] = {}
    for r in runs:
        if r.status in _SETTLED and r.run_index is not None:
            by_index.setdefault(r.run_index, []).append(r)
    longitudinal_points = []
    for idx in sorted(by_index):
        grp = by_index[idx]
        succ = [r for r in grp if r.status == ExperimentRunStatus.SUCCESS.value]
        toks = [
            t
            for r in grp
            if (t := _run_effort_tokens(records_by_task.get(r.task_id))) is not None
        ]
        longitudinal_points.append(
            {
                "run_index": idx,
                "n": len(grp),
                "quality_mean": _mean([r.weighted_score for r in succ]),
                "trajectory_mean": _mean(
                    [_traj_score(records_by_task.get(r.task_id), r.trajectory_score) for r in succ]
                ),
                "cost_mean": _mean([float(r.cost_usd or 0) for r in grp]),
                "tokens_mean": _mean(toks),  # SPA-77: token effort across repetitions
            }
        )
    longitudinal = {"available": len(longitudinal_points) > 1, "points": longitudinal_points}

    # --- human feedback (E-05) per config -------------------------------------
    # The third oracle aggregated like the judge heatmaps, BUT over ALL runs that
    # carry human feedback — not success-only. Human annotation is a post-hoc
    # verdict on the run (a human deliberately rates failures too), so dropping
    # non-success runs would discard exactly the rejects the verdict distribution
    # is about. Dimensions are SPARSE (a human may rate a subset), so missing /
    # non-numeric scores are skipped per dimension.
    h_dim_order: list[str] = []
    h_dim_labels: dict[str, str] = {}
    h_dim_samples: dict[str, dict[str, list[float]]] = {}
    h_overall: dict[str, list[float]] = {}
    h_verdicts: dict[str, dict[str, int]] = {}
    any_human = False
    # checker↔human (v12): the executable checker is the outcome ground truth on
    # verifiable benches, but it is itself imperfect — pair its pass/fail verdict
    # with the human approve/reject gold to surface where even the checker disagrees.
    ch_cells = {"pass_approve": 0, "pass_reject": 0, "fail_approve": 0, "fail_reject": 0}
    for r in runs:
        rec = records_by_task.get(r.task_id)
        hf = (getattr(rec, "human_feedback", None) or {}) if rec is not None else {}
        ev = getattr(r, "external_verdict", None)
        hv = hf.get("verdict")
        if ev is not None and hv in ("approve", "reject"):
            ch_cells[("pass" if ev else "fail") + "_" + hv] += 1
        if not hf:
            continue
        any_human = True
        run_scores: list[float] = []
        for dim in hf.get("dimensions") or []:
            key, score = dim.get("key"), dim.get("score")
            if key is None or not isinstance(score, (int, float)):
                continue
            if key not in h_dim_order:
                h_dim_order.append(key)
                h_dim_labels[key] = dim.get("name") or key
            h_dim_samples.setdefault(r.config_key, {}).setdefault(key, []).append(float(score))
            run_scores.append(float(score))
        if run_scores:
            h_overall.setdefault(r.config_key, []).append(sum(run_scores) / len(run_scores))
        verdict = hf.get("verdict") or "none"
        bucket = h_verdicts.setdefault(r.config_key, {"approve": 0, "reject": 0, "none": 0})
        bucket[verdict if verdict in bucket else "none"] += 1
    human_rows = []
    for key in sorted(configs):
        cells = {}
        for dim_key in h_dim_order:
            vals = h_dim_samples.get(key, {}).get(dim_key) or []
            cells[dim_key] = {"mean": _mean(vals), "std": _std(vals), "n": len(vals)}
        overall_vals = h_overall.get(key) or []
        verdicts = h_verdicts.get(key) or {"approve": 0, "reject": 0, "none": 0}
        human_rows.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "cells": cells,
                "overall_score": {
                    "mean": _mean(overall_vals),
                    "std": _std(overall_vals),
                    "n": len(overall_vals),
                },
                "n_rated": sum(verdicts.values()),
                "verdicts": verdicts,
            }
        )
    human_feedback = {
        "available": any_human,
        "dimensions": h_dim_order,
        "dimension_labels": h_dim_labels,
        "rows": human_rows,
    }
    # checker↔human agreement (v12): Cohen's κ + raw agreement on the verdict, where
    # checker pass≈human approve and checker fail≈human reject.
    ch_n = sum(ch_cells.values())
    checker_human = {
        "available": ch_n > 0,
        "n": ch_n,
        "kappa": _binary_kappa(
            ch_cells["pass_approve"], ch_cells["pass_reject"],
            ch_cells["fail_approve"], ch_cells["fail_reject"],
        ),
        "agreement": (ch_cells["pass_approve"] + ch_cells["fail_reject"]) / ch_n if ch_n else None,
        "cells": ch_cells,
    }

    # --- cost breakdown per config --------------------------------------------
    # Where the money went: agent execution (== QualityRecord.cost_usd, the task
    # cost; includes orchestrator overhead when enabled — it is not separately
    # metered) vs each evaluator's judge_cost_usd. Computed straight from the
    # profiles so it stays complete even though ExperimentRun.cost_usd
    # (_run_cost) only folds in E-02/E-07/E-14. Over settled runs (where cost was
    # actually incurred).
    _JUDGE_COST_KEYS = [
        ("judge_outcome", "quality_profile"),
        ("judge_trajectory", "trajectory_profile"),
        ("judge_evidence", "trajectory_evidence_profile"),
        ("judge_failure", "failure_profile"),
        ("judge_hallucination", "hallucination_profile"),
    ]

    def _cost_row(group: list[ExperimentRun]) -> dict:
        settled = [r for r in group if r.status in _SETTLED]
        parts = {"agent": 0.0, "judge_total": 0.0, "total": 0.0}
        for k, _ in _JUDGE_COST_KEYS:
            parts[k] = 0.0
        for r in settled:
            rec = records_by_task.get(r.task_id)
            agent = (
                float(getattr(rec, "cost_usd", 0) or 0)
                if rec is not None
                else float(r.cost_usd or 0)
            )
            parts["agent"] += agent
            judges = 0.0
            for k, attr in _JUDGE_COST_KEYS:
                prof = getattr(rec, attr, None) if rec is not None else None
                c = float((prof or {}).get("judge_cost_usd") or 0) if prof else 0.0
                parts[k] += c
                judges += c
            parts["judge_total"] += judges
            parts["total"] += agent + judges
        return {k: round(v, 6) for k, v in parts.items()}

    any_cost = any(float(r.cost_usd or 0) > 0 for r in runs)
    cost_per_config = [
        {"config_key": key, "label": labels.get(key, key), **_cost_row(by_config[key])}
        for key in sorted(configs)
    ]
    cost_totals = _cost_row(runs)
    cost_breakdown = {
        "available": any_cost,
        "per_config": cost_per_config,
        "totals": cost_totals,
    }

    # --- E-09 trajectory-match per config -------------------------------------
    # Match against the canonical (gold) trajectory — the strongest "judge the
    # process" signal — aggregated per config (only cases that carry a canonical
    # trajectory produce a scored match).
    trajectory_match_rows = []
    any_match = False
    for key in sorted(configs):
        scores: list[float] = []
        matched = 0
        scored = 0
        for r in by_config[key]:
            rec = records_by_task.get(r.task_id)
            tm = (rec.trajectory_match_profile or {}) if rec is not None else {}
            if tm.get("status") != "scored":
                continue
            scored += 1
            any_match = True
            if tm.get("score") is not None:
                scores.append(float(tm["score"]))
            if tm.get("matched"):
                matched += 1
        trajectory_match_rows.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "n_scored": scored,
                "match_rate": round(matched / scored, 4) if scored else None,
                "score_mean": _mean(scores),
            }
        )
    trajectory_match = {"available": any_match, "per_config": trajectory_match_rows}

    # --- external executable verdict (Toolathlon gold.external_eval) -----------
    # The executable checker's pass-rate per config — the ground-truth outcome
    # signal RQ2 compares the judges against (independent of E-02/E-07).
    external_per_config = []
    any_external = False
    for key in sorted(configs):
        evaluated = [r for r in by_config[key] if r.external_verdict is not None]
        passed = [r for r in evaluated if r.external_verdict]
        if evaluated:
            any_external = True
        external_per_config.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "n_evaluated": len(evaluated),
                "n_pass": len(passed),
                "pass_rate": round(len(passed) / len(evaluated), 4) if evaluated else None,
            }
        )
    external = {"available": any_external, "per_config": external_per_config}

    # --- RQ2 primary: does the judge separate the checker's passes from its
    # failures? Threshold-FREE (SPA-87) ---------------------------------------
    # The question is about ordering, so it is answered with ordering: the judge's
    # score distribution on runs the checker passed, the same on runs it failed,
    # and the AUC between them. `median_on_fail` IS the over-credit number — how
    # well the judge scores work that demonstrably did not work — and it needs no
    # cut-off to exist, so no cut-off can be chosen after the fact to improve it.
    # This replaces the 2×2 as the headline; the 2×2 stays below to show which
    # quadrant the disagreement sits in.
    def _judge_scores(subset: list[ExperimentRun]) -> tuple[list[float], list[float]]:
        passed = [
            float(r.weighted_score)
            for r in subset
            if r.external_verdict is True and r.weighted_score is not None
        ]
        failed = [
            float(r.weighted_score)
            for r in subset
            if r.external_verdict is False and r.weighted_score is not None
        ]
        return passed, failed

    def _discrimination_for(subset: list[ExperimentRun]) -> dict:
        passed, failed = _judge_scores(subset)
        med_pass, med_fail = _median(passed), _median(failed)
        return {
            "n_checker_pass": len(passed),
            "n_checker_fail": len(failed),
            "median_on_pass": med_pass,
            "median_on_fail": med_fail,
            "mean_on_pass": _mean(passed),
            "mean_on_fail": _mean(failed),
            # How far apart the two distributions sit, in judge points. Positive =
            # the judge scores the checker's passes higher, as it should.
            "separation": (
                round(med_pass - med_fail, 4)
                if med_pass is not None and med_fail is not None
                else None
            ),
            "auc": rank_auc(passed, failed),
            "mann_whitney": mann_whitney_u(passed, failed),
        }

    discrimination_overall = _discrimination_for(runs)
    judge_discrimination = {
        "available": (
            discrimination_overall["n_checker_pass"] > 0
            and discrimination_overall["n_checker_fail"] > 0
        ),
        "primary": True,
        "overall": discrimination_overall,
        "per_config": [
            {
                "config_key": key,
                "label": labels.get(key, key),
                **_discrimination_for(by_config[key]),
            }
            for key in sorted(configs)
        ],
    }

    # --- RQ2 illustration: executable verdict × outcome judge (2×2) -----------
    # Where the disagreement lives, at ONE pre-registered cut-off. Every run with
    # both an external verdict and a weighted score lands in a quadrant;
    # agreement = (pass∧high + fail∧low) / n, and `fail_high` is the over-credit
    # corner. Reported next to the sensitivity ladder, because a 2×2 whose cut-off
    # is chosen after the fact is not evidence — which is why the cut-off comes
    # from the experiment's frozen eval_config, not from this file.
    pre_registered = (exp.eval_config or {}).get("judge_threshold")
    try:
        judge_threshold = float(pre_registered)
        threshold_source = "pre_registered"
    except (TypeError, ValueError):
        judge_threshold = RQ2_JUDGE_THRESHOLD
        threshold_source = "default"

    def _rq2_for(subset: list[ExperimentRun], threshold: float) -> dict:
        cells = {"pass_high": 0, "pass_low": 0, "fail_high": 0, "fail_low": 0}
        n = 0
        for r in subset:
            if r.external_verdict is None or r.weighted_score is None:
                continue
            n += 1
            high = float(r.weighted_score) >= threshold
            if r.external_verdict and high:
                cells["pass_high"] += 1
            elif r.external_verdict:
                cells["pass_low"] += 1
            elif high:
                cells["fail_high"] += 1
            else:
                cells["fail_low"] += 1
        agree = cells["pass_high"] + cells["fail_low"]
        return {"n": n, "cells": cells, "agreement": round(agree / n, 4) if n else None}

    rq2_overall = _rq2_for(runs, judge_threshold)
    rq2 = {
        "available": rq2_overall["n"] > 0,
        # Not the headline any more: one cut-off's view of a question answered
        # above without one.
        "primary": False,
        "judge_threshold": judge_threshold,
        "threshold_source": threshold_source,
        "overall": rq2_overall,
        "per_config": [
            {
                "config_key": key,
                "label": labels.get(key, key),
                **_rq2_for(by_config[key], judge_threshold),
            }
            for key in sorted(configs)
        ],
        "sensitivity": [
            {
                "threshold": t,
                "pre_registered": t == judge_threshold,
                **_rq2_for(runs, t),
            }
            for t in sorted({*RQ2_SENSITIVITY_THRESHOLDS, judge_threshold})
        ],
    }

    # --- pareto ----------------------------------------------------------------
    points = []
    for entry in per_config:
        points.append(
            {
                "config_key": entry["config_key"],
                "label": entry["label"],
                "quality": entry["quality_mean"],
                "cost": entry["cost_mean"],
                "effort": entry["tokens_mean"],  # SPA-77: token effort (bubble + frontier)
                "time": entry["duration_mean"],  # caveated reference only (wall-clock)
            }
        )
    frontier = pareto_frontier(points)
    for p in points:
        p["on_frontier"] = p["config_key"] in frontier
    pareto = {"points": points, "frontier": frontier}

    # --- outcome × trajectory scatter -------------------------------------------
    # Include SETTLED runs (success + failed) that carry both scores, tagged with
    # status — the failed-but-scored runs (judge_incomplete_runs) are the canonical
    # RQ2 "good outcome despite an unclean finish" points and must be visible, not
    # silently dropped. The frontend renders them distinctly (grey crosses).
    scatter = [
        {
            "config_key": r.config_key,
            "label": labels.get(r.config_key, r.config_key),
            "case_key": r.case_key,
            "run_index": r.run_index,
            "status": r.status,
            "outcome": r.weighted_score,
            "trajectory": _traj_score(records_by_task.get(r.task_id), r.trajectory_score),
            "cost": float(r.cost_usd or 0),
            "duration": r.duration_seconds,
            "tokens": _run_effort_tokens(records_by_task.get(r.task_id)),
            "task_id": str(r.task_id) if r.task_id else None,
        }
        for r in runs
        if r.status in _SETTLED
        and r.weighted_score is not None
        and r.trajectory_score is not None
    ]

    # --- pairwise leaderboard (derived from pointwise scores, E-19) -------------
    scored = [
        {"case": r.case_key, "player": r.config_key, "score": r.weighted_score}
        for r in success_runs
        if r.weighted_score is not None
    ]
    matches, match_meta = build_matches(scored, subject="config")
    ranking = rank(matches, method=method)
    for player in ranking.get("players") or []:
        player["label"] = labels.get(player["player"], player["player"])
    leaderboard = {
        "source": "derived_pointwise",
        "derivation": match_meta,
        **ranking,
    }

    # --- significance ------------------------------------------------------------
    samples: dict[str, dict[str, list[float]]] = {}
    for key in configs:
        group_success = [
            r
            for r in by_config[key]
            if r.status == ExperimentRunStatus.SUCCESS.value
        ]
        cfg_samples: dict[str, list[float]] = {}
        weighted = [
            r.weighted_score for r in group_success if r.weighted_score is not None
        ]
        if weighted:
            cfg_samples["weighted_score"] = weighted
        trajectory = [
            _traj_score(records_by_task.get(r.task_id), r.trajectory_score)
            for r in group_success
            if r.trajectory_score is not None
        ]
        if trajectory:
            cfg_samples["trajectory_score"] = trajectory
        for dim_key, vals in dim_samples.get(key, {}).items():
            if vals:
                cfg_samples[f"dim:{dim_key}"] = vals
        if cfg_samples:
            samples[key] = cfg_samples
    significance = significance_matrix(samples)

    # --- failure modes -------------------------------------------------------------
    # E-14 detects failure CLASSES (tool_confusion / loop / premature_stop / …),
    # each with a free-text ``reason`` and confidence. The report counted classes
    # but threw the reasons away — so "loop ×3" gave no clue WHAT looped. Keep the
    # class counts (back-compat) and add ``class_reasons``: up to 3 representative
    # reasons per class, highest-confidence first, deduped by text.
    _REASONS_PER_CLASS = 3
    failure_per_config = []
    for key in sorted(configs):
        group = by_config[key]
        classes: dict[str, int] = {}
        reasons: dict[str, list[dict]] = {}
        for r in group:
            rec = records_by_task.get(r.task_id)
            profile = (rec.failure_profile or {}) if rec is not None else {}
            for failure in profile.get("failures") or []:
                cls = failure.get("class")
                if not cls:
                    continue
                classes[cls] = classes.get(cls, 0) + 1
                reason = (failure.get("reason") or "").strip()
                if reason:
                    reasons.setdefault(cls, []).append(
                        {"reason": reason, "confidence": failure.get("confidence")}
                    )
        class_reasons: dict[str, list[dict]] = {}
        for cls, items in reasons.items():
            seen: dict[str, dict] = {}
            for it in sorted(
                items, key=lambda x: x.get("confidence") or 0.0, reverse=True
            ):
                seen.setdefault(it["reason"], it)
            class_reasons[cls] = list(seen.values())[:_REASONS_PER_CLASS]
        failure_per_config.append(
            {
                "config_key": key,
                "label": labels.get(key, key),
                "statuses": {
                    status: sum(1 for r in group if r.status == status)
                    for status in sorted({r.status for r in group})
                },
                "classes": classes,
                "class_reasons": class_reasons,
            }
        )
    failure_modes = {"per_config": failure_per_config}

    # --- orchestrator on/off comparison ----------------------------------------------
    on_keys = [k for k, c in configs.items() if c.get("orchestrator")]
    off_keys = [k for k, c in configs.items() if not c.get("orchestrator")]

    def _side(keys: list[str]) -> Optional[dict]:
        group = [r for k in keys for r in by_config.get(k, [])]
        if not group:
            return None
        return {"configs": sorted(keys), **_group_means(group, records_by_task)}

    on_side, off_side = _side(on_keys), _side(off_keys)
    orchestrator: dict = {"on": on_side, "off": off_side, "delta": None}
    if on_side and off_side:
        delta = {}
        for metric in ("quality_mean", "trajectory_mean", "cost_mean",
                       "tokens_mean", "duration_mean", "success_rate"):
            a, b = on_side.get(metric), off_side.get(metric)
            delta[metric] = round(a - b, 4) if (a is not None and b is not None) else None
        orchestrator["delta"] = delta  # on minus off

    return {
        "schema_version": SCHEMA_VERSION,
        # What this report was computed FROM (SPA-84). The cache is served only
        # when both still match the experiment, which is what stops a mutated
        # experiment returning its pre-mutation numbers. The fingerprint is
        # recomputed here rather than read from the stored column: the column is
        # only ever written by a mutation, so comparing it to itself would echo
        # the revision counter instead of measuring the inputs.
        "input_revision": exp.revision,
        "input_fingerprint": experiment_input_fingerprint(exp),
        "selection": selection,
        "generated_at": datetime.utcnow().isoformat(),
        "partial": partial,
        "n_terminal_runs": n_terminal,
        "exclusions": exclusions,
        "summary": summary,
        "effort": effort,
        "heatmap": heatmap,
        "quality_gate": quality_gate,
        "trajectory_heatmap": trajectory_heatmap,
        "loop_detection": loop_detection,
        "axis_reliability": axis_reliability,
        "outcome_axis_reliability": outcome_axis_reliability,
        "trace_stats": trace_stats,
        "longitudinal": longitudinal,
        "human_feedback": human_feedback,
        "cost_breakdown": cost_breakdown,
        "trajectory_match": trajectory_match,
        "external": external,
        "judge_discrimination": judge_discrimination,
        "rq2": rq2,
        "pareto": pareto,
        "scatter": scatter,
        "leaderboard": leaderboard,
        "significance": significance,
        "failure_modes": failure_modes,
        "orchestrator": orchestrator,
        "judge_calibration": calibration,
        "checker_human": checker_human,
    }


async def select_runs(
    db: AsyncSession, exp: Experiment, *, selection: str = SELECTION_LATEST_VALID
) -> list[ExperimentRun]:
    """Resolve which executions of each cell the report should count (SPA-84).

    ``ExperimentRun`` holds a cell's current state; superseded executions live in
    ``experiment_attempts``. Attempts are returned as detached ``ExperimentRun``
    instances rather than a second shape, so every consumer downstream stays
    unchanged — they are never added to the session.
    """
    if selection not in SELECTION_POLICIES:
        raise ValueError(f"unknown selection policy: {selection!r}")

    stmt = select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id)
    if selection != SELECTION_ALL_ATTEMPTS:
        # A retired configuration left the matrix; only an explicit request for
        # the full history brings it back.
        stmt = stmt.where(LIVE_CELL)
    current = list(
        (
            await db.execute(
                stmt.order_by(
                    ExperimentRun.config_key,
                    ExperimentRun.case_key,
                    ExperimentRun.run_index,
                )
            )
        )
        .scalars()
        .all()
    )
    if selection == SELECTION_LATEST_VALID or not current:
        return current

    by_id = {r.id: r for r in current}
    attempts = (
        (
            await db.execute(
                select(ExperimentAttempt)
                .where(ExperimentAttempt.experiment_run_id.in_(list(by_id)))
                .order_by(ExperimentAttempt.attempt_index)
            )
        )
        .scalars()
        .all()
    )

    def _as_run(cell: ExperimentRun, att: ExperimentAttempt) -> ExperimentRun:
        return ExperimentRun(
            experiment_id=cell.experiment_id,
            config_key=cell.config_key,
            case_key=cell.case_key,
            run_index=cell.run_index,
            task_id=att.task_id,
            status=att.status,
            cost_usd=att.cost_usd,
            weighted_score=att.weighted_score,
            trajectory_score=att.trajectory_score,
            duration_seconds=att.duration_seconds,
            failure_type=att.failure_type,
            external_verdict=att.external_verdict,
            launch_time=att.launch_time,
            attempt_count=att.attempt_index,
            completed_at=att.completed_at,
        )

    if selection == SELECTION_ALL_ATTEMPTS:
        # Retiring a config snapshots each cell WITHOUT clearing it, so that
        # ledger row describes the execution still sitting on the live row —
        # which is already in ``current``. Counting both reports one execution
        # twice. But a cell that was retried and then retired holds no execution
        # at all any more (the retry reset it to pending without advancing the
        # counter), so there the ledger row is the ONLY copy and dropping it
        # would lose the execution entirely.
        def _live_holds_its_execution(cell: ExperimentRun) -> bool:
            return cell.task_id is not None or cell.status != ExperimentRunStatus.PENDING.value

        superseded = [
            a
            for a in attempts
            if a.attempt_index < (by_id[a.experiment_run_id].attempt_count or 0)
            or not _live_holds_its_execution(by_id[a.experiment_run_id])
        ]
        return current + [_as_run(by_id[a.experiment_run_id], a) for a in superseded]

    # first_attempt: a cell that was retried is represented by attempt 1; a cell
    # that ran once is already its own first attempt.
    firsts = {}
    for a in attempts:
        firsts.setdefault(a.experiment_run_id, a)
    return [
        _as_run(cell, firsts[cell.id]) if cell.id in firsts else cell
        for cell in current
    ]


async def config_drift(db: AsyncSession, exp: Experiment) -> list[dict]:
    """Configurations whose frozen resolution no longer matches reality (SPA-84).

    Pinning what a config resolved to is only worth doing if somebody is told
    when it stops being true. A template edited mid-experiment, or a model row
    repointed at another vendor, changes what a condition means at an unchanged
    fingerprint — this is what makes that visible.
    """
    pinned = [c for c in live_configs(exp) if c.get("resolved")]
    if not pinned:
        return []

    # What the runs ACTUALLY executed under, recorded at spawn. This is the
    # authoritative half: the pin describes intent at start, while the engine
    # re-resolves the template, the model and the tool set at every spawn.
    # Current cells plus the ledger — a cell retried under a changed template
    # holds only the newer condition, so the earlier attempt is the evidence.
    #
    # Grouped by (config_key, case_key), NOT by config alone: the resolved MCP
    # server set is derived from the CASE, so one unchanged configuration
    # legitimately runs different tool sets across cases. Comparing those would
    # declare every Toolathlon experiment split. What must hold is narrower and
    # actually meaningful: the same configuration on the same case is the same
    # condition, however many runs and retries it took.
    rows = (
        await db.execute(
            select(
                ExperimentRun.config_key,
                ExperimentRun.case_key,
                ExperimentRun.condition_fingerprint,
                ExperimentRun.core_condition_fingerprint,
            ).where(
                ExperimentRun.experiment_id == exp.id,
                LIVE_CELL,
                ExperimentRun.condition_fingerprint.isnot(None),
            )
        )
    ).all()
    attempt_rows = (
        await db.execute(
            select(
                ExperimentRun.config_key,
                ExperimentRun.case_key,
                ExperimentAttempt.condition_fingerprint,
                ExperimentAttempt.core_condition_fingerprint,
            )
            .join(ExperimentAttempt, ExperimentAttempt.experiment_run_id == ExperimentRun.id)
            .where(
                ExperimentRun.experiment_id == exp.id,
                LIVE_CELL,
                ExperimentAttempt.condition_fingerprint.isnot(None),
            )
        )
    ).all()
    per_cell: dict[tuple[str, str], set[str]] = {}
    per_config: dict[str, set[str]] = {}
    for config_key, case_key, full, core in [*rows, *attempt_rows]:
        per_cell.setdefault((config_key, case_key), set()).add(full)
        if core:
            per_config.setdefault(config_key, set()).add(core)
    # Within a case: the full hash, which additionally covers the tool set.
    split_cases: dict[str, dict[str, list[str]]] = {}
    for (config_key, case_key), fingerprints in per_cell.items():
        if len(fingerprints) > 1:
            split_cases.setdefault(config_key, {})[case_key] = sorted(fingerprints)
    # Across the whole config: the case-independent hash. This is the one that
    # catches an edit made between two CASES — with the default single run per
    # cell, a per-case comparison has exactly one value and can never disagree.
    split_configs = {
        config_key: sorted(cores)
        for config_key, cores in per_config.items()
        if len(cores) > 1
    }

    images = _agent_image_ids()
    out: list[dict] = []
    for cfg in pinned:
        was = cfg["resolved"]
        now = await _resolve_config_state(db, cfg, images)
        changed = {
            field: {"pinned": was.get(field), "current": now.get(field)}
            for field in (
                "model_api_name",
                "template_content_sha256",
                "provider_name",
            )
            # A field absent from the pin predates it; only a real change counts.
            if field in was and was.get(field) != now.get(field)
        }
        pinned_images = was.get("agent_images") or {}
        now_images = now.get("agent_images") or {}
        for name, image_id in pinned_images.items():
            # An unavailable docker socket reports None; that is missing
            # evidence, not evidence of a rebuild.
            if image_id and now_images.get(name) and now_images[name] != image_id:
                changed[f"agent_image:{name}"] = {
                    "pinned": image_id,
                    "current": now_images[name],
                }
        # The stronger signal: not "the pin is out of date" but "runs of the same
        # cell did not all execute under the same thing". The pin is compared
        # against NOW, so it misses an edit reverted before the report and cannot
        # say which runs were affected. The per-run fingerprints can.
        split = split_cases.get(cfg.get("config_key")) or {}
        core_split = split_configs.get(cfg.get("config_key")) or []
        if changed or split or core_split:
            entry = {
                "config_key": cfg.get("config_key"),
                "label": cfg.get("label"),
                "resolved_at": was.get("resolved_at"),
                "changed": changed,
            }
            if split:
                entry["split_cases"] = split
            if core_split:
                entry["core_conditions"] = core_split
            out.append(entry)
    return out


async def compute_report(
    db: AsyncSession,
    exp: Experiment,
    *,
    method: str = "bt",
    partial: bool = False,
    selection: str = SELECTION_LATEST_VALID,
) -> dict:
    """Load the experiment's runs + records and assemble the report."""
    runs = await select_runs(db, exp, selection=selection)
    task_ids = [r.task_id for r in runs if r.task_id]
    records_by_task: dict[uuid.UUID, QualityRecord] = {}
    if task_ids:
        rows = (
            await db.execute(
                select(QualityRecord).where(QualityRecord.task_id.in_(task_ids))
            )
        ).scalars().all()
        records_by_task = {rec.task_id: rec for rec in rows}

    # Per-experiment judge↔human calibration (E-17): scope the workspace calibration
    # to THIS experiment's tasks, so the report shows agreement on the runs the user
    # actually annotated here — not the workspace-global badge (which mixes prior
    # experiments). Empty until some of these runs carry human feedback.
    calibration = None
    if task_ids:
        from app.quality.judge_calibration import (
            DEFAULT_MIN_KAPPA,
            _compute_report,
            collect_judge_human_pairs,
        )
        from app.api.settings import get_setting

        pairs = await collect_judge_human_pairs(
            db, exp.workspace_id, task_ids=task_ids
        )
        threshold = await get_setting(db, "judge_calibration_min_kappa", DEFAULT_MIN_KAPPA)
        calibration = _compute_report(pairs, threshold_kappa=float(threshold))
        calibration["available"] = calibration.get("sample_size", 0) > 0

    report = build_report(
        exp, runs, records_by_task, method=method, partial=partial,
        calibration=calibration, selection=selection,
    )
    report["config_drift"] = await config_drift(db, exp)
    return report
