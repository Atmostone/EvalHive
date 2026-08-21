"""Deterministic loop / repetition detection over a cleaned trace (E-07 anchor).

The E-07 trajectory judge scores a ``loop_detection`` axis, but that score is the
LLM's freestyle opinion over a *budget-trimmed* trace — and the project's own
calibration found that axis the most miscalibrated (judge mean 4.38 vs human
8.13). This module is the deterministic, LLM-free counterpart: it **counts**
actual repetition structurally over the full, untrimmed step LIST (no step is
dropped before counting — unlike the judge, which scores the budget-trimmed
trace; note each step's *content* may already be output-capped by E-06, which
does not affect duplicate detection — identity is taken from the call, not the
output). The report shows the counted loop rate next to the judge's opinion and
uses the counted signal to anchor the LLM axis.

This is a **precision-oriented structural lower bound over tool-calls**, not a
universal loop oracle: it counts only tool actions (reasoning loops are out of
scope) and catches exact / cyclic repetition, so it may *under-count* semantic
loops that vary their wording. It is built to be confident when it fires, and to
be the floor the LLM axis is measured against.

Two complementary structural signals over the agent's tool-call sequence:

1. **Exact stuck-repeat** — the longest run of *consecutive identical actions*
   (same tool_name AND byte-identical arguments up to key order), e.g. the same
   failing call retried verbatim. Argument-aware, so genuine progress (same tool,
   different parameters) is not flagged.
2. **Tandem tool cycles** — a contiguous repeated n-gram over the tool-name
   sequence with period ≥ 2, e.g. ``search → click → search → click → search →
   click``. Found by a phase-aware global scan (every (period, start)), so a
   cycle that begins after an unaligned prefix is not missed.

Pure and total: ``detect_loops`` never raises (mirrors ``trace_cleaner`` /
``trajectory`` error discipline) — on any unexpected input it returns the
no-loop result. Stores only hashes/short tool names, never raw content.
"""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

LOOP_SCHEMA_VERSION = 1

# Thresholds for the boolean ``loop_detected`` (deterministic, documented, tunable
# here rather than scattered). A real pathology is either the exact same action
# ≥3× in a row, or a multi-step cycle that repeats ≥3×.
_MIN_REPEAT_RUN = 3
_MIN_CYCLE_REPEATS = 3
# Cycle search bounds over the tool-name sequence: periods 2..5, must repeat ≥2×
# to be *recorded* (loop_detected gates on _MIN_CYCLE_REPEATS separately).
_MIN_CYCLE_PERIOD = 2
_MAX_CYCLE_PERIOD = 5
_MIN_CYCLE_RECORD_REPEATS = 2
_MAX_CYCLES_KEPT = 5


def _canonical_arguments(arguments: dict) -> str:
    """Byte-exact canonical rendering of a call's arguments.

    Only key ORDER is normalized — `{a,b}` and `{b,a}` are one call. Nothing else
    is: no case folding, no whitespace collapsing, no length cap. Those are
    normalizations for prose, and arguments are not prose — `README.md` and
    `readme.md` are different files, two shell commands that diverge at character
    5000 are different commands, and two file bodies that share a long prefix are
    different writes. Folding any of them together manufactures a loop that never
    happened, in a detector whose whole value is being right when it fires.
    """
    try:
        return json.dumps(
            arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
        )
    except Exception:
        return repr(arguments)


def _action_key(tool_name: str, arguments: dict | None, index: int) -> str:
    """Identity of an action = tool name + a hash of its canonical arguments.

    An action is the **call**, not the result (SPA-86). Keying on the output — as
    this did before arguments were recorded — gets both directions wrong: two
    different calls that happen to return the same ``file not found`` counted as a
    repeat, while the same call issued twice counted as progress whenever its
    output differed.

    ``arguments is None`` means they were never recorded, which is not the same as
    a call that took none (``{}``, and two such calls really are the same action).
    Nothing establishes that two unrecorded calls were alike, and this detector is
    a precision-oriented lower bound — so such a step is given an identity unique
    to its position and can never be counted as a repeat. Silence is not evidence.
    """
    if arguments is None:
        return f"{tool_name} @{index}"
    # sha256 over the FULL rendering: the hash is what bounds the cost, so there is
    # no reason to truncate the input to it and collide distinct calls.
    h = hashlib.sha256(
        _canonical_arguments(arguments).encode("utf-8", "replace")
    ).hexdigest()[:32]
    return f"{tool_name} {h}"


