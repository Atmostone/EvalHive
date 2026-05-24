"""TRACE Evidence Bank Judge (E-08).

The 6-axis judge (E-07) scores the whole trajectory holistically — each step is
weighed without the context of what the agent has *already established*. In the
reference-free setting that is weak: a context-less judge cannot tell that a step
re-derives a known fact, or that a correct final answer rests on nothing the agent
actually gathered ("🤷 lucky" — guessed / recalled from memory rather than worked
out from tools).

E-08 follows the TRACE approach: it walks the cleaned trace (E-06) step by step,
accumulating an **evidence bank** — the facts established by prior steps. The bank
persists between steps and is fed into the prompt that assesses the *next* step, so
each step is judged on the background of the accumulated evidence. After the walk a
single evidence-aware call produces the same 6-axis profile as E-07 (for direct
comparison), plus a ``groundedness`` signal derived from the bank.

Pipeline (faithful TRACE): N per-step ``assess_step`` calls (each sees the bank so
far) + 1 final ``score_trajectory`` call informed by the bank — ``N + 1`` calls.
Cost is bounded by ``trace_evidence_max_steps`` (head+tail window) and
``trace_evidence_max_input_tokens`` (final-call budget). Model selection, the
6-axis tool and the axis parser are reused from E-07/E-02 (DRY). Consistent with
the rest of ``app.quality`` the judge never raises: a per-step failure degrades to
a step marked with an error and the walk continues; a final-call failure becomes
``status: "error"``. The profile is written to the
``quality_records.trajectory_evidence_profile`` slot, next to E-07's
``trajectory_profile``.
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
from app.quality.judge import _judge_cost, _resolve_judge_model, _tokens_from_response
from app.quality.trace_cleaner import _count_tokens, _truncate_to_tokens, build_cleaned_trace
from app.quality.trajectory import (
    _MAX_SCALE,
    AXES,
    DEFAULT_MAX_INPUT_TOKENS,
    TRAJECTORY_TOOL,
    _parse_axes_from_args,
)
from app.utils.events import log_event

logger = logging.getLogger(__name__)

TRACE_EVIDENCE_SCHEMA_VERSION = 1
# Default cap on assessed steps per task (head+tail window beyond this), overridable
# via `trace_evidence_max_steps`. Bounds the N per-step calls (§5.4 cost control).
DEFAULT_MAX_STEPS = 30
_MAX_FACTS_PER_STEP = 6
_FACT_CAP = 200
_NOTE_CAP = 300


# Per-step tool: assess one step in the context of the evidence bank so far and
# extract the new evidence it establishes (appended to the bank for later steps).
ASSESS_STEP_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "assess_step",
            "description": (
                "Assess one trajectory step against the evidence already gathered, "
                "and extract the new evidence this step establishes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "redundant": {
                        "type": "boolean",
                        "description": "True if this step mainly re-derives a fact already in the evidence bank.",
                    },
                    "grounded": {
                        "type": "boolean",
                        "description": (
                            "True if the step's action/conclusion is justified by the task and the "
                            "evidence gathered so far — not a guess or unsupported leap."
                        ),
                    },
                    "progress": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": _MAX_SCALE,
                        "description": "How much useful NEW evidence toward the goal this step added (0 none, 10 decisive).",
                    },
                    "execution": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": _MAX_SCALE,
                        "description": "Tool choice, parameters and error handling quality given the context (0 broken, 10 flawless).",
                    },
                    "new_facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Concise facts/observations this step established, to add to the evidence bank.",
                    },
                    "note": {"type": "string", "description": "One-line note on this step."},
                },
                "required": ["redundant", "grounded", "progress", "execution", "new_facts", "note"],
            },
        },
    }
]


def _select_steps(steps: list[dict], max_steps: int) -> tuple[list[dict], bool]:
    """Cap the assessed steps to a head+tail window (order preserved).

    The outcome lives in the tail, so when a trajectory exceeds the budget we keep
    the opening and closing steps and drop the middle. Returns (steps, capped)."""
    if max_steps <= 0 or len(steps) <= max_steps:
        return list(steps), False
    head = max_steps // 2
    tail = max_steps - head
    return list(steps[:head]) + list(steps[-tail:]), True


def _serialize_step(step: dict) -> str:
    tool = step.get("tool_name")
    label = f"{step.get('kind')}/{tool}" if tool else str(step.get("kind"))
    trunc = " [truncated]" if step.get("truncated") else ""
    content = (step.get("content") or "").strip()
    return f"[{step.get('seq')}] {label}{trunc}: {content}"


def _format_bank(facts: list[tuple]) -> str:
    """Render the accumulated evidence (list of (source_seq, fact)) for a prompt."""
    if not facts:
        return "(empty — no evidence gathered yet)"
    return "\n".join(f"- [from step {seq}] {fact}" for seq, fact in facts)


def _parse_step(step: dict, args: dict) -> dict:
    try:
        progress = max(0, min(_MAX_SCALE, int(args.get("progress"))))
    except (TypeError, ValueError):
        progress = 0
    try:
        execution = max(0, min(_MAX_SCALE, int(args.get("execution"))))
    except (TypeError, ValueError):
        execution = 0
    facts = [str(f)[:_FACT_CAP] for f in (args.get("new_facts") or [])][:_MAX_FACTS_PER_STEP]
    return {
        "seq": step.get("seq"),
        "kind": step.get("kind"),
        "tool_name": step.get("tool_name"),
        "redundant": bool(args.get("redundant")),
        "grounded": bool(args.get("grounded")),
        "progress": progress,
        "execution": execution,
        "facts": facts,
        "note": str(args.get("note") or "")[:_NOTE_CAP],
    }


async def _build_evidence_bank(
    task: dict, steps: list[dict], judge_llm
) -> tuple[list[dict], int, int, list[dict]]:
    """Walk the steps, threading the accumulated evidence bank into each prompt.

    Returns (bank, input_tokens, output_tokens, errors). A per-step LLM/parse
    failure is recorded as a zero-scored step with an ``error`` and the walk
    continues — one bad step must not abort the trajectory."""
    provider = get_llm_provider()
    bank: list[dict] = []
    facts: list[tuple] = []  # (source_seq, fact) accumulated so far
    in_tot = out_tot = 0
    errors: list[dict] = []

    system = (
        "You are a strict, fair judge maintaining an evidence bank while reviewing an "
        "AI agent's execution step by step. You are given the evidence gathered so far "
        "and the current step. Judge THIS step on the background of that evidence: is it "
        "redundant (re-derives a known fact), is it grounded (justified by the task and "
        "prior evidence rather than a guess), how much new evidence it adds, and how well "
        "it was executed. Then list the concrete new facts it established. Use the "
        "assess_step tool."
    )
    for s in steps:
        user = (
            f"Task title: {task.get('title') or '(none)'}\n"
            f"Task description: {task.get('description') or '(none)'}\n\n"
            f"Evidence bank so far:\n{_format_bank(facts)}\n\n"
            f"Current step:\n{_serialize_step(s)}"
        )
        try:
            resp = await provider.acompletion(
                model=judge_llm.model.api_name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                tools=ASSESS_STEP_TOOL,
                tool_choice={"type": "function", "function": {"name": "assess_step"}},
                api_key=judge_llm.provider.api_key,
                api_base=judge_llm.provider.endpoint,
            )
            args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
            it, ot = _tokens_from_response(resp)
            in_tot += it
            out_tot += ot
            rec = _parse_step(s, args)
        except Exception as e:  # noqa: BLE001 — one bad step must not abort the walk
            logger.warning(f"evidence step assessment failed (seq {s.get('seq')}): {e}")
            rec = {
                "seq": s.get("seq"),
                "kind": s.get("kind"),
                "tool_name": s.get("tool_name"),
                "redundant": False,
                "grounded": False,
                "progress": 0,
                "execution": 0,
                "facts": [],
                "note": "",
                "error": str(e)[:200],
            }
            errors.append({"seq": s.get("seq"), "error": str(e)[:200]})
        bank.append(rec)
        for f in rec["facts"]:
            facts.append((rec["seq"], f))
    return bank, in_tot, out_tot, errors


def _annotate_step(step: dict, rec: dict | None) -> str:
    """Serialize a step with its evidence-bank annotations for the final scoring."""
    line = _serialize_step(step)
    if rec is None:
        return line
    tags = []
    if rec.get("facts"):
        tags.append("new evidence: " + "; ".join(rec["facts"]))
    tags.append("redundant" if rec.get("redundant") else "non-redundant")
    tags.append("grounded" if rec.get("grounded") else "ungrounded")
    if rec.get("note"):
        tags.append(rec["note"])
    return f"{line}\n    ↳ {' | '.join(tags)}"


def _fit_annotated_to_budget(
    header: str, blocks: list[str], max_input_tokens: int
) -> tuple[str, bool]:
    """Join header + per-step annotated blocks, dropping middle blocks to fit budget.

    Mirrors E-07's head+tail strategy; hard-truncates as a last resort. Returns
    (text, input_capped)."""
    text = header + "\n".join(blocks)
    if _count_tokens(text) <= max_input_tokens:
        return text, False

    kept = list(blocks)
    omitted = 0
    while len(kept) > 2 and _count_tokens(text) > max_input_tokens:
        kept.pop(len(kept) // 2)
        omitted += 1
        mid = len(kept) // 2
        marker = f"… [{omitted} middle step(s) omitted to fit the judge token budget] …"
        text = header + "\n".join(kept[:mid] + [marker] + kept[mid:])

    if _count_tokens(text) > max_input_tokens:
        text, _ = _truncate_to_tokens(text, max_input_tokens)
    return text, True


async def _score_with_evidence(
    task: dict, steps: list[dict], bank: list[dict], judge_llm, *, max_input_tokens: int
) -> dict:
    """Final evidence-aware 6-axis scoring (one call). Never raises — a failure
    becomes ``status: "error"``."""
    by_seq = {r["seq"]: r for r in bank}
    header = (
        f"Task title: {task.get('title') or '(none)'}\n"
        f"Task description: {task.get('description') or '(none)'}\n\n"
        "Trajectory steps (chronological), each annotated with the evidence bank "
        "accumulated step by step:\n"
    )
    blocks = [_annotate_step(s, by_seq.get(s.get("seq"))) for s in steps]
    serialized, input_capped = _fit_annotated_to_budget(header, blocks, max_input_tokens)

    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict, fair judge of an AI agent's execution trajectory. Assess "
                "HOW the agent reached its result, using the evidence-bank annotations: a step "
                "marked 'redundant' re-derived a known fact (hurts efficiency); a final result "
                "that is 'ungrounded' — not supported by the gathered evidence — means the agent "
                "got lucky (guessed or recalled) rather than working it out, which you must "
                "penalize on goal_alignment. Score each of the six axes 0 (worst) to 10 (best) "
                "using the score_trajectory tool, with a brief reason per axis and a one-line "
                "summary. Be calibrated: 10 is flawless, 5 is mediocre, 0 is absent/broken."
            ),
        },
        {
            "role": "user",
            "content": (
                "Axes to score:\n"
                + "\n".join(f"- {name}: {desc}" for _, name, desc in AXES)
                + "\n\nAnnotated agent trajectory:\n"
                + serialized
            ),
        },
    ]
    try:
        resp = await get_llm_provider().acompletion(
            model=judge_llm.model.api_name,
            messages=messages,
            tools=TRAJECTORY_TOOL,
            tool_choice={"type": "function", "function": {"name": "score_trajectory"}},
            api_key=judge_llm.provider.api_key,
            api_base=judge_llm.provider.endpoint,
        )
        args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
        in_tok, out_tok = _tokens_from_response(resp)
        axes, overall, loop_detected = _parse_axes_from_args(args)
        return {
            "status": "scored",
            "axes": axes,
            "overall_score": overall,
            "loop_detected": loop_detected,
            "summary": str(args.get("summary") or "")[:1000],
            "judge_input_tokens": in_tok,
            "judge_output_tokens": out_tok,
            "input_capped": input_capped,
        }
    except Exception as e:  # noqa: BLE001 — the judge must not crash the request
        logger.warning(f"evidence final scoring failed: {e}")
        return {"status": "error", "error": str(e)[:300], "input_capped": input_capped}


async def evaluate_trajectory_with_evidence(
    cleaned_trace: dict, judge_llm, *, max_input_tokens: int, max_steps: int
) -> dict:
    """Score a cleaned trace (E-06) the TRACE way and return the profile dict.

    Builds the evidence bank step by step (each step assessed against the bank so
    far), then produces an evidence-aware 6-axis profile comparable to E-07, plus a
    ``groundedness`` signal. Never raises."""
    task = cleaned_trace.get("task") or {}
    all_steps = list(cleaned_trace.get("steps") or [])
    steps, steps_capped = _select_steps(all_steps, max_steps)

    bank, in1, out1, step_errors = await _build_evidence_bank(task, steps, judge_llm)
    final = await _score_with_evidence(
        task, steps, bank, judge_llm, max_input_tokens=max_input_tokens
    )

    grounded = sum(1 for r in bank if r.get("grounded"))
    redundant_steps = sum(1 for r in bank if r.get("redundant"))
    groundedness = round(grounded / len(bank), 2) if bank else None
    total_in = in1 + final.get("judge_input_tokens", 0)
    total_out = out1 + final.get("judge_output_tokens", 0)
    stats = cleaned_trace.get("stats") or {}
    errors = list(step_errors)
    if final.get("status") == "error":
        errors.append({"error": final.get("error")})

    return {
        "schema_version": TRACE_EVIDENCE_SCHEMA_VERSION,
        "status": final.get("status"),
        "axes": final.get("axes", []),
        "overall_score": final.get("overall_score"),
        "loop_detected": final.get("loop_detected", False),
        "summary": final.get("summary", ""),
        "groundedness": groundedness,
        "redundant_steps": redundant_steps,
        "evidence_bank": bank,
        "judge_model": judge_llm.model.api_name,
        "judge_calls": len(steps) + 1,
        "judge_input_tokens": total_in,
        "judge_output_tokens": total_out,
        "judge_cost_usd": _judge_cost(judge_llm, total_in, total_out),
        "input_capped": final.get("input_capped", False) or steps_capped,
        "trace_stats": {
            "original_tokens": stats.get("original_tokens"),
            "cleaned_tokens": stats.get("cleaned_tokens"),
            "steps_total": stats.get("steps_total"),
            "steps_assessed": len(steps),
        },
        "evaluated_at": datetime.utcnow().isoformat(),
        "errors": errors,
    }


async def evaluate_task_trace_evidence(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Judge ``task``'s trajectory with the evidence bank and write the profile to
    its quality record's ``trajectory_evidence_profile`` slot.

    Returns the profile dict, or ``None`` when skipped (no judge model, or an empty
    trace with no steps). A failed final call is persisted with ``status: "error"``
    — not skipped. Re-running overwrites any existing profile."""
    judge_llm = await _resolve_judge_model(db, task.workspace_id)
    if judge_llm is None:
        logger.info(f"trace evidence eval skipped — no judge/orchestrator model for task {task.id}")
        return None

    cleaned_trace = await build_cleaned_trace(db, task)
    if not (cleaned_trace.get("steps") or []):
        logger.info(f"trace evidence eval skipped — empty trace for task {task.id}")
        return None

    from app.api.settings import get_setting

    raw_cap = await get_setting(db, "trace_evidence_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
    try:
        max_input_tokens = int(raw_cap)
    except (TypeError, ValueError):
        max_input_tokens = DEFAULT_MAX_INPUT_TOKENS
    raw_steps = await get_setting(db, "trace_evidence_max_steps", DEFAULT_MAX_STEPS)
    try:
        max_steps = int(raw_steps)
    except (TypeError, ValueError):
        max_steps = DEFAULT_MAX_STEPS

    profile = await evaluate_trajectory_with_evidence(
        cleaned_trace, judge_llm, max_input_tokens=max_input_tokens, max_steps=max_steps
    )

    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)
    if record is not None:
        record.trajectory_evidence_profile = profile

    await log_event(
        db,
        "trajectory_evidence_evaluated",
        "system",
        {
            "overall_score": profile["overall_score"],
            "groundedness": profile["groundedness"],
            "loop_detected": profile["loop_detected"],
            "status": profile["status"],
            "judge_model": judge_llm.model.api_name,
            "judge_calls": profile["judge_calls"],
            "judge_cost_usd": profile["judge_cost_usd"],
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return profile
