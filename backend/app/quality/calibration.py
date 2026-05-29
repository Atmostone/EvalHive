"""Confidence Calibration (E-16).

A model can be confident in a wrong answer and unsure about a right one. Without
knowing how well a model's stated confidence tracks its actual correctness you
cannot delegate agent → agent — a Coordinator has no basis for trusting a sub-
agent's claim. E-16 measures calibration over finished runs.

For each task it records the pair **(predicted_confidence, actual_correctness)**:

- **actual_correctness** is read from the E-02 outcome profile via
  :func:`app.quality.capability._outcome_from_profile` (a scored ``reference``
  dimension, else ``weighted_score ≥ threshold``). When neither exists the run is
  skipped — there is no ground truth to calibrate against.
- **predicted_confidence** does not exist anywhere in the system, so E-16 elicits
  it with a single *post-hoc self-probe*: the model that ran the task re-reads the
  task + its own answer + work trace (but **not** the grader's verdict) and reports
  ``P(answer is correct) ∈ [0,1]``. The probe runs on the task's own model
  (resolved by ``model_used``, falling back to the workspace judge) so the
  per-model breakdown reflects each model's calibration of *itself*.

The per-task slot (``quality_records.calibration_profile``) stores just the raw
pair plus its Brier term; the headline calibration metrics — **ECE**, **Brier
score** and the **reliability diagram** — are inherently population-level and are
computed at aggregate time (:func:`aggregate_calibration`), with a per-model
recommendation ("model X overestimates itself in the 70–80% confidence zone").

Consistent with the rest of ``app.quality``: at most ONE LLM call per task, input
bounded by ``calibration_judge_max_input_tokens``, the E-02 profile is reused
(run once only when missing), and the probe never raises — an LLM/parse failure
becomes ``status: "error"`` instead of an exception. The calibration metric lives
in its own orthogonal slot; it is not a dimension of the E-02 rubric engine.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import ResolvedModel
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.plugins.llm import get_llm_provider
from app.quality.capability import DEFAULT_OUTCOME_THRESHOLD, _outcome_from_profile
from app.quality.judge import _judge_cost, _resolve_judge_model, _tokens_from_response
from app.quality.trace_cleaner import _count_tokens, build_cleaned_trace
from app.quality.trajectory import _fit_trace_to_budget
from app.utils.events import log_event

logger = logging.getLogger(__name__)

CALIBRATION_SCHEMA_VERSION = 1
# Default cap on the probe's input tokens per task; overridable via the
# `calibration_judge_max_input_tokens` setting (cost cap).
DEFAULT_MAX_INPUT_TOKENS = 12000
# Number of equal-width confidence buckets for ECE / the reliability diagram.
DEFAULT_BINS = 10

_RESULT_CAP = 8000  # chars of the deliverable fed to the probe
_REASON_CAP = 500


def _clamp_confidence(raw) -> float:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(c, 3)))


# ---------------------------------------------------------------------------
# Model resolution — the probe runs on the model that did the task
# ---------------------------------------------------------------------------
async def _resolve_doer_model(db: AsyncSession, workspace_id, model_used: str | None):
    """Resolve the model that ran the task, by ``api_name`` within the workspace's
    providers, so the confidence probe reflects that model's own self-assessment.

    Falls back to the workspace judge model (``quality_judge`` → ``orchestrator``)
    when the task's model can't be found (e.g. it was deleted, or ``model_used`` is
    blank). Returns ``None`` when nothing is configured."""
    if model_used:
        try:
            row = (
                await db.execute(
                    select(LLMModel)
                    .join(Provider, LLMModel.provider_id == Provider.id)
                    .where(
                        Provider.workspace_id == workspace_id,
                        LLMModel.api_name == model_used,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is not None:
                provider = await db.get(Provider, row.provider_id)
                if provider is not None:
                    return ResolvedModel(provider=provider, model=row)
        except Exception as e:  # noqa: BLE001 — fall back to the judge model
            logger.debug(f"doer-model resolve failed for '{model_used}': {e}")
    return await _resolve_judge_model(db, workspace_id)


# ---------------------------------------------------------------------------
# Self-probe LLM call
# ---------------------------------------------------------------------------
ASSESS_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "assess_confidence",
            "description": (
                "Report your calibrated probability that the answer correctly and "
                "completely solves the task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "Probability in [0,1] that the answer is correct and "
                            "complete."
                        ),
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "One or two sentences justifying the confidence.",
                    },
                },
                "required": ["confidence", "reasoning"],
            },
        },
    }
]


def _build_probe_input(task: Task, cleaned_trace: dict, max_input_tokens: int) -> tuple[str, bool]:
    """Assemble the probe's text input: the task, the agent's own answer and its
    work trace — but NOT the grader's verdict. Returns (text, input_capped)."""
    answer = (task.result_summary or "").strip()[:_RESULT_CAP]
    head_parts = [
        f"TASK TITLE: {task.title}",
        f"TASK DESCRIPTION:\n{task.description or '(none)'}",
        f"\nYOUR ANSWER (the result you produced):\n{answer or '(empty)'}",
        "\nYOUR WORK TRACE (for recall):",
    ]
    head_block = "\n".join(head_parts) + "\n"

    if cleaned_trace.get("steps") or []:
        remaining = max(200, max_input_tokens - _count_tokens(head_block))
        trace_text, input_capped = _fit_trace_to_budget(cleaned_trace, remaining)
    else:
        trace_text, input_capped = "(no recorded trajectory steps)", False
    return head_block + trace_text, input_capped