def _tandem_cycles(seq: list[str]) -> tuple[list[dict], int]:
    """Maximal contiguous tandem repeats over ``seq`` (a tool-name list), period
    2..N. PHASE-AWARE GLOBAL SCAN: every (period, start) is probed and the run of
    consecutive equal period-blocks measured, so a cycle that begins after an
    unaligned prefix is NOT missed — a greedy left-anchored pass silently
    under-counts those (e.g. [x,y]+[x,y,z]*3 contains xyz*3 but greedy reports 2).

    Returns ``(cycles, max_repeats)``: the top non-overlapping cycles for display
    AND the global maximum repeat count over all candidates (the latter drives
    loop_detected, independent of the display de-overlap). A genuine multi-step
    cycle has ≥2 distinct tools in its pattern; a [fetch, fetch] "cycle" is just
    same-tool repetition, covered by max_repeat_run / max_same_tool_run."""
    n = len(seq)
    candidates: list[tuple[int, int, int, int]] = []  # (coverage, start, period, repeats)
    max_repeats = 0
    for period in range(_MIN_CYCLE_PERIOD, _MAX_CYCLE_PERIOD + 1):
        # need room for at least _MIN_CYCLE_RECORD_REPEATS blocks from `start`
        for start in range(0, n - period * _MIN_CYCLE_RECORD_REPEATS + 1):
            block = seq[start : start + period]
            if len(set(block)) < 2:
                continue
            repeats = 1
            while seq[start + repeats * period : start + (repeats + 1) * period] == block:
                repeats += 1
            if repeats >= _MIN_CYCLE_RECORD_REPEATS:
                candidates.append((repeats * period, start, period, repeats))
                max_repeats = max(max_repeats, repeats)
    # display: greedily select non-overlapping cycles by coverage (then earliest).
    candidates.sort(key=lambda c: (-c[0], c[1]))
    cycles: list[dict] = []
    covered = [False] * n
    for _cov, start, period, repeats in candidates:
        span = range(start, start + period * repeats)
        if any(covered[j] for j in span):
            continue
        for j in span:
            covered[j] = True
        cycles.append(
            {
                "pattern": seq[start : start + period],
                "period": period,
                "repeats": repeats,
                "start": start,
                "end": start + period * repeats,
            }
        )
        if len(cycles) >= _MAX_CYCLES_KEPT:
            break
    return cycles, max_repeats


def _loop_score(
    max_repeat_run: int, max_cycle_repeats: int, max_same_tool_run: int
) -> float:
    """A deterministic 0–10 loop-cleanliness score (10 = no loops), so it can sit
    next to the LLM ``loop_detection`` axis for calibration. Heuristic: the worst
    of the structural penalties. Documented, not load-bearing — the counted fields
    above are the real signal.

    NOTE: based on the *structural* repetition signals (exact stuck-repeat, tandem
    cycles, same-tool runs) — deliberately NOT on repeated_action_ratio, which
    conflates a benign revisit (reading the same config twice) with being stuck. A
    long same-tool run gets a softer, lower-weight penalty than a hard loop, since
    varying-content iteration (pagination) is weaker evidence of a loop."""
    pen_run = max(0.0, (max_repeat_run - 1) * 2.0)  # run 3 → 4, run 6 → 10
    pen_cyc = max(0.0, (max_cycle_repeats - 1) * 2.5)  # 3 reps → 5, 5 reps → 10
    pen_tool = max(0.0, (max_same_tool_run - 3) * 1.0)  # 8 same-tool calls → 5
    penalty = min(10.0, max(pen_run, pen_cyc, pen_tool))
    return round(10.0 - penalty, 1)


def _empty(n_actions: int = 0) -> dict:
    return {
        "schema_version": LOOP_SCHEMA_VERSION,
        "n_actions": n_actions,
        "max_repeat_run": 0,
        "repeated_action_ratio": 0.0,
        "max_same_tool_run": 0,
        "max_cycle_repeats": 0,
        "cycles": [],
        "loop_detected": False,
        "loop_score": 10.0,
    }


