"""6-axis Trajectory Judge (E-07).

Outcome evaluation (E-02) answers "how good is the result", not "how the agent
got there". A correct answer reached by a 12-step trajectory that should have
been 4 steps is "🤷 lucky": such agents are expensive and unstable. This module
adds the second axis — trajectory.

It takes the cleaned trace from E-06 (`build_cleaned_trace`) and, in a SINGLE
LLM call, scores the whole trajectory on six axes (§5.2 of EVALUATION_FRAMEWORK):
efficiency, tool_selection, parameter_quality, error_recovery, goal_alignment,
loop_detection — each 0-10 with a required reason — plus a one-line summary. The
profile is written to ``quality_records.trajectory_profile`` (next to E-02's
outcome ``quality_profile``).

Consistent with the rest of `app.quality`, the judge never raises: an LLM or
parse failure becomes ``status: "error"`` instead of an exception, and the
endpoint still answers. Cost is bounded by the configurable
``trajectory_judge_max_input_tokens`` setting (the cleaned trace is trimmed to
fit before the call). Model selection reuses E-02's resolver
(`quality_judge` → `orchestrator`).
"""

from __future__ import annotations

import hashlib
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
from app.quality.trace_loops import detect_loops
from app.utils.events import log_event

logger = logging.getLogger(__name__)

# v2: loop_analysis (deterministic loop detector, E-07 anchor)
# v3: prompt_fingerprint — the conditions the verdict was obtained under (SPA-85)
TRAJECTORY_SCHEMA_VERSION = 3
_MAX_SCALE = 10
# Default cap on the judge's input (cleaned trace) tokens per task; overridable
# via the `trajectory_judge_max_input_tokens` setting (acceptance: cost cap).
DEFAULT_MAX_INPUT_TOKENS = 12000
# loop_detection axis below this score → the derived `loop_detected` badge flips.
_LOOP_SCORE_THRESHOLD = 5
_REASON_CAP = 500
_SUMMARY_CAP = 1000

# The 6 trajectory axes (§5.2): (key, display name, what it measures).
AXES: list[tuple[str, str, str]] = [
    ("efficiency", "Efficiency",
     "were there redundant or repeated steps; could the path be shorter"),
    ("tool_selection", "Tool selection",
     "were the right tools chosen (no confusion between similar tools)"),
    ("parameter_quality", "Parameter quality",
     "were the parameters in the tool calls correct"),
    ("error_recovery", "Error recovery",
     "how the agent reacted to tool errors (adequate retry / stuck in a loop / ignored)"),
    ("goal_alignment", "Goal alignment",
     "did each step move toward the goal or were there distractions"),
    ("loop_detection", "Loop detection",
     "did the agent get stuck repeating itself (10 = no loops, 0 = badly stuck)"),
]


# Pulled out of the call site so the prompt can be fingerprinted (SPA-85) rather
# than reconstructed. The axis wording lives in AXES and is hashed with it.
_TRAJECTORY_SYSTEM_PROMPT = (
    "You are a strict, fair judge of an AI agent's execution trajectory. "
    "Assess HOW the agent reached its result — not whether the final answer "
    "is correct. Score each of the six axes from 0 (worst) to 10 (best) using "
    "the score_trajectory tool, with a brief reason per axis and a one-line "
    "summary. Be calibrated: 10 is flawless, 5 is mediocre, 0 is absent/broken. "
    "Set applicable=false for an axis that does not apply to this run (it will "
    "be excluded from the aggregate, not scored 0): parameter_quality and "
    "efficiency when the agent made zero tool calls / did no real work; "
    "error_recovery when no tool errors occurred (nothing to recover from); "
    "loop_detection when there was no real activity (crashed at step 1)."
)


def _axes_block() -> str:
    return "Axes to score:\n" + "\n".join(f"- {name}: {desc}" for _, name, desc in AXES)