async def _probe_confidence(
    task: Task, cleaned_trace: dict, probe_llm, max_input_tokens: int
) -> dict:
    """Run the single self-assessment LLM call. Never raises — failures return a
    dict with ``status: "error"``."""
    serialized, input_capped = _build_probe_input(task, cleaned_trace, max_input_tokens)
    messages = [
        {
            "role": "system",
            "content": (
                "You are the AI agent that produced the ANSWER below. Honestly and "
                "calibratedly estimate the probability (0 to 1) that your answer "
                "correctly and completely solves the task. You do NOT get to see any "
                "grader's verdict — judge only from the task and your own work. Do "
                "not default to high confidence; reserve values above 0.9 for "
                "answers you are almost certain are correct, and use low values when "
                "the task is ambiguous or your work is thin. Use the "
                "assess_confidence tool."
            ),
        },
        {"role": "user", "content": serialized},
    ]
    try:
        resp = await get_llm_provider().acompletion(
            model=probe_llm.model.api_name,
            messages=messages,
            tools=ASSESS_TOOL,
            tool_choice={"type": "function", "function": {"name": "assess_confidence"}},
            api_key=probe_llm.provider.api_key,
            api_base=probe_llm.provider.endpoint,
        )
        choice = resp.choices[0].message
        args = json.loads(choice.tool_calls[0].function.arguments)
        in_tok, out_tok = _tokens_from_response(resp)
        return {
            "status": "scored",
            "confidence": _clamp_confidence((args or {}).get("confidence")),
            "reasoning": str((args or {}).get("reasoning") or "")[:_REASON_CAP],
            "judge_input_tokens": in_tok,
            "judge_output_tokens": out_tok,
            "judge_cost_usd": _judge_cost(probe_llm, in_tok, out_tok),
            "input_capped": input_capped,
        }
    except Exception as e:  # noqa: BLE001 — the probe must not crash the request
        logger.warning(f"calibration probe failed for task {task.id}: {e}")
        return {"status": "error", "error": str(e)[:300], "input_capped": input_capped}


