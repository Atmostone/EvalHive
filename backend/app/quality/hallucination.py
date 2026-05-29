"""Hallucination Detection (E-15).

Agents fabricate things: URLs that were never fetched, library/API symbols that
do not exist, numbers with no source, and claims with no citation. Outcome
scoring (E-02) and the trajectory judge (E-07) do not see this — a run can be
judged "correct" while its written deliverable invents a reference. E-15 is a
**fact-checker over the finished run's deliverable** (``task.result_summary``)
across four categories — ``urls`` / ``apis`` / ``numbers`` / ``citations`` —
writing a per-category breakdown plus a top-level ``hallucination_rate`` to
``quality_records.hallucination_profile`` (next to the other quality slots).

The pipeline is hybrid:

- **URLs** — deterministic, *in-trace only*: a URL is "supported" iff it actually
  appears in some tool argument/result of the E-06 cleaned trace. If the agent
  wrote a URL into the deliverable that no tool ever fetched, it is unsupported.
  No live HTTP is performed (no network dependency, no SSRF surface).
- **APIs** — hybrid: dotted ``pkg.func(`` symbols inside code fences are first
  matched against the trace (supported if seen). The *unconfirmed* ones are sent
  to the LLM for a plausibility verdict (it knows public libraries).
- **numbers / citations** — a single LLM call decides, for each extracted
  numeric token / claim sentence, whether the trace supports it.

So there is at most ONE LLM call per task (skipped entirely when the deterministic
pass leaves nothing to ask). Consistent with the rest of ``app.quality``: model
selection reuses E-02's resolver (`quality_judge` → `orchestrator`), the input is
bounded by ``hallucination_judge_max_input_tokens``, existing E-02/E-08 profiles
are read as-is (never re-run), and the judge never raises — an LLM/parse failure
becomes ``status: "error"`` instead of an exception. Grouping by model/template
(:func:`aggregate_hallucinations`) gives the per-category hallucination rate.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.plugins.llm import get_llm_provider
from app.quality.capability import DEFAULT_OUTCOME_THRESHOLD, _outcome_from_profile
from app.quality.judge import _judge_cost, _resolve_judge_model, _tokens_from_response
from app.quality.trace_cleaner import _count_tokens, build_cleaned_trace
from app.quality.trajectory import _fit_trace_to_budget
from app.utils.events import log_event

logger = logging.getLogger(__name__)

HALLUCINATION_SCHEMA_VERSION = 1
# Default cap on the judge's input tokens per task; overridable via the
# `hallucination_judge_max_input_tokens` setting (acceptance: cost cap).
DEFAULT_MAX_INPUT_TOKENS = 12000
# The four fact-check categories (acceptance: 4 categories of checks).
CATEGORIES = ("urls", "apis", "numbers", "citations")

_RESULT_CAP = 8000  # chars of the deliverable fed to extraction/LLM
_ITEM_CAP = 50  # max items kept per category (profile size guard)
_REASON_CAP = 500
_CLAIM_CAP = 300
_SUMMARY_CAP = 1000
_MAX_NUMBER_CANDIDATES = 80
_MAX_CLAIM_CANDIDATES = 30
_MIN_CLAIM_LEN = 30

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]}\"'<]+", re.IGNORECASE)
# Dotted symbol immediately followed by a call: `fastapi.middleware.X(`.
_API_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\s*\(")
# Fenced code (``` ... ```), or inline `code`.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# Numeric tokens, optional thousands/decimals and a trailing percent sign.
_NUMBER_RE = re.compile(r"\d[\d.,]*\s?%?")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。!?])\s+|\n{2,}")
_TRAILING_PUNCT = ".,;:!?)]}>\"'»"


# ---------------------------------------------------------------------------
# Deterministic extraction
# ---------------------------------------------------------------------------
def _dedup(seq: list[str]) -> list[str]:
    """Order-preserving de-duplication."""
    return list(dict.fromkeys(seq))


def _extract_urls(text: str) -> list[str]:
    """All distinct URLs in ``text`` (http/https or bare www.), trimmed."""
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(_TRAILING_PUNCT)
        if len(url) > len("www."):
            out.append(url)
    return _dedup(out)


def _code_regions(text: str) -> str:
    """Concatenated code-fence + inline-code regions of ``text`` (where API
    calls live). Keeps API extraction out of ordinary prose, cutting noise."""
    parts = _FENCE_RE.findall(text or "")
    parts += _INLINE_CODE_RE.findall(text or "")
    return "\n".join(parts)


def _extract_api_symbols(text: str) -> list[str]:
    """Distinct dotted ``pkg.func`` call symbols found inside code regions."""
    code = _code_regions(text)
    return _dedup(m.group(1) for m in _API_RE.finditer(code))


def _is_year(token: str) -> bool:
    t = token.strip().rstrip("%").replace(" ", "")
    return t.isdigit() and len(t) == 4 and 1900 <= int(t) <= 2100


def _extract_numbers(text: str) -> list[str]:
    """Distinct numeric tokens worth fact-checking.

    Drops bare single digits (0-9) and 4-digit years — neither is a meaningful
    fabricated statistic. Keeps percentages, decimals and multi-digit figures."""
    out: list[str] = []
    for m in _NUMBER_RE.finditer(text or ""):
        tok = m.group(0).strip()
        if not tok:
            continue
        bare = tok.rstrip("%").replace(" ", "")
        if not any(ch.isdigit() for ch in bare):
            continue
        # Drop trivially-uninformative single digits without a percent sign.
        if "%" not in tok and "." not in bare and "," not in bare and len(bare) <= 1:
            continue
        if _is_year(tok):
            continue
        out.append(tok)
        if len(out) >= _MAX_NUMBER_CANDIDATES:
            break
    return _dedup(out)


def _extract_claims(text: str) -> list[str]:
    """Sentence-level factual claims from the deliverable (for citation checks).

    Code fences are stripped first so claims are prose, not code."""
    prose = _FENCE_RE.sub(" ", text or "")
    out: list[str] = []
    for raw in _SENTENCE_SPLIT_RE.split(prose):
        s = raw.strip()
        if len(s) < _MIN_CLAIM_LEN:
            continue
        if not any(ch.isalpha() for ch in s):
            continue
        out.append(s[:_CLAIM_CAP])
        if len(out) >= _MAX_CLAIM_CANDIDATES:
            break
    return _dedup(out)


def _corpus_from_trace(cleaned_trace: dict) -> str:
    """Lower-cased concatenation of all step contents — the in-trace ground
    truth a URL or symbol must appear in to count as supported."""
    parts = [
        str(s.get("content") or "")
        for s in (cleaned_trace.get("steps") or [])
    ]
    return "\n".join(parts).lower()


def _url_supported(url: str, corpus: str) -> bool:
    """A URL is supported iff it (or its scheme-less form) appears in the trace."""
    u = url.lower().rstrip("/")
    if u in corpus:
        return True
    bare = re.sub(r"^https?://", "", u)
    return bool(bare) and bare in corpus


def _symbol_supported(symbol: str, corpus: str) -> bool:
    return symbol.lower() in corpus


# ---------------------------------------------------------------------------
# LLM fact-check (numbers / citations / uncertain APIs)
# ---------------------------------------------------------------------------
HALLUCINATION_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "classify_hallucinations",
            "description": (
                "Fact-check the agent's deliverable against the trace. For every "
                "candidate you are given, decide whether it is SUPPORTED by the "
                "trace (or, for APIs, by well-known public libraries). Anything not "
                "supported is a hallucination. Do not invent candidates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "apis": {
                        "type": "array",
                        "description": "Verdicts for the API/library symbols listed.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string", "description": "The symbol, verbatim."},
                                "supported": {"type": "boolean"},
                                "reason": {"type": "string", "description": "One sentence."},
                            },
                            "required": ["value", "supported", "reason"],
                        },
                    },
                    "numbers": {
                        "type": "array",
                        "description": "Verdicts for the numeric tokens listed.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string", "description": "The number, verbatim."},
                                "supported": {"type": "boolean"},
                                "reason": {"type": "string", "description": "One sentence."},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["value", "supported", "reason", "confidence"],
                        },
                    },
                    "citations": {
                        "type": "array",
                        "description": "Verdicts for the factual claims listed.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string", "description": "The claim, verbatim or trimmed."},
                                "supported": {"type": "boolean"},
                                "reason": {"type": "string", "description": "One sentence."},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["claim", "supported", "reason", "confidence"],
                        },
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line overall assessment (or 'clean').",
                    },
                },
                "required": ["apis", "numbers", "citations", "summary"],
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


def _summarize_outcome(outcome_profile: dict | None) -> str | None:
    """One-line E-02 outcome context for the fact-checker, or None when absent."""
    correct, signal, score = _outcome_from_profile(
        outcome_profile, DEFAULT_OUTCOME_THRESHOLD
    )
    if signal == "none":
        return None
    verdict = "correct" if correct else "incorrect"
    score_txt = f", score {score}" if score is not None else ""
    return f"Outcome judged {verdict} (signal: {signal}{score_txt})."


def _facts_from_evidence(evidence_profile: dict | None) -> list[str]:
    """Curated facts the trajectory actually established (E-08), as grounding."""
    if not isinstance(evidence_profile, dict) or evidence_profile.get("status") == "error":
        return []
    facts: list[str] = []
    for rec in evidence_profile.get("evidence_bank") or []:
        for f in rec.get("facts") or []:
            f = str(f).strip()
            if f:
                facts.append(f)
    return facts[:50]


def _build_llm_input(
    result_text: str,
    outcome_profile: dict | None,
    evidence_profile: dict | None,
    cleaned_trace: dict,
    *,
    uncertain_apis: list[str],
    numbers: list[str],
    claims: list[str],
    max_input_tokens: int,
) -> tuple[str, bool, bool, bool]:
    """Assemble the fact-checker's text input. Returns
    (text, input_capped, used_outcome, used_evidence)."""
    outcome_line = _summarize_outcome(outcome_profile)
    facts = _facts_from_evidence(evidence_profile)

    head_parts = [f"DELIVERABLE (the agent's result):\n{result_text}"]
    if outcome_line:
        head_parts.append(f"\nOUTCOME: {outcome_line}")
    if facts:
        head_parts.append(
            "\nTRACE FACTS (established by the trajectory):\n"
            + "\n".join(f"- {f}" for f in facts)
        )
    head_parts.append("\nCANDIDATES TO FACT-CHECK:")
    if uncertain_apis:
        head_parts.append("APIs:\n" + "\n".join(f"- {a}" for a in uncertain_apis))
    if numbers:
        head_parts.append("Numbers:\n" + "\n".join(f"- {n}" for n in numbers))
    if claims:
        head_parts.append("Claims:\n" + "\n".join(f"- {c}" for c in claims))
    head_block = "\n".join(head_parts) + "\n\n"

    remaining = max(200, max_input_tokens - _count_tokens(head_block))
    trace_text, input_capped = _fit_trace_to_budget(cleaned_trace, remaining)
    text = head_block + "TRACE (ground truth):\n" + trace_text
    return text, input_capped, outcome_line is not None, bool(facts)


async def _classify_with_llm(
    result_text: str,
    outcome_profile: dict | None,
    evidence_profile: dict | None,
    cleaned_trace: dict,
    judge_llm,
    *,
    uncertain_apis: list[str],
    numbers: list[str],
    claims: list[str],
    max_input_tokens: int,
) -> dict:
    """Run the single fact-check LLM call. Never raises — failures return a dict
    with ``status: "error"``."""
    serialized, input_capped, used_outcome, used_evidence = _build_llm_input(
        result_text,
        outcome_profile,
        evidence_profile,
        cleaned_trace,
        uncertain_apis=uncertain_apis,
        numbers=numbers,
        claims=claims,
        max_input_tokens=max_input_tokens,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a strict, fair fact-checker of an AI agent's written "
                "deliverable. The TRACE is the ground truth of what the agent "
                "actually retrieved. For each candidate you are given, decide if it "
                "is SUPPORTED. A number/claim is supported only if the trace backs "
                "it up; an API symbol is supported if it exists in a well-known "
                "public library OR appears in the trace. Mark anything fabricated as "
                "not supported. Judge ONLY the candidates listed — never add new "
                "ones. Use the classify_hallucinations tool."
            ),
        },
        {"role": "user", "content": serialized},
    ]
    try:
        resp = await get_llm_provider().acompletion(
            model=judge_llm.model.api_name,
            messages=messages,
            tools=HALLUCINATION_TOOL,
            tool_choice={"type": "function", "function": {"name": "classify_hallucinations"}},
            api_key=judge_llm.provider.api_key,
            api_base=judge_llm.provider.endpoint,
        )
        choice = resp.choices[0].message
        args = json.loads(choice.tool_calls[0].function.arguments)
        in_tok, out_tok = _tokens_from_response(resp)
        return {
            "status": "scored",
            "args": args if isinstance(args, dict) else {},
            "summary": str((args or {}).get("summary") or "")[:_SUMMARY_CAP],
            "judge_input_tokens": in_tok,
            "judge_output_tokens": out_tok,
            "judge_cost_usd": _judge_cost(judge_llm, in_tok, out_tok),
            "input_capped": input_capped,
            "used_outcome_profile": used_outcome,
            "used_trajectory_evidence": used_evidence,
        }
    except Exception as e:  # noqa: BLE001 — the judge must not crash the request
        logger.warning(f"hallucination fact-check failed for task: {e}")
        return {
            "status": "error",
            "error": str(e)[:300],
            "input_capped": input_capped,
            "used_outcome_profile": used_outcome,
            "used_trajectory_evidence": used_evidence,
        }


def _verdict_map(items, key: str) -> dict[str, dict]:
    """Index an LLM verdict list by its ``value``/``claim`` field (lower-cased)."""
    out: dict[str, dict] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        v = str(it.get(key) or "").strip()
        if v:
            out[v.lower()] = it
    return out


def _llm_items_for(
    candidates: list[str],
    verdicts: dict[str, dict],
    *,
    key: str,
    with_confidence: bool,
) -> list[dict]:
    """Build the *hallucinated* items for a candidate list from LLM verdicts.

    A candidate is flagged only when the LLM explicitly returned a verdict with
    ``supported=False`` for it (no verdict → benefit of the doubt, not flagged)."""
    items: list[dict] = []
    for cand in candidates:
        v = verdicts.get(cand.lower())
        if v is None or bool(v.get("supported", True)):
            continue
        item = {
            key: cand[:_CLAIM_CAP if key == "claim" else _REASON_CAP],
            "kind": "llm",
            "supported": False,
            "reason": str(v.get("reason") or "")[:_REASON_CAP],
        }
        if with_confidence:
            item["confidence"] = _clamp_confidence(v.get("confidence"))
        items.append(item)
        if len(items) >= _ITEM_CAP:
            break
    return items


def _category(checked: int, items: list[dict]) -> dict:
    return {"checked": checked, "hallucinated": len(items), "items": items[:_ITEM_CAP]}


async def evaluate_task_hallucinations(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Fact-check ``task``'s deliverable and write the hallucination profile.

    Returns the profile dict, or ``None`` when skipped (no judge model, no
    deliverable text, or an empty trace). URLs/known APIs are checked
    deterministically against the trace; the remaining numbers/claims/uncertain
    APIs go to a single LLM call. Reads existing E-02/E-08 profiles as grounding
    but never re-runs them. A failed LLM/parse is persisted with
    ``status: "error"`` — not skipped.
    """
    judge_llm = await _resolve_judge_model(db, task.workspace_id)
    if judge_llm is None:
        logger.info(
            f"hallucination eval skipped — no judge/orchestrator model for task {task.id}"
        )
        return None

    result_text = (task.result_summary or "").strip()[:_RESULT_CAP]
    if not result_text:
        logger.info(f"hallucination eval skipped — no result deliverable for task {task.id}")
        return None

    cleaned_trace = await build_cleaned_trace(db, task)
    if not (cleaned_trace.get("steps") or []):
        logger.info(f"hallucination eval skipped — empty trace for task {task.id}")
        return None

    from app.api.settings import get_setting

    raw_cap = await get_setting(
        db, "hallucination_judge_max_input_tokens", DEFAULT_MAX_INPUT_TOKENS
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

    outcome_profile = record.quality_profile if record is not None else None
    evidence_profile = record.trajectory_evidence_profile if record is not None else None

    # --- Deterministic pass -------------------------------------------------
    corpus = _corpus_from_trace(cleaned_trace)
    urls = _extract_urls(result_text)
    symbols = _extract_api_symbols(result_text)
    numbers = _extract_numbers(result_text)
    claims = _extract_claims(result_text)

    url_items = [
        {
            "value": u,
            "kind": "deterministic",
            "supported": False,
            "reason": "URL never appears in any tool argument or result",
        }
        for u in urls
        if not _url_supported(u, corpus)
    ]
    uncertain_apis = [s for s in symbols if not _symbol_supported(s, corpus)]

    # --- LLM pass (only when there is something left to ask) ----------------
    need_llm = bool(uncertain_apis or numbers or claims)
    status = "scored"
    summary = ""
    judge_in = judge_out = 0
    judge_cost = 0.0
    input_capped = False
    used_outcome = used_evidence = False
    api_items: list[dict] = []
    number_items: list[dict] = []
    citation_items: list[dict] = []
    errors: list[dict] = []

    if need_llm:
        res = await _classify_with_llm(
            result_text,
            outcome_profile,
            evidence_profile,
            cleaned_trace,
            judge_llm,
            uncertain_apis=uncertain_apis,
            numbers=numbers,
            claims=claims,
            max_input_tokens=max_input_tokens,
        )
        status = res.get("status", "scored")
        input_capped = res.get("input_capped", False)
        used_outcome = res.get("used_outcome_profile", False)
        used_evidence = res.get("used_trajectory_evidence", False)
        judge_in = res.get("judge_input_tokens", 0)
        judge_out = res.get("judge_output_tokens", 0)
        judge_cost = res.get("judge_cost_usd", 0.0)
        if status == "error":
            errors = [{"error": res.get("error")}]
        else:
            summary = res.get("summary", "")
            args = res.get("args") or {}
            api_items = _llm_items_for(
                uncertain_apis, _verdict_map(args.get("apis"), "value"),
                key="value", with_confidence=False,
            )
            number_items = _llm_items_for(
                numbers, _verdict_map(args.get("numbers"), "value"),
                key="value", with_confidence=True,
            )
            citation_items = _llm_items_for(
                claims, _verdict_map(args.get("citations"), "claim"),
                key="claim", with_confidence=True,
            )
    else:
        used_outcome = _summarize_outcome(outcome_profile) is not None

    categories = {
        "urls": _category(len(urls), url_items),
        "apis": _category(len(symbols), api_items),
        "numbers": _category(len(numbers), number_items),
        "citations": _category(len(claims), citation_items),
    }
    items_total = sum(c["checked"] for c in categories.values())
    hallucination_count = sum(c["hallucinated"] for c in categories.values())
    rate = round(hallucination_count / items_total, 4) if items_total else 0.0

    stats = cleaned_trace.get("stats") or {}
    profile = {
        "schema_version": HALLUCINATION_SCHEMA_VERSION,
        "status": status,
        "categories": categories,
        "hallucination_count": hallucination_count,
        "items_total": items_total,
        "hallucination_rate": rate,
        "summary": summary,
        "judge_model": judge_llm.model.api_name,
        "judge_input_tokens": judge_in,
        "judge_output_tokens": judge_out,
        "judge_cost_usd": judge_cost,
        "input_capped": input_capped,
        "used_outcome_profile": used_outcome,
        "used_trajectory_evidence": used_evidence,
        "trace_stats": {
            "original_tokens": stats.get("original_tokens"),
            "cleaned_tokens": stats.get("cleaned_tokens"),
            "steps_total": stats.get("steps_total"),
        },
        "evaluated_at": datetime.utcnow().isoformat(),
        "errors": errors,
    }

    if record is not None:
        record.hallucination_profile = profile

    await log_event(
        db,
        "hallucinations_evaluated",
        "system",
        {
            "status": status,
            "hallucination_rate": rate,
            "hallucination_count": hallucination_count,
            "by_category": {k: v["hallucinated"] for k, v in categories.items()},
            "judge_model": judge_llm.model.api_name,
            "judge_cost_usd": judge_cost,
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return profile


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def _blank_hallucination_counts() -> dict:
    return {
        "runs_total": 0,
        "hallucinated_runs": 0,
        "by_category": {c: {"checked": 0, "hallucinated": 0} for c in CATEGORIES},
    }


def _with_rates(b: dict) -> dict:
    total = b["runs_total"]
    by_cat = {
        c: {
            **v,
            "rate": round(v["hallucinated"] / v["checked"], 4) if v["checked"] else None,
        }
        for c, v in b["by_category"].items()
    }
    return {
        **b,
        "hallucinated_run_rate": (
            round(b["hallucinated_runs"] / total, 4) if total else None
        ),
        "by_category": by_cat,
    }


async def aggregate_hallucinations(
    db: AsyncSession,
    *,
    workspace_id,
    model_used: str | None = None,
    template_id=None,
    category: str | None = None,
    suite: str | None = None,
) -> dict:
    """Aggregate hallucination profiles across a workspace.

    For each scored run, every category's ``checked``/``hallucinated`` counts are
    summed and ``hallucinated_runs`` counts runs with ≥1 hallucination. Breakdowns
    by category, model and template give the "hallucination rate per (model,
    template)" signal. ``category`` narrows the population to runs with ≥1
    hallucination in that category; ``suite`` restricts to one Benchmark Case
    Store suite. Per-category ``rate`` is hallucinated / checked within the bucket.
    """
    q = select(QualityRecord).where(
        QualityRecord.workspace_id == workspace_id,
        QualityRecord.hallucination_profile.isnot(None),
    )
    if model_used:
        q = q.where(QualityRecord.model_used == model_used)
    if template_id is not None:
        q = q.where(QualityRecord.template_id == template_id)
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    rows = (await db.execute(q)).scalars().all()

    overall = _blank_hallucination_counts()
    by_category: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_template: dict[str, dict] = {}

    for r in rows:
        prof = r.hallucination_profile or {}
        if prof.get("status") != "scored":
            continue
        cats = prof.get("categories") or {}
        if category and not (cats.get(category) or {}).get("hallucinated"):
            continue
        model = r.model_used or "unknown"
        tmpl = r.template_name or (str(r.template_id) if r.template_id else "unknown")
        has_hallucination = bool(prof.get("hallucination_count"))
        buckets = [
            overall,
            by_model.setdefault(model, _blank_hallucination_counts()),
            by_template.setdefault(tmpl, _blank_hallucination_counts()),
        ]
        # A per-category bucket for the categories with ≥1 hallucination on this run.
        for c in CATEGORIES:
            if (cats.get(c) or {}).get("hallucinated"):
                buckets.append(by_category.setdefault(c, _blank_hallucination_counts()))
        for bucket in buckets:
            bucket["runs_total"] += 1
            if has_hallucination:
                bucket["hallucinated_runs"] += 1
            for c in CATEGORIES:
                cb = cats.get(c) or {}
                bucket["by_category"][c]["checked"] += int(cb.get("checked") or 0)
                bucket["by_category"][c]["hallucinated"] += int(cb.get("hallucinated") or 0)

    return {
        "workspace_id": str(workspace_id),
        "filters": {
            "model_used": model_used,
            "template_id": str(template_id) if template_id else None,
            "category": category,
            "suite": suite,
        },
        **_with_rates(overall),
        "by_category": {k: _with_rates(v) for k, v in sorted(by_category.items())},
        "by_model": {k: _with_rates(v) for k, v in sorted(by_model.items())},
        "by_template": {k: _with_rates(v) for k, v in sorted(by_template.items())},
    }
