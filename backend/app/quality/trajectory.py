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
from app.quality.judge import _resolve_judge_model
from app.utils.cost import llm_call_cost, tokens_from_response
from app.utils.tool_args import error_class, extract_tool_args
from app.quality.trace_cleaner import (
    _count_tokens,
    _ERROR_RE,
    _truncate_to_tokens,
    build_cleaned_trace,
    render_arguments,
    TraceCleanerConfig,
)
from app.quality.trace_loops import detect_loops
from app.utils.events import log_event

logger = logging.getLogger(__name__)

# v2: loop_analysis (deterministic loop detector, E-07 anchor)
# v3: prompt_fingerprint — the conditions the verdict was obtained under (SPA-85)
# v4: tool-call arguments in the trace + the `trim` policy the verdict was
#     obtained under (SPA-86)
# v5: reasoning_shown / n_reasoning_steps — whether the judge saw the model's own
#     deliberation, which changes what the score means and is therefore a
#     condition of the verdict, not a detail of it (SPA-114)
TRAJECTORY_SCHEMA_VERSION = 5
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


def _serialize_trace(cleaned_trace: dict, steps: list[dict] | None = None) -> str:
    """Render a cleaned trace (E-06 dict) as the judge's text input.

    A tool step is rendered as the CALL — name and arguments — followed by its
    output, because `parameter_quality` and `tool_selection` are questions about
    the call, and until SPA-86 the judge was shown only the name and the result.

    A trace spanning several attempts carries `kind == "attempt"` boundary steps
    (SPA-113). They are rendered as a plain separator line rather than a numbered
    step: they are not something the agent did, and numbering them would put a
    frame around the run inside the run.
    """
    task = cleaned_trace.get("task") or {}
    lines = [
        f"Task title: {task.get('title') or '(none)'}",
        f"Task description: {task.get('description') or '(none)'}",
        "",
        "Trajectory steps (chronological):",
    ]
    for s in cleaned_trace.get("steps") if steps is None else steps:
        if s.get("kind") == "attempt":
            lines.append((s.get("content") or "").strip())
            continue
        tool = s.get("tool_name")
        label = f"{s.get('kind')}/{tool}" if tool else str(s.get("kind"))
        trunc = " [truncated]" if s.get("truncated") else ""
        rendered_args = render_arguments(s.get("arguments"))
        args = f" args={rendered_args}" if rendered_args else ""
        if s.get("arguments_truncated"):
            args += " [args truncated]"
        if s.get("result_missing"):
            args += " [no result recorded]"
        if s.get("parts_missing"):
            args += f" [{s['parts_missing']} output part(s) missing]"
        content = (s.get("content") or "").strip()
        lines.append(f"[{s.get('seq')}] {label}{trunc}{args}: {content}")
    return "\n".join(lines)


# Below these a shrunken block stops being evidence and becomes noise, so the
# trim moves on to the next kind rather than grinding them to nothing.
_OUTPUT_FLOOR_TOKENS = 50
_REASONING_FLOOR_TOKENS = 40
# The MODEL's own deliberation (SPA-114), as distinct from the orchestrator's
# one-line rationale above. A floor of its own because it is a different animal:
# a reasoning model can spend most of its output tokens here, so a shared cap
# would let one talkative turn evict the tool calls it is meant to explain.
_MODEL_REASONING_FLOOR_TOKENS = 40


def _shrink(content: str, cap: int) -> tuple[str, bool]:
    """Cut `content` to `cap` tokens with an explicit marker. The marker names the
    budget, so a block shortened here is distinguishable from one the cleaner's
    per-output cap had already trimmed."""
    if cap <= 0 or _count_tokens(content) <= cap:
        return content, False
    head, dropped = _truncate_to_tokens(content, cap)
    return f"{head}\n…[{dropped} more tokens dropped to fit the judge budget]…", True