def detect_loops(steps: list[dict] | None) -> dict:
    """Count structural repetition over a cleaned trace's step list (E-06 output,
    BEFORE any judge-budget trimming). Considers only tool actions (kind=="tool"
    with a tool_name); reasoning/agent steps don't "loop" in the actionable sense.

    Returns a dict with the counted signals and a boolean ``loop_detected``. Never
    raises — any malformed input yields the no-loop result."""
    try:
        tool_steps = [
            s
            for s in (steps or [])
            if isinstance(s, dict) and s.get("kind") == "tool" and s.get("tool_name")
        ]
        n = len(tool_steps)
        if n == 0:
            return _empty(0)

        tool_seq = [str(s.get("tool_name")) for s in tool_steps]
        action_keys = [
            _action_key(str(s.get("tool_name")), s.get("arguments"), i)
            for i, s in enumerate(tool_steps)
        ]
        # SPA-113: the same call in attempt 1 and attempt 2 is a RETRY, not a loop.
        # The attempt index is folded into the identity so a repetition across the
        # boundary cannot be counted as one within a run — the counter answers «did
        # this agent go in circles», and starting over is the opposite of that.
        attempts = [int(s.get("attempt") or 1) for s in tool_steps]
        _seg_attempt: list[int] = []
        if len(set(attempts)) > 1:
            action_keys = [f"a{a}|{k}" for a, k in zip(attempts, action_keys)]

        # 1) longest run of consecutive IDENTICAL actions (tool + content).
        max_repeat_run = 1
        run = 1
        for a, b in zip(action_keys, action_keys[1:]):
            run = run + 1 if a == b else 1
            max_repeat_run = max(max_repeat_run, run)

        # Everything below reads a SEQUENCE, and a sequence that spans an attempt
        # boundary is two sequences read as one. Folding the attempt into the
        # action identity (above) fixed the identical-action run and nothing else:
        # `search click search` followed by `click search click` concatenates into
        # a (search, click) cycle repeated three times, so two ordinary retries
        # were being reported as a loop — the exact reading SPA-113 set out to
        # stop. Per-attempt segments, maximum across them.
        segments: list[list[str]] = []
        for tool, attempt in zip(tool_seq, attempts):
            if not segments or attempt != _seg_attempt[-1]:
                segments.append([])
                _seg_attempt.append(attempt)
            segments[-1].append(tool)

        # longest run of the same TOOL (content may differ) — context, not a trigger.
        max_same_tool_run = 1
        for seg in segments:
            run = 1
            for a, b in zip(seg, seg[1:]):
                run = run + 1 if a == b else 1
                max_same_tool_run = max(max_same_tool_run, run)

        # 2) fraction of actions that exactly duplicate an earlier action.
        seen: set[str] = set()
        dup = 0
        for k in action_keys:
            if k in seen:
                dup += 1
            else:
                seen.add(k)
        repeated_action_ratio = round(dup / n, 4) if n else 0.0

        # 3) multi-step tandem cycles over the tool-name sequence (period ≥ 2),
        # WITHIN an attempt. max_cycle_repeats is the global max over ALL
        # candidates (phase-aware), not just the de-overlapped display cycles.
        cycles: list[dict] = []
        max_cycle_repeats = 0
        for seg in segments:
            seg_cycles, seg_max = _tandem_cycles(seg)
            cycles.extend(seg_cycles)
            max_cycle_repeats = max(max_cycle_repeats, seg_max)

        loop_detected = (
            max_repeat_run >= _MIN_REPEAT_RUN or max_cycle_repeats >= _MIN_CYCLE_REPEATS
        )
        return {
            "schema_version": LOOP_SCHEMA_VERSION,
            "n_actions": n,
            "max_repeat_run": max_repeat_run,
            "repeated_action_ratio": repeated_action_ratio,
            "max_same_tool_run": max_same_tool_run,
            "max_cycle_repeats": max_cycle_repeats,
            "cycles": cycles,
            "loop_detected": loop_detected,
            "loop_score": _loop_score(
                max_repeat_run, max_cycle_repeats, max_same_tool_run
            ),
        }
    except Exception as e:  # noqa: BLE001 — deterministic detector must never crash eval
        logger.warning(f"detect_loops failed, returning no-loop result: {e}")
        return _empty(0)
