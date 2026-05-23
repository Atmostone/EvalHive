"""Reference-based evaluation (E-03).

Scores an agent's task result against a gold ``reference_answer`` and returns a
single 0-10 dimension that is folded into the E-02 quality profile. Four modes:

- ``pointwise`` — an LLM judge scores the result against the reference (uses the
  ``quality_judge`` model, like E-02).
- ``exact`` — 10 iff the normalized result equals the normalized reference, else 0.
- ``fuzzy`` — ``difflib`` similarity ratio (stdlib, no extra dependency) × 10.
- ``semantic`` — cosine similarity of embeddings (configured embedding provider) × 10.

Pairwise (A vs B vs reference) is deferred — it needs a second candidate result
(E-21 Elo / E-11 variance) that a single task does not provide.

Like ``_judge_dimension`` in :mod:`app.quality.judge`, ``evaluate_reference_dimension``
never raises: a missing reference yields ``status: "skipped"`` and any failure
yields ``status: "error"`` so one dimension can never block the others.
"""

from __future__ import annotations

import json
import logging
import math
import re
from difflib import SequenceMatcher

from app.plugins.llm import get_llm_provider
from app.models.task import Task
from app.quality.judge import JUDGE_TOOL, _MAX_SCALE, _tokens_from_response

logger = logging.getLogger(__name__)

REFERENCE_MODES = ("pointwise", "exact", "fuzzy", "semantic")
DEFAULT_REFERENCE_MODE = "pointwise"


def _normalize(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def exact_match(result: str, reference: str) -> float:
    """1.0 if the normalized texts are identical, else 0.0."""
    return 1.0 if _normalize(result) == _normalize(reference) else 0.0


def fuzzy_match(result: str, reference: str) -> float:
    """Character-level similarity ratio in [0, 1] (stdlib difflib)."""
    return SequenceMatcher(None, _normalize(result), _normalize(reference)).ratio()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


async def semantic_match(result: str, reference: str) -> float:
    """Cosine similarity of the two texts' embeddings, clamped to [0, 1]."""
    from app.knowledge.rag import get_embeddings

    vecs = await get_embeddings([result, reference])
    sim = _cosine(vecs[0], vecs[1])
    return max(0.0, min(1.0, sim))


def _scored(similarity: float, reasoning: str) -> dict:
    return {
        "status": "scored",
        "score": int(round(similarity * _MAX_SCALE)),
        "reasoning": reasoning,
        "input_tokens": 0,
        "output_tokens": 0,
    }


async def _pointwise_judge(result: str, reference: str, dim: dict, judge_llm) -> dict:
    """LLM judge: score the result against the reference on this dimension."""
    name = dim.get("name") or dim.get("key")
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict, fair quality judge. Compare the agent's RESULT "
                "against the REFERENCE (gold) answer and score how well the result "
                "matches the reference on the named dimension, from 0 to 10. Use the "
                "score_dimension tool. 10 = matches the reference, 5 = partially, "
                "0 = wrong or absent. Equivalent wording counts as a match."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Dimension: {name}\n"
                f"What it measures: {dim.get('description') or name}\n\n"
                f"REFERENCE (gold) answer:\n{reference}\n\n"
                f"Agent RESULT:\n{result or '(empty)'}"
            ),
        },
    ]
    resp = await get_llm_provider().acompletion(
        model=judge_llm.model.api_name,
        messages=messages,
        tools=JUDGE_TOOL,
        tool_choice={"type": "function", "function": {"name": "score_dimension"}},
        api_key=judge_llm.provider.api_key,
        api_base=judge_llm.provider.endpoint,
    )
    args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
    score = max(0, min(_MAX_SCALE, int(args["score"])))
    inp, out = _tokens_from_response(resp)
    return {
        "status": "scored",
        "score": score,
        "reasoning": str(args.get("reasoning") or "")[:1000],
        "input_tokens": inp,
        "output_tokens": out,
    }


async def evaluate_reference_dimension(dim: dict, task: Task, judge_llm) -> dict:
    """Score one ``reference`` dimension. Never raises — errors become a result dict.

    Returns the same shape as ``judge._judge_dimension`` plus a ``skipped`` status
    when the task has no ``reference_answer``.
    """
    try:
        reference = (task.reference_answer or "").strip()
        if not reference:
            return {"status": "skipped", "score": None}

        result = task.result_summary or ""
        mode = dim.get("reference_mode") or DEFAULT_REFERENCE_MODE

        if mode == "pointwise":
            if judge_llm is None:
                return {
                    "status": "error",
                    "score": None,
                    "error": "no judge model for pointwise reference",
                }
            return await _pointwise_judge(result, reference, dim, judge_llm)
        if mode == "exact":
            sim = exact_match(result, reference)
            return _scored(sim, f"exact match: {'yes' if sim else 'no'}")
        if mode == "fuzzy":
            sim = fuzzy_match(result, reference)
            return _scored(sim, f"fuzzy ratio {sim:.2f}")
        if mode == "semantic":
            sim = await semantic_match(result, reference)
            return _scored(sim, f"cosine similarity {sim:.2f}")
        return {"status": "error", "score": None, "error": f"unknown reference_mode '{mode}'"}
    except Exception as e:  # noqa: BLE001 — one dimension must not break the rest
        logger.warning(f"reference dimension '{dim.get('key')}' failed: {e}")
        return {"status": "error", "score": None, "error": str(e)[:300]}