def _apply_caps(
    steps: list[dict],
    *,
    output_cap: int,
    error_output_cap: int,
    reasoning_cap: int,
    model_reasoning_cap: int = 0,
) -> tuple[list[dict], int, int, int]:
    """Shorten step contents per kind, always applied to the ORIGINAL steps.

    Tool calls and their arguments are never touched — only the *results* and the
    reasoning prose are negotiable. Error outputs get their own cap so the two
    concerns stay separable: `error_output_cap = 0` leaves them whole, which is how
    the first pass spares them (the same preference the cleaner's
    `keep_tail_on_error` encodes) and the second pass gives them up.

    Applying to the originals rather than to the previous round's output is what
    makes the returned counts mean «how many outputs are shrunk under this policy»
    instead of «how many changed since last time» — the latter reported 1 when two
    non-error outputs had been shrunk in an earlier pass.
    """
    out: list[dict] = []
    outputs_shrunk = 0
    reasoning_shrunk = 0
    model_reasoning_shrunk = 0
    for s in steps:
        kind = s.get("kind")
        content = s.get("content") or ""
        if kind == "tool":
            cap = error_output_cap if _ERROR_RE.search(content) else output_cap
            if not cap:
                out.append(s)
                continue
            content, changed = _shrink(content, cap)
            outputs_shrunk += int(changed)
        elif kind == "reasoning" and reasoning_cap:
            content, changed = _shrink(content, reasoning_cap)
            reasoning_shrunk += int(changed)
        elif kind == "model_reasoning" and model_reasoning_cap:
            content, changed = _shrink(content, model_reasoning_cap)
            model_reasoning_shrunk += int(changed)
        else:
            out.append(s)
            continue
        out.append({**s, "content": content, "truncated": s.get("truncated") or content != (s.get("content") or "")})
    return out, outputs_shrunk, reasoning_shrunk, model_reasoning_shrunk