async def evaluate_task_calibration(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Elicit ``task``'s confidence and pair it with its E-02 correctness.

    Returns the profile dict, or ``None`` when skipped (no model resolvable, no
    deliverable text, or no correctness signal in the E-02 profile). The E-02
    profile is reused as-is and only run once when missing. A failed probe is
    persisted with ``status: "error"`` — not skipped. Overwrites on re-run.
    """
    probe_llm = await _resolve_doer_model(db, task.workspace_id, task.model_used)
    if probe_llm is None:
        logger.info(f"calibration eval skipped — no model resolvable for task {task.id}")
        return None

    result_text = (task.result_summary or "").strip()
    if not result_text:
        logger.info(f"calibration eval skipped — no result deliverable for task {task.id}")
        return None

    from app.api.settings import get_setting

    threshold = float(
        await get_setting(db, "calibration_outcome_threshold", DEFAULT_OUTCOME_THRESHOLD)
        or DEFAULT_OUTCOME_THRESHOLD
    )
    raw_cap = await get_setting(
        db, "calibration_judge_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS
    )
    try:
        max_input_tokens = int(raw_cap)
    except (TypeError, ValueError):
        max_input_tokens = DEFAULT_MAX_INPUT_TOKENS

    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)

    # Actual correctness from E-02 (run it once if the profile is missing).
    profile_existed = record is not None and record.quality_profile is not None
    outcome_profile = record.quality_profile if record is not None else None
    if outcome_profile is None:
        from app.quality.judge import evaluate_task_quality

        outcome_profile = await evaluate_task_quality(db, task, commit=False)
    correct, signal, score = _outcome_from_profile(outcome_profile, threshold)
    if signal == "none":
        logger.info(f"calibration eval skipped — no correctness signal for task {task.id}")
        return None

    cleaned_trace = await build_cleaned_trace(db, task)
    res = await _probe_confidence(task, cleaned_trace, probe_llm, max_input_tokens)

    status = res.get("status", "scored")
    errors: list[dict] = []
    confidence = None
    brier_term = None
    if status == "error":
        errors = [{"error": res.get("error")}]
    else:
        confidence = res.get("confidence")
        actual = 1.0 if correct else 0.0
        brier_term = round((confidence - actual) ** 2, 4)

    stats = cleaned_trace.get("stats") or {}
    profile = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "status": status,
        "predicted_confidence": confidence,
        "actual_correct": bool(correct),
        "outcome_signal": signal,
        "outcome_score": round(score, 2) if score is not None else None,
        "outcome_threshold": threshold,
        "brier_term": brier_term,
        "confidence_source": "self_probe",
        "probe_model": probe_llm.model.api_name,
        "reasoning": res.get("reasoning", ""),
        "judge_input_tokens": res.get("judge_input_tokens", 0),
        "judge_output_tokens": res.get("judge_output_tokens", 0),
        "judge_cost_usd": res.get("judge_cost_usd", 0.0),
        "input_capped": res.get("input_capped", False),
        "used_outcome_profile": profile_existed,
        "trace_stats": {
            "original_tokens": stats.get("original_tokens"),
            "cleaned_tokens": stats.get("cleaned_tokens"),
            "steps_total": stats.get("steps_total"),
        },
        "evaluated_at": datetime.utcnow().isoformat(),
        "errors": errors,
    }

    if record is not None:
        record.calibration_profile = profile

    await log_event(
        db,
        "calibration_evaluated",
        "system",
        {
            "status": status,
            "predicted_confidence": confidence,
            "actual_correct": bool(correct),
            "brier_term": brier_term,
            "probe_model": probe_llm.model.api_name,
            "judge_cost_usd": res.get("judge_cost_usd", 0.0),
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return profile


# ---------------------------------------------------------------------------
# Aggregation — ECE / Brier / reliability diagram
# ---------------------------------------------------------------------------
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _calibration_metrics(pairs: list[tuple[float, bool]], bins: int) -> dict:
    """Compute ECE, Brier score and the reliability diagram for a set of
    (predicted_confidence, actual_correct) pairs.

    - ``brier`` = mean of ``(confidence - actual)^2``.
    - ``reliability`` = per-bucket ``{lo, hi, count, avg_confidence, accuracy}``
      over ``bins`` equal-width confidence buckets in [0,1].
    - ``ece`` = Σ over non-empty buckets of ``(count/total) * |avg_conf - accuracy|``.
    - ``overconfidence`` = ``avg_confidence - accuracy`` (positive ⇒ overconfident).
    """
    n = len(pairs)
    width = 1.0 / bins
    reliability = [
        {"lo": round(i * width, 4), "hi": round((i + 1) * width, 4),
         "count": 0, "avg_confidence": None, "accuracy": None}
        for i in range(bins)
    ]
    if n == 0:
        return {
            "count": 0, "ece": None, "brier": None, "accuracy": None,
            "avg_confidence": None, "overconfidence": None, "reliability": reliability,
        }

    confs = [max(0.0, min(1.0, float(c))) for c, _ in pairs]
    acts = [1.0 if a else 0.0 for _, a in pairs]
    accuracy = _mean(acts)
    avg_conf = _mean(confs)
    brier = _mean([(c - a) ** 2 for c, a in zip(confs, acts)])

    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for c, a in zip(confs, acts):
        idx = min(bins - 1, int(c * bins))
        buckets[idx].append((c, a))

    ece = 0.0
    for i, bucket in enumerate(buckets):
        cnt = len(bucket)
        if not cnt:
            continue
        b_conf = _mean([c for c, _ in bucket])
        b_acc = _mean([a for _, a in bucket])
        ece += (cnt / n) * abs(b_conf - b_acc)
        reliability[i].update(
            {"count": cnt, "avg_confidence": round(b_conf, 4), "accuracy": round(b_acc, 4)}
        )

    return {
        "count": n,
        "ece": round(ece, 4),
        "brier": round(brier, 4),
        "accuracy": round(accuracy, 4),
        "avg_confidence": round(avg_conf, 4),
        "overconfidence": round(avg_conf - accuracy, 4),
        "reliability": reliability,
    }


def _recommendation_for(model: str, metrics: dict, *, min_count: int = 3) -> str | None:
    """Human-readable calibration verdict for ``model``: the confidence bucket with
    the largest signed gap between stated confidence and actual accuracy. Returns
    ``None`` when there is not enough data to say anything."""
    if not metrics.get("count"):
        return None
    best = None  # (gap, bucket)
    for b in metrics.get("reliability") or []:
        if (b.get("count") or 0) < min_count or b.get("avg_confidence") is None:
            continue
        gap = b["avg_confidence"] - b["accuracy"]
        if best is None or abs(gap) > abs(best[0]):
            best = (gap, b)
    if best is None:
        return None
    gap, b = best
    zone = f"{b['lo']:.0%}–{b['hi']:.0%}"
    detail = f"(avg confidence {b['avg_confidence']:.0%} vs accuracy {b['accuracy']:.0%})"
    if gap > 0.1:
        return f"Model {model} overestimates itself in the {zone} confidence zone {detail}."
    if gap < -0.1:
        return f"Model {model} underestimates itself in the {zone} confidence zone {detail}."
    return f"Model {model} is reasonably calibrated (largest gap in {zone}, {abs(gap):.0%})."


async def aggregate_calibration(
    db: AsyncSession,
    *,
    workspace_id,
    model_used: str | None = None,
    template_id=None,
    suite: str | None = None,
    bins: int = DEFAULT_BINS,
) -> dict:
    """Aggregate calibration profiles across a workspace into ECE / Brier /
    reliability-diagram metrics, overall and broken down by model and template.

    The per-model breakdown is the "compare models by calibration" view; each gets
    a plain-language recommendation. Filters narrow the population; ``suite``
    restricts to one Benchmark Case Store suite.
    """
    try:
        bins = max(2, min(20, int(bins)))
    except (TypeError, ValueError):
        bins = DEFAULT_BINS

    q = select(QualityRecord).where(
        QualityRecord.workspace_id == workspace_id,
        QualityRecord.calibration_profile.isnot(None),
    )
    if model_used:
        q = q.where(QualityRecord.model_used == model_used)
    if template_id is not None:
        q = q.where(QualityRecord.template_id == template_id)
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    rows = (await db.execute(q)).scalars().all()

    overall: list[tuple[float, bool]] = []
    by_model_pairs: dict[str, list[tuple[float, bool]]] = {}
    by_template_pairs: dict[str, list[tuple[float, bool]]] = {}

    for r in rows:
        prof = r.calibration_profile or {}
        if prof.get("status") != "scored":
            continue
        conf = prof.get("predicted_confidence")
        if conf is None:
            continue
        pair = (float(conf), bool(prof.get("actual_correct")))
        model = r.model_used or prof.get("probe_model") or "unknown"
        tmpl = r.template_name or (str(r.template_id) if r.template_id else "unknown")
        overall.append(pair)
        by_model_pairs.setdefault(model, []).append(pair)
        by_template_pairs.setdefault(tmpl, []).append(pair)

    by_model = {m: _calibration_metrics(p, bins) for m, p in sorted(by_model_pairs.items())}
    recommendations = [
        rec for m, metrics in by_model.items() if (rec := _recommendation_for(m, metrics))
    ]

    return {
        "workspace_id": str(workspace_id),
        "filters": {
            "model_used": model_used,
            "template_id": str(template_id) if template_id else None,
            "suite": suite,
        },
        "bins": bins,
        "overall": _calibration_metrics(overall, bins),
        "by_model": by_model,
        "by_template": {t: _calibration_metrics(p, bins) for t, p in sorted(by_template_pairs.items())},
        "recommendations": recommendations,
    }
