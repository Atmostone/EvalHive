"""Failure Mode Classifier (E-14).

"Pass/fail" is too coarse. For research signal — "model X suffers tool confusion
in 23% of runs" (§3.4 F1) — we need to classify the *type* of failure on top of
the trajectory judge (E-07). This module takes the E-06 cleaned trace plus, when
available, the E-02 outcome profile and the E-07 trajectory profile, and in a
SINGLE LLM call emits a **multi-label** set of failure classes, each with a
confidence and a brief reason, plus a one-line summary. The profile is written
to ``quality_records.failure_profile`` (next to the other quality slots).

Six base classes (extensible via :data:`FAILURE_CLASSES`):

- ``tool_confusion`` — picked a similar but wrong tool;
- ``parameter_blind`` — right tool, wrong/garbled parameters;
- ``loop`` — got stuck repeating itself;
- ``premature_stop`` — stopped before the task was complete;
- ``hallucinated_tool_result`` — fabricated a result instead of calling a tool;
- ``ignored_error`` — ignored a tool error and carried on.

The classifier runs on every terminal task; a clean run yields ``failures: []``,
so it also surfaces "succeeded with a defective process". Grouping by model /
template (:func:`aggregate_failure_modes`) gives the distribution of failure
types per (model, template) — the research deliverable feeding E-24 and SPA-30.

Consistent with the rest of ``app.quality``: model selection reuses E-02's
resolver (`quality_judge` → `orchestrator`), the input is bounded by the
configurable ``failure_judge_max_input_tokens`` setting, existing E-02/E-07
profiles are read as-is (never re-run), and the judge never raises — an LLM or
parse failure becomes ``status: "error"`` instead of an exception.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.plugins.llm import get_llm_provider
from app.quality.capability import DEFAULT_OUTCOME_THRESHOLD, _outcome_from_profile
from app.quality.judge import _judge_cost, _resolve_judge_model, _tokens_from_response
from app.quality.trace_cleaner import _count_tokens, build_cleaned_trace
from app.quality.trajectory import (
    AXES as TRAJECTORY_AXES,
    _fit_trace_to_budget,
)
from app.utils.events import log_event

logger = logging.getLogger(__name__)

FAILURE_SCHEMA_VERSION = 1
# Default cap on the judge's input tokens per task; overridable via the
# `failure_judge_max_input_tokens` setting (acceptance: cost cap).
DEFAULT_MAX_INPUT_TOKENS = 12000
_REASON_CAP = 500
_SUMMARY_CAP = 1000

# The 6 base failure classes (§3.4 F1): (key, what it means). Extensible — add a
# row here and it flows through the tool schema, validation and aggregation.
FAILURE_CLASSES: list[tuple[str, str]] = [
    ("tool_confusion",
     "picked a similar but wrong tool for the step"),
    ("parameter_blind",
     "called the right tool but with wrong, missing or garbled parameters"),
    ("loop",
     "got stuck repeating the same step/cycle without progress"),
    ("premature_stop",
     "stopped before the task was actually complete"),
    ("hallucinated_tool_result",
     "fabricated a tool result instead of actually calling the tool"),
    ("ignored_error",
     "a tool returned an error and the agent ignored it and carried on"),
]
FAILURE_CLASS_KEYS = [k for k, _ in FAILURE_CLASSES]


FAILURE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "classify_failures",
            "description": (
                "Classify the agent trajectory's failure modes. Return a (possibly "
                "empty) list of failure labels — one per distinct problem actually "
                "observed — plus a one-line summary. A clean run returns an empty list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "failures": {
                        "type": "array",
                        "description": (
                            "Failure labels observed in this trajectory. Empty if the "
                            "agent worked cleanly. Do not invent failures."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "class": {
                                    "type": "string",
                                    "enum": FAILURE_CLASS_KEYS,
                                    "description": "The failure class.",
                                },
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                    "description": "How sure you are (0..1).",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Brief evidence for this label (one sentence).",
                                },
                            },
                            "required": ["class", "confidence", "reason"],
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line overall assessment of the failures (or 'clean').",
                    },
                },
                "required": ["failures", "summary"],
            },
        },
    }
]


def _clamp_confidence(raw) -> float:
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, round(c, 3)))


def _parse_failures_from_args(args: dict) -> list[dict]:
    """Parse + validate the ``failures`` list out of a ``classify_failures`` payload.

    Drops labels with an unknown class, clamps confidence to [0, 1], caps the
    reason, and de-duplicates by class (keeping the highest confidence)."""
    by_class: dict[str, dict] = {}
    for item in args.get("failures") or []:
        if not isinstance(item, dict):
            continue
        cls = str(item.get("class") or "").strip()
        if cls not in FAILURE_CLASS_KEYS:
            continue  # unknown class → drop (schema is the source of truth)
        label = {
            "class": cls,
            "confidence": _clamp_confidence(item.get("confidence")),
            "reason": str(item.get("reason") or "")[:_REASON_CAP],
        }
        prev = by_class.get(cls)
        if prev is None or label["confidence"] > prev["confidence"]:
            by_class[cls] = label
    # Stable, taxonomy order.
    return [by_class[k] for k in FAILURE_CLASS_KEYS if k in by_class]


def _summarize_outcome(outcome_profile: dict | None) -> str | None:
    """One-line outcome context for the classifier (E-02), or None when absent."""
    correct, signal, score = _outcome_from_profile(
        outcome_profile, DEFAULT_OUTCOME_THRESHOLD
    )
    if signal == "none":
        return None
    verdict = "correct" if correct else "incorrect"
    score_txt = f", score {score}" if score is not None else ""
    return f"Outcome judged {verdict} (signal: {signal}{score_txt})."


def _summarize_trajectory(trajectory_profile: dict | None) -> str | None:
    """A compact rendering of the E-07 trajectory profile as a grounding signal."""
    if not isinstance(trajectory_profile, dict):
        return None
    if trajectory_profile.get("status") != "scored":
        return None
    axis_keys = {k for k, _, _ in TRAJECTORY_AXES}
    parts = [
        f"{a.get('key')}={a.get('score')}"
        for a in (trajectory_profile.get("axes") or [])
        if a.get("key") in axis_keys
    ]
    if not parts:
        return None
    loop = " loop_detected=true" if trajectory_profile.get("loop_detected") else ""
    summary = str(trajectory_profile.get("summary") or "").strip()
    summary_txt = f" — {summary}" if summary else ""
    return "Trajectory judge (E-07) axis scores 0-10: " + ", ".join(parts) + loop + summary_txt


def _build_inputs(
    cleaned_trace: dict,
    outcome_profile: dict | None,
    trajectory_profile: dict | None,
    max_input_tokens: int,
) -> tuple[str, bool, bool, bool]:
    """Assemble the judge's text input: optional outcome/trajectory context lines
    plus the (budget-fitted) serialized trace. Returns
    (text, input_capped, used_outcome, used_trajectory)."""
    outcome_line = _summarize_outcome(outcome_profile)
    trajectory_line = _summarize_trajectory(trajectory_profile)

    context_lines: list[str] = []
    if outcome_line:
        context_lines.append(outcome_line)
    if trajectory_line:
        context_lines.append(trajectory_line)
    context_block = ("\n".join(context_lines) + "\n\n") if context_lines else ""

    # Fit the trace into the budget left after the (small) context block.
    remaining = max(200, max_input_tokens - _count_tokens(context_block))
    trace_text, input_capped = _fit_trace_to_budget(cleaned_trace, remaining)
    return (
        context_block + trace_text,
        input_capped,
        outcome_line is not None,
        trajectory_line is not None,
    )


async def _classify_failures(
    cleaned_trace: dict,
    outcome_profile: dict | None,
    trajectory_profile: dict | None,
    judge_llm,
    *,
    max_input_tokens: int,
) -> dict:
    """Classify failure modes in one LLM call. Never raises — failures become a
    result dict with ``status: "error"``."""
    serialized, input_capped, used_outcome, used_trajectory = _build_inputs(
        cleaned_trace, outcome_profile, trajectory_profile, max_input_tokens
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict, fair analyst of an AI agent's execution trajectory. "
                "Identify which failure modes the agent exhibited, choosing only from the "
                "given classes, using the classify_failures tool. Label a class ONLY when "
                "there is concrete evidence in the trajectory — do not guess. A trajectory "
                "can have several failures (multi-label) or none at all. A correct final "
                "outcome does not rule out a defective process (e.g. a loop)."
            ),
        },
        {
            "role": "user",
            "content": (
                "Failure classes to choose from:\n"
                + "\n".join(f"- {key}: {desc}" for key, desc in FAILURE_CLASSES)
                + "\n\nAgent trajectory and context:\n"
                + serialized
            ),
        },
    ]
    try:
        resp = await get_llm_provider().acompletion(
            model=judge_llm.model.api_name,
            messages=messages,
            tools=FAILURE_TOOL,
            tool_choice={"type": "function", "function": {"name": "classify_failures"}},
            api_key=judge_llm.provider.api_key,
            api_base=judge_llm.provider.endpoint,
        )
        choice = resp.choices[0].message
        args = json.loads(choice.tool_calls[0].function.arguments)
        in_tok, out_tok = _tokens_from_response(resp)

        return {
            "status": "scored",
            "failures": _parse_failures_from_args(args),
            "summary": str(args.get("summary") or "")[:_SUMMARY_CAP],
            "judge_input_tokens": in_tok,
            "judge_output_tokens": out_tok,
            "judge_cost_usd": _judge_cost(judge_llm, in_tok, out_tok),
            "input_capped": input_capped,
            "used_outcome_profile": used_outcome,
            "used_trajectory_profile": used_trajectory,
        }
    except Exception as e:  # noqa: BLE001 — the judge must not crash the request
        logger.warning(f"failure-mode classifier failed for task: {e}")
        return {
            "status": "error",
            "error": str(e)[:300],
            "input_capped": input_capped,
            "used_outcome_profile": used_outcome,
            "used_trajectory_profile": used_trajectory,
        }


async def evaluate_task_failure_modes(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Classify ``task``'s failure modes and write the profile to its quality record.

    Returns the profile dict, or ``None`` when skipped (no judge model, or an empty
    trace). Reads the existing E-02/E-07 profiles as grounding context but never
    re-runs them. Re-running overwrites the profile (on-demand re-judge). A failed
    LLM/parse call is persisted with ``status: "error"`` — not skipped.
    """
    judge_llm = await _resolve_judge_model(db, task.workspace_id)
    if judge_llm is None:
        logger.info(
            f"failure-mode eval skipped — no judge/orchestrator model for task {task.id}"
        )
        return None

    cleaned_trace = await build_cleaned_trace(db, task)
    if not (cleaned_trace.get("steps") or []):
        logger.info(f"failure-mode eval skipped — empty trace for task {task.id}")
        return None

    from app.api.settings import get_setting

    raw_cap = await get_setting(db, "failure_judge_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
    try:
        max_input_tokens = int(raw_cap)
    except (TypeError, ValueError):
        max_input_tokens = DEFAULT_MAX_INPUT_TOKENS

    # Ensure the quality record exists (E-01); its E-02/E-07 slots are the context.
    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)

    outcome_profile = record.quality_profile if record is not None else None
    trajectory_profile = record.trajectory_profile if record is not None else None

    result = await _classify_failures(
        cleaned_trace,
        outcome_profile,
        trajectory_profile,
        judge_llm,
        max_input_tokens=max_input_tokens,
    )

    stats = cleaned_trace.get("stats") or {}
    profile = {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "status": result.get("status"),
        "failures": result.get("failures", []),
        "summary": result.get("summary", ""),
        "judge_model": judge_llm.model.api_name,
        "judge_input_tokens": result.get("judge_input_tokens", 0),
        "judge_output_tokens": result.get("judge_output_tokens", 0),
        "judge_cost_usd": result.get("judge_cost_usd", 0.0),
        "input_capped": result.get("input_capped", False),
        "used_outcome_profile": result.get("used_outcome_profile", False),
        "used_trajectory_profile": result.get("used_trajectory_profile", False),
        "trace_stats": {
            "original_tokens": stats.get("original_tokens"),
            "cleaned_tokens": stats.get("cleaned_tokens"),
            "steps_total": stats.get("steps_total"),
        },
        "evaluated_at": datetime.utcnow().isoformat(),
        "errors": (
            [{"error": result.get("error")}] if result.get("status") == "error" else []
        ),
    }

    if record is not None:
        record.failure_profile = profile

    await log_event(
        db,
        "failure_modes_evaluated",
        "system",
        {
            "status": profile["status"],
            "failure_classes": [f["class"] for f in profile["failures"]],
            "judge_model": judge_llm.model.api_name,
            "judge_cost_usd": profile["judge_cost_usd"],
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return profile


def _blank_fail_counts() -> dict:
    return {
        "runs_total": 0,
        "failure_runs": 0,
        "by_class": {k: 0 for k in FAILURE_CLASS_KEYS},
    }


def _with_rates(b: dict) -> dict:
    total = b["runs_total"]
    rate = (
        {k: round(c / total, 4) for k, c in b["by_class"].items()} if total else None
    )
    return {
        **b,
        "failure_rate": round(b["failure_runs"] / total, 4) if total else None,
        "rate": rate,
    }


async def aggregate_failure_modes(
    db: AsyncSession,
    *,
    workspace_id,
    model_used: str | None = None,
    template_id=None,
    failure_class: str | None = None,
    suite: str | None = None,
) -> dict:
    """Aggregate failure profiles across a workspace into class distributions.

    For each scored run, every distinct failure label increments its class count;
    ``runs_total`` counts runs and ``failure_runs`` counts runs with ≥1 label.
    Breakdowns by class, model and template give the "distribution of failure
    types per (model, template)" signal. ``failure_class`` narrows the population
    to runs carrying that class; ``suite`` restricts to one Benchmark Case Store
    suite. ``rate`` is per-class count / runs_total within the bucket.
    """
    q = select(QualityRecord).where(
        QualityRecord.workspace_id == workspace_id,
        QualityRecord.failure_profile.isnot(None),
    )
    if model_used:
        q = q.where(QualityRecord.model_used == model_used)
    if template_id is not None:
        q = q.where(QualityRecord.template_id == template_id)
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    rows = (await db.execute(q)).scalars().all()

    overall = _blank_fail_counts()
    by_class: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_template: dict[str, dict] = {}

    for r in rows:
        prof = r.failure_profile or {}
        if prof.get("status") != "scored":
            continue
        classes = [
            f.get("class")
            for f in (prof.get("failures") or [])
            if f.get("class") in FAILURE_CLASS_KEYS
        ]
        if failure_class and failure_class not in classes:
            continue
        model = r.model_used or "unknown"
        tmpl = r.template_name or (str(r.template_id) if r.template_id else "unknown")
        buckets = [
            overall,
            by_model.setdefault(model, _blank_fail_counts()),
            by_template.setdefault(tmpl, _blank_fail_counts()),
        ]
        # A per-class bucket only for the classes present on this run.
        for cls in set(classes):
            buckets.append(by_class.setdefault(cls, _blank_fail_counts()))
        for bucket in buckets:
            bucket["runs_total"] += 1
            if classes:
                bucket["failure_runs"] += 1
            for cls in set(classes):
                bucket["by_class"][cls] += 1

    return {
        "workspace_id": str(workspace_id),
        "filters": {
            "model_used": model_used,
            "template_id": str(template_id) if template_id else None,
            "failure_class": failure_class,
            "suite": suite,
        },
        **_with_rates(overall),
        "by_class": {k: _with_rates(v) for k, v in sorted(by_class.items())},
        "by_model": {k: _with_rates(v) for k, v in sorted(by_model.items())},
        "by_template": {k: _with_rates(v) for k, v in sorted(by_template.items())},
    }