def _signature_summary(steps: list[dict]) -> str:
    """`read_file×3, bash×2` — what the omitted middle actually consisted of."""
    counts: dict[str, int] = {}
    for s in steps:
        key = str(s.get("tool_name") or s.get("kind") or "step")
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(
        f"{k}×{n}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _max_content_tokens(steps: list[dict], kind: str) -> int:
    return max(
        (_count_tokens(s.get("content") or "") for s in steps if s.get("kind") == kind),
        default=0,
    )


def _max_output_tokens(steps: list[dict], *, errors: bool) -> int:
    """Largest tool output of ONE class — error-looking, or not.

    Each pass must halve from the largest output it is actually allowed to touch.
    Starting both passes from the global maximum inverted the whole order whenever
    the two classes differed in size: with a 1K ordinary output beside a 10K error,
    the first pass began at 10K, its first halving changed nothing it was permitted
    to cut, and the «halving stopped buying anything» guard exited the pass — so the
    error was sacrificed first, and the ordinary output it was supposed to outlive
    was never touched. The mirror case wasted the error pass and hard-cut instead.
    """
    return max(
        (
            _count_tokens(s.get("content") or "")
            for s in steps
            if s.get("kind") == "tool"
            and bool(_ERROR_RE.search(s.get("content") or "")) is errors
        ),
        default=0,
    )


def fit_trace_to_budget(cleaned_trace: dict, max_input_tokens: int) -> tuple[str, dict]:
    """Serialize the trace, trimming it to the judge's input budget if needed.

    A budget has to be spent on something, and what it is spent on is a condition
    of the verdict — so the order is by **value**, not by position, and every
    omission leaves a marker:

    0. **the model's own deliberation** goes first (SPA-114). It is verbose by
       construction — a reasoning model spends most of its output tokens there —
       and a tool call plus its result is denser evidence per token about how the
       agent WORKED, which is the question being asked. SPA-86 put reasoning after
       outputs, but that was decided when «reasoning» meant the orchestrator's
       one-line rationale, not a model's full thinking, and inheriting it would
       let one talkative turn evict the calls it is supposed to explain;
    1. **tool outputs** shrink next (progressively halved), error outputs last;
    2. **the orchestrator's reasoning** shrinks only once outputs are at the floor;
    3. **whole middle steps** are dropped third, and the gap marker names the tool
       signatures that went with them;
    4. a **hard tail cut** is the last resort, and it says so in the text.

    Never trimmed: the tool call and its arguments, step ordering, head and tail.
    `max_input_tokens <= 0` means no budget at all. Returns (text, trim_report).
    """
    steps = list(cleaned_trace.get("steps") or [])
    stats = cleaned_trace.get("stats") or {}
    # What the CLEANER already removed, before this function saw the trace. Without
    # it a run whose outputs were capped at 600 tokens reports «nothing removed»
    # merely because what survived happened to fit the budget — and two runs that
    # lost different amounts of evidence look identically untouched, which is the
    # one comparison this block exists to make possible.
    pre = {
        "pre_trim_outputs_truncated": int(stats.get("steps_truncated") or 0),
        "pre_trim_args_truncated": int(stats.get("steps_args_truncated") or 0),
        "pre_trim_dropped_tokens": sum(
            max(0, (s.get("original_tokens") or 0) - (s.get("kept_tokens") or 0))
            for s in steps
            if s.get("truncated")
        ),
    }
    report: dict = {
        "mode": "budget",
        "max_input_tokens": max_input_tokens,
        "capped": False,
        **pre,
        "output_cap_applied": None,
        "error_output_cap_applied": None,
        "reasoning_cap_applied": None,
        "model_reasoning_cap_applied": None,
        "outputs_shrunk": 0,
        "reasoning_shrunk": 0,
        "model_reasoning_shrunk": 0,
        "steps_omitted": 0,
        "omitted_signatures": "",
        "hard_cut_tokens": 0,
    }
    # True when ANY stage removed something — the cleaner's per-output cap counts,
    # not just the budget fit. `capped` keeps its old meaning (the budget bit) so
    # existing readers of `input_capped` are unchanged.
    report["anything_removed"] = bool(
        pre["pre_trim_outputs_truncated"] or pre["pre_trim_args_truncated"]
    )

    if not max_input_tokens or max_input_tokens <= 0:
        report["mode"] = "none"
        report["max_input_tokens"] = None
        return _serialize_trace(cleaned_trace, steps), report

    text = _serialize_trace(cleaned_trace, steps)
    if _count_tokens(text) <= max_input_tokens:
        return text, report

    report["capped"] = True
    report["anything_removed"] = True

    base = list(steps)
    out_cap = 0  # 0 = that class is untouched
    err_cap = 0
    model_reason_cap = 0

    # 0) The model's own deliberation, halved until it fits or hits the floor.
    #    First in the sacrifice order: it is the cheapest evidence per token about
    #    how the agent worked, and the most likely to be the reason the trace does
    #    not fit at all.
    cap = _max_content_tokens(base, "model_reasoning")
    while cap > _MODEL_REASONING_FLOOR_TOKENS and _count_tokens(text) > max_input_tokens:
        cap = max(_MODEL_REASONING_FLOOR_TOKENS, cap // 2)
        trimmed, _, _, n_model = _apply_caps(
            base, output_cap=0, error_output_cap=0, reasoning_cap=0,
            model_reasoning_cap=cap,
        )
        candidate = _serialize_trace(cleaned_trace, trimmed)
        if _count_tokens(candidate) >= _count_tokens(text):
            break  # halving stopped buying anything
        text = candidate
        model_reason_cap = cap
        report["model_reasoning_cap_applied"] = cap
        report["model_reasoning_shrunk"] = n_model
        steps = trimmed
    if _count_tokens(text) <= max_input_tokens:
        return text, report

    # 1) Tool outputs, halved until they fit or hit the floor. Two passes over the
    #    ORIGINAL steps: the first spares error outputs (error_output_cap = 0), the
    #    second gives them up too while holding the cap the first pass settled on.
    for is_error_pass in (False, True):
        # Halve from the largest output of THIS class, never the global maximum.
        cap = _max_output_tokens(base, errors=is_error_pass)
        while cap > _OUTPUT_FLOOR_TOKENS and _count_tokens(text) > max_input_tokens:
            cap = max(_OUTPUT_FLOOR_TOKENS, cap // 2)
            trial_out = out_cap if is_error_pass else cap
            trial_err = cap if is_error_pass else 0
            trimmed, n_out, _, _ = _apply_caps(
                base, output_cap=trial_out, error_output_cap=trial_err, reasoning_cap=0,
                model_reasoning_cap=model_reason_cap,
            )
            candidate = _serialize_trace(cleaned_trace, trimmed)
            if _count_tokens(candidate) >= _count_tokens(text):
                break  # halving stopped buying anything
            text = candidate
            out_cap, err_cap = trial_out, trial_err
            # Counted against the originals, so both passes' shrinking is included
            # rather than the later pass overwriting the earlier one's tally. The
            # two caps are reported separately because they are two decisions.
            report["outputs_shrunk"] = n_out
            report["output_cap_applied"] = out_cap or None
            report["error_output_cap_applied"] = err_cap or None
            steps = trimmed
        if _count_tokens(text) <= max_input_tokens:
            return text, report

    # 2) Reasoning, once the outputs are spent — carrying the output caps forward so
    #    this stage measures the combined policy, not reasoning in isolation.
    cap = _max_content_tokens(base, "reasoning")
    while cap > _REASONING_FLOOR_TOKENS and _count_tokens(text) > max_input_tokens:
        cap = max(_REASONING_FLOOR_TOKENS, cap // 2)
        trimmed, n_out, n_reason, _ = _apply_caps(
            base, output_cap=out_cap, error_output_cap=err_cap, reasoning_cap=cap,
            model_reasoning_cap=model_reason_cap,
        )
        candidate = _serialize_trace(cleaned_trace, trimmed)
        if _count_tokens(candidate) >= _count_tokens(text):
            break
        text = candidate
        report["reasoning_cap_applied"], report["reasoning_shrunk"] = cap, n_reason
        report["outputs_shrunk"] = n_out
        steps = trimmed
    if _count_tokens(text) <= max_input_tokens:
        return text, report

    # 3) Whole middle steps — the outcome lives in the tail, so head and tail stay.
    # An attempt boundary is never evicted (SPA-113): drop it and the retry it marks
    # silently becomes repetition inside one run, which is the reading the marker
    # exists to prevent. It costs one line, so it is never what makes a trace fit.
    omitted: list[dict] = []
    while len(steps) > 2 and _count_tokens(text) > max_input_tokens:
        victim = len(steps) // 2
        movable = [i for i in range(1, len(steps) - 1) if steps[i].get("kind") != "attempt"]
        if not movable:
            break
        victim = min(movable, key=lambda i: abs(i - victim))
        omitted.append(steps.pop(victim))
        marker = {
            "seq": "…",
            "kind": "omitted",
            "tool_name": None,
            "arguments": None,
            "truncated": True,
            "content": (
                f"[{len(omitted)} middle step(s) omitted to fit the judge token "
                f"budget: {_signature_summary(omitted)}]"
            ),
        }
        head = len(steps) // 2
        text = _serialize_trace(cleaned_trace, steps[:head] + [marker] + steps[head:])
    report["steps_omitted"] = len(omitted)
    report["omitted_signatures"] = _signature_summary(omitted)
    if _count_tokens(text) <= max_input_tokens:
        return text, report

    # 4) Last resort. It used to cut silently and set a flag; now it says so in the
    #    text the judge reads, which is the only place the judge can see it.
    marker = "\n…[trace hard-cut here to fit the judge token budget]…"
    total = _count_tokens(text)
    text, dropped = _truncate_to_tokens(text, max(1, max_input_tokens - _count_tokens(marker)))
    report["hard_cut_tokens"] = dropped or max(0, total - max_input_tokens)
    return text + marker, report


async def _judge_trajectory(cleaned_trace: dict, judge_llm, *, max_input_tokens: int) -> dict:
    """Score the whole trajectory in one LLM call. Never raises — failures become
    a result dict with ``status: "error"``."""
    serialized, trim = fit_trace_to_budget(cleaned_trace, max_input_tokens)
    input_capped = bool(trim["capped"])
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
            args = extract_tool_args(choice)
            in_tok, out_tok = tokens_from_response(resp)

            axes, overall, loop_detected = _parse_axes_from_args(args)
            return {
                "status": "scored",
                "axes": axes,
                "overall_score": overall,
                "loop_detected": loop_detected,
                "summary": str(args.get("summary") or "")[:_SUMMARY_CAP],
                "judge_input_tokens": in_tok,
                "judge_output_tokens": out_tok,
                "judge_cost_usd": llm_call_cost(judge_llm, in_tok, out_tok),
                "input_capped": input_capped,
                "trim": trim,
            }
        except Exception as e:  # noqa: BLE001 — the judge must not crash the request
            last_err = e
            logger.warning(f"trajectory judge attempt {attempt + 1}/2 failed for task: {e}")
    return {
        "status": "error",
        "error": str(last_err)[:300],
        "error_class": error_class(last_err) if last_err else "evaluation",
        "input_capped": input_capped,
        "trim": trim,
    }


async def evaluate_task_trajectory(
    db: AsyncSession,
    task: Task,
    *,
    commit: bool = True,
    trace_config: TraceCleanerConfig | None = None,
    max_input_tokens: int | None = None,
    show_reasoning: bool = True,
) -> dict | None:
    """Judge ``task``'s trajectory and write the profile to its quality record.

    Returns the profile dict, or ``None`` when skipped (no judge model, or an
    empty trace with no steps to score). Re-running overwrites any existing
    profile (intentional, for on-demand re-judge). A failed LLM/parse call is
    persisted as a profile with ``status: "error"`` — not skipped.

    ``trace_config`` and ``max_input_tokens`` override the workspace settings for
    this one evaluation — an experiment can ask for an untrimmed trace
    (``max_input_tokens=0``) without changing what every other run is judged on.

    ``show_reasoning`` decides whether the judge sees the model's own deliberation
    (SPA-114). Shown by default: `error_recovery` and `goal_alignment` are
    questions about intent, and without it the judge infers intent from tool calls
    and a final answer. The opposite case is real — private reasoning is not
    behaviour, and grading it rewards models that narrate well — so it is a
    per-experiment switch, and either way the choice is recorded in the profile as
    `reasoning_shown`. A score computed with reasoning visible is not comparable
    to one computed without, exactly like `files_only` on the outcome judge.
    """
    judge_llm = await _resolve_judge_model(db, task.workspace_id)
    if judge_llm is None:
        logger.info(
            f"trajectory eval skipped — no judge/orchestrator model for task {task.id}"
        )
        return None

    cleaned_trace = await build_cleaned_trace(db, task, config=trace_config)
    n_reasoning_steps = sum(
        1 for s in (cleaned_trace.get("steps") or []) if s.get("kind") == "model_reasoning"
    )
    if not show_reasoning:
        # Withheld from the JUDGE only — the trace itself keeps it, so the same run
        # can be re-judged under the other policy without re-running the agent.
        cleaned_trace = {
            **cleaned_trace,
            "steps": [
                s for s in (cleaned_trace.get("steps") or [])
                if s.get("kind") != "model_reasoning"
            ],
        }
    if not (cleaned_trace.get("steps") or []):
        logger.info(f"trajectory eval skipped — empty trace for task {task.id}")
        return None

    if max_input_tokens is None:
        from app.api.settings import get_setting

        raw_cap = await get_setting(
            db, "trajectory_judge_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS
        )
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
        # A protocol condition, not a task property (SPA-114) — the same shape as
        # `files_only` on the outcome judge. `n_reasoning_steps` counts what the
        # RUN had, so «hidden» and «the model never emitted any» stay distinct:
        # a non-reasoning model scores under `shown` with a count of zero.
        "reasoning_shown": bool(show_reasoning),
        "n_reasoning_steps": n_reasoning_steps,
        # What the judge was allowed to read, and what it cost to fit (SPA-86). An
        # E-07 score from an untrimmed run and one from a trimmed run answer
        # different questions; without this block they are silently comparable.
        "trim": {
            **(result.get("trim") or {}),
            **{k: v for k, v in (cleaned_trace.get("config") or {}).items()},
        },
        "trace_stats": {
            "original_tokens": stats.get("original_tokens"),
            "cleaned_tokens": stats.get("cleaned_tokens"),
            "steps_total": stats.get("steps_total"),
            "steps_truncated": stats.get("steps_truncated"),
            "steps_args_truncated": stats.get("steps_args_truncated"),
        },
        "evaluated_at": datetime.utcnow().isoformat(),
        # The classification travels with the error, or computing it was
        # pointless: an infrastructure failure that reaches the profile as bare
        # text is indistinguishable from a judge that broke on its own (SPA-111).
        "errors": (
            [{
                "error": result.get("error"),
                "error_class": result.get("error_class") or "evaluation",
            }]
            if result.get("status") == "error"
            else []
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