def trajectory_prompt_fingerprint() -> str:
    """Hash of everything the trajectory judge is asked, minus the trace itself:
    the system prompt, the axis definitions and the tool schema."""
    payload = {
        "system": _TRAJECTORY_SYSTEM_PROMPT,
        "axes": _axes_block(),
        "tool": TRAJECTORY_TOOL,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _axis_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": _MAX_SCALE,
                "description": "0 (worst) to 10 (best).",
            },
            "reason": {
                "type": "string",
                "description": "Brief justification for the score (one sentence).",
            },
            "applicable": {
                "type": "boolean",
                "description": (
                    "false if this axis does not apply to this trajectory at all — "
                    "when false the axis is EXCLUDED from the trajectory aggregate, "
                    "not scored 0."
                ),
            },
        },
        "required": ["score", "reason"],
    }


# Single function-tool: all six axes scored at once across the whole trajectory
# (§5.4 cost control — one call, not three-per-step).
TRAJECTORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "score_trajectory",
            "description": (
                "Score the agent's whole execution trajectory on six axes, each 0-10 "
                "with a brief reason, plus a one-line overall summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **{key: _axis_schema() for key, _, _ in AXES},
                    "summary": {
                        "type": "string",
                        "description": "One-line overall assessment of the trajectory.",
                    },
                },
                "required": [key for key, _, _ in AXES] + ["summary"],
            },
        },
    }
]


def _parse_axes_from_args(args: dict) -> tuple[list[dict], float | None, bool]:
    """Parse the 6 axes out of a ``score_trajectory`` tool-call payload.

    Clamps each score to [0, 10], caps the reason, and derives the overall mean
    and the ``loop_detected`` flag. Shared by the holistic judge (E-07) and the
    evidence-aware final scoring (E-08)."""
    axes: list[dict] = []
    total = 0
    scored_count = 0
    for key, name, _ in AXES:
        raw = args.get(key)
        # The judge usually returns {"score", "reason"} per axis, but some models
        # emit a bare scalar (``"efficiency": 8``); tolerate both so one variant
        # response can't crash the whole scoring.
        if isinstance(raw, dict):
            raw_score, raw_reason = raw.get("score"), raw.get("reason")
            applicable = raw.get("applicable")
        else:
            raw_score, raw_reason = raw, ""
            applicable = None
        if applicable is False:
            # Axis inherently N/A for this trajectory (e.g. error_recovery with no
            # tool errors, parameter_quality with zero tool calls) — excluded from
            # the aggregate (both the total AND the divisor), not scored 0.
            axes.append(
                {
                    "key": key,
                    "name": name,
                    "score": None,
                    "status": "not_applicable",
                    "reason": str(raw_reason or "")[:_REASON_CAP],
                }
            )
            continue
        try:
            score = int(raw_score)
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(_MAX_SCALE, score))
        axes.append(
            {
                "key": key,
                "name": name,
                "score": score,
                "reason": str(raw_reason or "")[:_REASON_CAP],
            }
        )
        total += score
        scored_count += 1

    # Renormalize over only the scored axes — an excluded axis must not drag the
    # mean toward 0 by dividing by the fixed axis count.
    overall = round(total / scored_count, 2) if scored_count else None
    # The loop badge only flips on a real, scored loop_detection axis; a N/A
    # loop_detection axis (score None) leaves the badge False.
    loop_axis = next((a for a in axes if a["key"] == "loop_detection"), None)
    loop_detected = bool(
        loop_axis
        and loop_axis.get("score") is not None
        and loop_axis["score"] < _LOOP_SCORE_THRESHOLD
    )
    return axes, overall, loop_detected


