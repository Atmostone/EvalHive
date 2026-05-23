"""LLM-as-judge quality evaluation (E-02).

Scores a finished task's result against its rubric, one independent LLM call per
``judge`` dimension, into a quality profile written to
``quality_records.quality_profile``. ``reference`` (E-03) and ``objective``
(E-04) dimensions are scored by their own engines and folded into the same
profile; ``human`` (E-05) dimensions are recorded as ``deferred`` until that
subsystem exists.

Independence (acceptance criterion): dimensions are scored concurrently and each
call is wrapped so one evaluator failing never blocks the others — a failed
dimension is recorded with ``status: "error"`` instead of raising.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.plugins.llm import get_llm_provider
from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.rubric import resolve_rubric_for_task
from app.utils.events import log_event

logger = logging.getLogger(__name__)

PROFILE_SCHEMA_VERSION = 2
_MAX_SCALE = 10
# Cap the result text handed to the judge to keep prompts bounded.
_RESULT_CHAR_CAP = 8000

JUDGE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "score_dimension",
            "description": (
                "Score the task result on a single quality dimension from 0 to 10 "
                "and justify the score in one or two sentences."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "score": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": _MAX_SCALE,
                        "description": "Quality on this dimension, 0 (worst) to 10 (best).",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Brief justification for the score.",
                    },
                },
                "required": ["score", "reasoning"],
            },
        },
    }
]


async def _resolve_judge_model(db: AsyncSession, workspace_id):
    """Resolve the quality-judge model, falling back to the orchestrator model."""
    from app.api._resolve_model import resolve_workspace_model

    for kind in ("quality_judge", "orchestrator"):
        try:
            return await resolve_workspace_model(db, workspace_id, kind)
        except Exception:
            continue
    return None


def _result_context(task: Task) -> str:
    summary = (task.result_summary or "").strip()
    if len(summary) > _RESULT_CHAR_CAP:
        summary = summary[:_RESULT_CHAR_CAP] + "\n…[truncated]"
    files = [str(f) for f in (task.result_files or [])]
    parts = [
        f"Task title: {task.title}",
        f"Task description: {task.description or '(none)'}",
        f"Result files: {', '.join(files) if files else '(none)'}",
        "",
        "Result:",
        summary or "(empty)",
    ]
    return "\n".join(parts)


def _tokens_from_response(resp) -> tuple[int, int]:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(
        getattr(usage, "completion_tokens", 0) or 0
    )


async def _judge_dimension(dim: dict, context: str, judge_llm) -> dict:
    """Score one ``judge`` dimension. Never raises — errors become a result dict."""
    name = dim.get("name") or dim.get("key")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict, fair quality judge. Score the task result on ONE "
                "dimension only, ignoring all other aspects. Use the score_dimension tool. "
                "Be calibrated: 10 is excellent, 5 is mediocre, 0 is absent/broken."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Dimension: {name}\n"
                f"What it measures: {dim.get('description') or name}\n\n"
                f"{context}"
            ),
        },
    ]
    try:
        resp = await get_llm_provider().acompletion(
            model=judge_llm.model.api_name,
            messages=messages,
            tools=JUDGE_TOOL,
            tool_choice={"type": "function", "function": {"name": "score_dimension"}},
            api_key=judge_llm.provider.api_key,
            api_base=judge_llm.provider.endpoint,
        )
        choice = resp.choices[0].message
        args = json.loads(choice.tool_calls[0].function.arguments)
        score = max(0, min(_MAX_SCALE, int(args["score"])))
        inp, out = _tokens_from_response(resp)
        return {
            "status": "scored",
            "score": score,
            "reasoning": str(args.get("reasoning") or "")[:1000],
            "input_tokens": inp,
            "output_tokens": out,
        }
    except Exception as e:  # noqa: BLE001 — one dimension must not break the rest
        logger.warning(f"judge dimension '{dim.get('key')}' failed: {e}")
        return {"status": "error", "score": None, "error": str(e)[:300]}


def _judge_cost(llm, input_tokens: int, output_tokens: int) -> float:
    in_rate = Decimal(llm.model.input_price_per_1m_usd or 0)
    out_rate = Decimal(llm.model.output_price_per_1m_usd or 0)
    cost = (Decimal(input_tokens) / Decimal(1_000_000)) * in_rate + (
        Decimal(output_tokens) / Decimal(1_000_000)
    ) * out_rate
    return float(cost.quantize(Decimal("0.000001")))


async def evaluate_task_quality(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Score ``task`` against its rubric and write the profile to its quality record.

    Returns the profile dict, or ``None`` when skipped (no rubric or no judge model).
    Re-running overwrites any existing profile (intentional, for on-demand re-judge).
    """
    rubric = await resolve_rubric_for_task(db, task)
    if rubric is None:
        logger.info(f"quality eval skipped — no rubric for task {task.id}")
        return None

    judge_llm = await _resolve_judge_model(db, task.workspace_id)
    if judge_llm is None:
        logger.info(
            f"quality eval skipped — no judge/orchestrator model for task {task.id}"
        )
        return None

    context = _result_context(task)
    dims = list(rubric.dimensions or [])

    from app.quality.reference import evaluate_reference_dimension
    from app.quality.objective import evaluate_objective_dimension

    # Score judge + reference + objective dimensions concurrently; each call is
    # isolated so one failure never blocks the rest. human evaluators stay deferred.
    coros = []
    coro_idx = []
    for i, d in enumerate(dims):
        evaluator = d.get("evaluator", "judge")
        if evaluator == "judge":
            coros.append(_judge_dimension(d, context, judge_llm))
            coro_idx.append(i)
        elif evaluator == "reference":
            coros.append(evaluate_reference_dimension(d, task, judge_llm))
            coro_idx.append(i)
        elif evaluator == "objective":
            coros.append(evaluate_objective_dimension(d, task))
            coro_idx.append(i)
    results = await asyncio.gather(*coros)
    by_idx = dict(zip(coro_idx, results))

    out_dims: list[dict] = []
    errors: list[dict] = []
    in_tok = out_tok = 0
    weighted_num = weighted_den = 0.0
    failed_critical: list[str] = []

    for i, d in enumerate(dims):
        evaluator = d.get("evaluator", "judge")
        entry = {
            "key": d.get("key"),
            "name": d.get("name") or d.get("key"),
            "evaluator": evaluator,
            "max": _MAX_SCALE,
            "weight": d.get("weight"),
            "threshold": d.get("threshold"),
            "critical": bool(d.get("critical")),
        }
        if evaluator == "reference":
            entry["reference_mode"] = d.get("reference_mode") or "pointwise"
        if evaluator == "objective":
            entry["probe"] = d.get("probe") or "lint"
        if evaluator not in ("judge", "reference", "objective"):
            # human (E-05) — schema-valid but not scored yet.
            entry.update({"status": "deferred", "score": None})
            out_dims.append(entry)
            continue

        res = by_idx.get(i, {"status": "error", "score": None})
        entry["status"] = res.get("status")
        entry["score"] = res.get("score")
        if res.get("status") == "scored":
            entry["reasoning"] = res.get("reasoning")
            in_tok += int(res.get("input_tokens") or 0)
            out_tok += int(res.get("output_tokens") or 0)
            weight = float(d.get("weight") or 0)
            weighted_num += entry["score"] * weight
            weighted_den += weight
            threshold = d.get("threshold")
            entry["passed"] = threshold is None or entry["score"] >= threshold
            if entry["critical"] and not entry["passed"]:
                failed_critical.append(d.get("key"))
        elif res.get("status") == "skipped":
            # reference dim w/o reference_answer, or objective probe w/o matching
            # artifact — neither scored nor an error; excluded from gate/weighted.
            pass
        else:
            entry["error"] = res.get("error")
            errors.append({"key": d.get("key"), "error": res.get("error")})
        out_dims.append(entry)

    weighted_score = round(weighted_num / weighted_den, 2) if weighted_den else None

    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "rubric_id": str(rubric.id),
        "rubric_name": rubric.name,
        "dimensions": out_dims,
        "weighted_score": weighted_score,
        "gate": {"passed": len(failed_critical) == 0, "failed_dimensions": failed_critical},
        "judge_model": judge_llm.model.api_name,
        "judge_input_tokens": in_tok,
        "judge_output_tokens": out_tok,
        "judge_cost_usd": _judge_cost(judge_llm, in_tok, out_tok),
        "evaluated_at": datetime.utcnow().isoformat(),
        "errors": errors,
    }

    # Ensure the quality record exists (E-01), then write the profile slot.
    record = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == task.id)
        )
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)
    if record is not None:
        record.quality_profile = profile

    await log_event(
        db,
        "quality_evaluated",
        "system",
        {
            "rubric": rubric.name,
            "weighted_score": weighted_score,
            "gate_passed": profile["gate"]["passed"],
            "judge_model": judge_llm.model.api_name,
            "judge_cost_usd": profile["judge_cost_usd"],
            "errors": len(errors),
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return profile


async def evaluate_task_quality_by_id(task_id: uuid.UUID | str) -> dict | None:
    """Standalone entrypoint (used by the scheduler): open a session and evaluate."""
    from app.database import async_session

    if isinstance(task_id, str):
        task_id = uuid.UUID(task_id)
    async with async_session() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return None
        return await evaluate_task_quality(db, task)