def _serialize_trace(cleaned_trace: dict) -> str:
    """Render a cleaned trace (E-06 dict) as the judge's text input."""
    task = cleaned_trace.get("task") or {}
    lines = [
        f"Task title: {task.get('title') or '(none)'}",
        f"Task description: {task.get('description') or '(none)'}",
        "",
        "Trajectory steps (chronological):",
    ]
    for s in cleaned_trace.get("steps") or []:
        tool = s.get("tool_name")
        label = f"{s.get('kind')}/{tool}" if tool else str(s.get("kind"))
        trunc = " [truncated]" if s.get("truncated") else ""
        content = (s.get("content") or "").strip()
        lines.append(f"[{s.get('seq')}] {label}{trunc}: {content}")
    return "\n".join(lines)


def _fit_trace_to_budget(cleaned_trace: dict, max_input_tokens: int) -> tuple[str, bool]:
    """Serialize the trace, trimming it to the judge's input budget if needed.

    The outcome lives in the tail, so middle steps are dropped first (head+tail
    preserved); a hard token truncation is the last resort. Returns
    (serialized_text, input_capped).
    """
    text = _serialize_trace(cleaned_trace)
    if _count_tokens(text) <= max_input_tokens:
        return text, False

    steps = list(cleaned_trace.get("steps") or [])
    omitted = 0
    while len(steps) > 2 and _count_tokens(text) > max_input_tokens:
        steps.pop(len(steps) // 2)
        omitted += 1
        marker = {
            "seq": "…",
            "kind": "omitted",
            "tool_name": None,
            "truncated": True,
            "content": f"[{omitted} middle step(s) omitted to fit the judge token budget]",
        }
        head = len(steps) // 2
        view = {**cleaned_trace, "steps": steps[:head] + [marker] + steps[head:]}
        text = _serialize_trace(view)

    if _count_tokens(text) > max_input_tokens:
        text, _ = _truncate_to_tokens(text, max_input_tokens)
    return text, True


def _loads_lenient(raw: str | None) -> dict:
    """Parse the judge's score_trajectory arguments, tolerating the JSON defects
    some models emit: a ```json fence, prose before/after the object, or trailing
    junk after a complete object. As a last resort, close a truncated object. Was
    the cause of ~27% of E-07 evals erroring out (``Expecting value: line 1 ...``)."""
    if not raw or not raw.strip():
        raise ValueError("empty tool-call arguments")
    s = raw.strip()
    if s.startswith("```"):  # ```json ... ``` fence
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Extract the first balanced {...} object via a string/escape-aware brace scan,
    # ignoring any pre/post prose and trailing junk.
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object in tool-call arguments")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start : i + 1])
    # Unbalanced → the object was truncated mid-stream; best-effort close it.
    return json.loads(s[start:] + "}" * depth)


def _extract_tool_args(choice) -> dict:
    """Pull the score_trajectory arguments off the judge message, tolerating
    providers that ignore ``tool_choice`` and return the JSON in ``content``."""
    raw = None
    tcs = getattr(choice, "tool_calls", None)
    if tcs:
        raw = tcs[0].function.arguments
    if not raw:  # provider ignored the forced tool call — JSON may be in content
        raw = getattr(choice, "content", None)
    return _loads_lenient(raw)


async def _judge_trajectory(cleaned_trace: dict, judge_llm, *, max_input_tokens: int) -> dict:
    """Score the whole trajectory in one LLM call. Never raises — failures become
    a result dict with ``status: "error"``."""
    serialized, input_capped = _fit_trace_to_budget(cleaned_trace, max_input_tokens)
    messages = [
        {"role": "system", "content": _TRAJECTORY_SYSTEM_PROMPT},
        {"role": "user", "content": _axes_block() + "\n\nAgent trajectory:\n" + serialized},
    ]
    last_err: Exception | None = None
    for attempt in range(2):  # one retry — malformed/truncated judge JSON is transient
        try:
            resp = await get_llm_provider().acompletion(
                model=judge_llm.model.api_name,
                messages=messages,
                tools=TRAJECTORY_TOOL,
                tool_choice={"type": "function", "function": {"name": "score_trajectory"}},
                api_key=judge_llm.provider.api_key,
                api_base=judge_llm.provider.endpoint,
            )
            choice = resp.choices[0].message
            args = _extract_tool_args(choice)
            in_tok, out_tok = _tokens_from_response(resp)

            axes, overall, loop_detected = _parse_axes_from_args(args)
            return {
                "status": "scored",
                "axes": axes,
                "overall_score": overall,
                "loop_detected": loop_detected,
                "summary": str(args.get("summary") or "")[:_SUMMARY_CAP],
                "judge_input_tokens": in_tok,
                "judge_output_tokens": out_tok,
                "judge_cost_usd": _judge_cost(judge_llm, in_tok, out_tok),
                "input_capped": input_capped,
            }
        except Exception as e:  # noqa: BLE001 — the judge must not crash the request
            last_err = e
            logger.warning(f"trajectory judge attempt {attempt + 1}/2 failed for task: {e}")
    return {"status": "error", "error": str(last_err)[:300], "input_capped": input_capped}


async def evaluate_task_trajectory(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Judge ``task``'s trajectory and write the profile to its quality record.

    Returns the profile dict, or ``None`` when skipped (no judge model, or an
    empty trace with no steps to score). Re-running overwrites any existing
    profile (intentional, for on-demand re-judge). A failed LLM/parse call is
    persisted as a profile with ``status: "error"`` — not skipped.
    """
    judge_llm = await _resolve_judge_model(db, task.workspace_id)
    if judge_llm is None:
        logger.info(
            f"trajectory eval skipped — no judge/orchestrator model for task {task.id}"
        )
        return None

    cleaned_trace = await build_cleaned_trace(db, task)
    if not (cleaned_trace.get("steps") or []):
        logger.info(f"trajectory eval skipped — empty trace for task {task.id}")
        return None

    from app.api.settings import get_setting

    raw_cap = await get_setting(db, "trajectory_judge_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS)
    try:
        max_input_tokens = int(raw_cap)
    except (TypeError, ValueError):
        max_input_tokens = DEFAULT_MAX_INPUT_TOKENS

    result = await _judge_trajectory(cleaned_trace, judge_llm, max_input_tokens=max_input_tokens)

    # Deterministic loop detector (SPA-75): counts repeated tool-calls structurally
    # over the full, untrimmed step LIST (no step dropped — unlike the judge, which
    # scores the budget-trimmed trace; per-step content may be output-capped by E-06,
    # which doesn't affect duplicate detection). An LLM-free anchor for the LLM's
    # `loop_detected` axis; runs even when the judge errors, so it is always present.
    loop_analysis = detect_loops(cleaned_trace.get("steps") or [])

    stats = cleaned_trace.get("stats") or {}
    profile = {
        "schema_version": TRAJECTORY_SCHEMA_VERSION,
        # What the judge was asked, so an edited prompt cannot be mistaken for
        # the one that produced a stored score (SPA-85).
        "prompt_fingerprint": trajectory_prompt_fingerprint(),
        "status": result.get("status"),
        "axes": result.get("axes", []),
        "overall_score": result.get("overall_score"),
        "loop_detected": result.get("loop_detected", False),
        "loop_analysis": loop_analysis,
        "summary": result.get("summary", ""),
        "judge_model": judge_llm.model.api_name,
        "judge_input_tokens": result.get("judge_input_tokens", 0),
        "judge_output_tokens": result.get("judge_output_tokens", 0),
        "judge_cost_usd": result.get("judge_cost_usd", 0.0),
        "input_capped": result.get("input_capped", False),
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

    # Ensure the quality record exists (E-01), then write the trajectory slot.
    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)
    if record is not None:
        record.trajectory_profile = profile

    await log_event(
        db,
        "trajectory_evaluated",
        "system",
        {
            "overall_score": profile["overall_score"],
            "loop_detected": profile["loop_detected"],
            "status": profile["status"],
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
