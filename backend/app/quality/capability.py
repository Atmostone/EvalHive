"""Capability-isolation Tests (E-13, part A): the deterministic Glass-Box harness.

A model can produce the right answer *from its parametric memory* without calling
the tool the task actually requires — fresh data after the model's cutoff, private
data in RAG, exact arithmetic, local state (time, the user's files). The outcome
looks correct but the agent "cheated": it will fail the moment the data changes.
Pure outcome scoring (E-02) cannot see this.

A capability-isolation task carries a ``capability_spec`` naming the tool(s) it
cannot be solved without (``required_tools``) and its ``category``. This harness
then, deterministically and without an LLM of its own:

1. reads the agent's *actual* tool calls from the E-06 cleaned trace (reusing the
   E-09 :func:`extract_tool_sequence`) and checks whether the required tools were
   really used (Glass-Box matching; ``match`` = ``all`` (default) or ``any``);
2. reads whether the *outcome* was correct — the E-02 weighted score ≥ a
   configurable threshold, or, when the rubric has a scored ``reference``
   dimension (E-03), that dimension's pass (objective and preferred);
3. classifies the run into one of four cells and writes the profile to
   ``quality_records.capability_profile``:

   - ``genuine`` — correct AND the required tool was used (capability shown);
   - ``cheated`` — correct BUT the required tool was NOT used (answered from
     memory — the C1 red flag);
   - ``failed_with_tool`` — incorrect, tool used;
   - ``failed_no_tool`` — incorrect, tool not used.

``capability_score`` over a set of tasks is ``genuine / total``, and grouping by
model (:func:`aggregate_capability`) is the "compare models by capability" signal.
The correctness step reuses the workspace's already-configured judge model — no new
model is introduced. Consistent with the rest of ``app.quality``, nothing raises:
any failure becomes a profile with ``status: "error"``.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.trace_cleaner import build_cleaned_trace
from app.quality.trajectory_match import extract_tool_sequence
from app.utils.events import log_event

logger = logging.getLogger(__name__)

CAPABILITY_SCHEMA_VERSION = 1
# Soft taxonomy (§3.4 C1); other values are accepted and echoed unchanged.
CATEGORIES = ("fresh_data", "private_data", "exact_compute", "local_state")
MATCH_MODES = ("all", "any")
DEFAULT_MATCH = "all"
# Default outcome-correctness threshold on the E-02 weighted score (0..10).
# Overridable per workspace via the `capability_outcome_threshold` setting.
DEFAULT_OUTCOME_THRESHOLD = 7.0

# Classification cells.
GENUINE = "genuine"
CHEATED = "cheated"
FAILED_WITH_TOOL = "failed_with_tool"
FAILED_NO_TOOL = "failed_no_tool"
_CLASSES = (GENUINE, CHEATED, FAILED_WITH_TOOL, FAILED_NO_TOOL)


def _normalize(name) -> str:
    return str(name or "").strip().lower()


def normalize_spec(spec) -> dict | None:
    """Validate a task's ``capability_spec`` into ``{required_tools, category, match}``.

    Returns ``None`` when the spec is absent or unusable (no required tools) — the
    task is then not a capability-isolation test and evaluation is skipped.
    """
    if not isinstance(spec, dict):
        return None
    raw_tools = spec.get("required_tools")
    if not isinstance(raw_tools, list):
        return None
    tools = [str(t).strip() for t in raw_tools if str(t).strip()]
    if not tools:
        return None
    match = spec.get("match")
    if match not in MATCH_MODES:
        match = DEFAULT_MATCH
    category = spec.get("category")
    category = str(category).strip() if category else None
    return {"required_tools": tools, "category": category, "match": match}


def _expand_called(called: list[str]) -> set[str]:
    """Normalized set of called tools, plus the bare tool name of MCP-prefixed calls.

    The agent exposes an MCP server's tools as ``<server>__<tool>`` (e.g. a `web`
    server's ``web_search`` becomes ``web__web_search``). So a case can require the
    bare ``web_search`` / ``now`` and still match the prefixed name the agent emits.
    """
    out: set[str] = set()
    for c in called:
        n = _normalize(c)
        out.add(n)
        if "__" in n:
            out.add(n.split("__", 1)[1])
    return out


def tool_used(required: list[str], called: list[str], match: str) -> tuple[bool, list[str]]:
    """Glass-Box check: were the required tools actually called?

    ``all`` (default) requires every required tool to appear; ``any`` requires at
    least one. MCP-prefixed names match their bare tool name. Returns
    ``(used, missing)`` where ``missing`` lists the required tools not found.
    """
    called_set = _expand_called(called)
    missing = [t for t in required if _normalize(t) not in called_set]
    if match == "any":
        used = len(missing) < len(required)
    else:  # all
        used = not missing
    return used, missing


async def _durable_tool_names(record) -> list[str]:
    """Tool names from the E-01 quality blob's pre-compaction tool-call record.

    The blob (``record.record_s3_path``) captures ``execution.tool_calls`` while the
    log chunks still carry ``tool_name`` — so this survives log compaction, unlike
    the cleaned trace. Best-effort: returns ``[]`` on any failure."""
    if record is None or not getattr(record, "record_s3_path", None):
        return []
    try:
        import json

        from app.storage.minio_client import read_quality_record

        blob = json.loads(read_quality_record(record.record_s3_path))
        calls = (blob.get("execution") or {}).get("tool_calls") or []
        return [str(c.get("tool_name")) for c in calls if c.get("tool_name")]
    except Exception:  # noqa: BLE001 — the blob is a best-effort fallback
        return []


def classify(outcome_correct: bool, used: bool) -> str:
    """The four-cell capability classification (the heart of C1)."""
    if outcome_correct:
        return GENUINE if used else CHEATED
    return FAILED_WITH_TOOL if used else FAILED_NO_TOOL


def _outcome_from_profile(
    profile: dict | None, threshold: float
) -> tuple[bool, str, float | None]:
    """Derive ``(correct, signal, score)`` from an E-02 quality profile.

    Prefers a scored ``reference`` dimension (E-03, objective); otherwise uses the
    weighted score ≥ ``threshold``. Returns signal ``"none"`` when neither is
    available — correctness is then ``False`` (we cannot claim the answer is right).
    """
    if not isinstance(profile, dict):
        return False, "none", None
    for d in profile.get("dimensions") or []:
        if d.get("evaluator") == "reference" and d.get("status") == "scored":
            score = d.get("score")
            if d.get("threshold") is not None and "passed" in d:
                correct = bool(d.get("passed"))
            else:
                correct = score is not None and float(score) >= threshold
            return correct, "reference", float(score) if score is not None else None
    weighted = profile.get("weighted_score")
    if weighted is not None:
        return float(weighted) >= threshold, "judge", float(weighted)
    return False, "none", None


async def evaluate_task_capability(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Run the capability-isolation harness for ``task``; write the profile.

    Returns ``None`` (skipped) when the task has no usable ``capability_spec``.
    Reuses the workspace judge for outcome correctness (runs E-02 once when the
    profile is missing). Overwrites on re-run, for on-demand re-evaluation. Never
    raises — any failure is persisted as a profile with ``status: "error"``.
    """
    spec = normalize_spec(task.capability_spec)
    if spec is None:
        logger.info(f"capability eval skipped — no capability_spec for task {task.id}")
        return None

    from app.api.settings import get_setting

    threshold = float(
        await get_setting(db, "capability_outcome_threshold", DEFAULT_OUTCOME_THRESHOLD)
        or DEFAULT_OUTCOME_THRESHOLD
    )

    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)

    try:
        cleaned_trace = await build_cleaned_trace(db, task)
        # Tool calls from the live cleaned trace, unioned with the durable tool-call
        # record captured in the E-01 blob *before* log compaction. After a task's
        # logs are archived the cleaned trace loses tool_name (so it sees no tools);
        # the blob is the compaction-proof source, keeping the Glass-Box check honest.
        called = list(dict.fromkeys(
            extract_tool_sequence(cleaned_trace) + await _durable_tool_names(record)
        ))
        distinct_called = called
        used, missing = tool_used(spec["required_tools"], called, spec["match"])

        # Outcome correctness reuses the configured judge; run E-02 once if absent.
        profile = record.quality_profile if record is not None else None
        if profile is None:
            from app.quality.judge import evaluate_task_quality

            profile = await evaluate_task_quality(db, task, commit=False)
        correct, signal, score = _outcome_from_profile(profile, threshold)

        cls = classify(correct, used)
        stats = cleaned_trace.get("stats") or {}
        result = {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "status": "scored",
            "category": spec["category"],
            "required_tools": spec["required_tools"],
            "match": spec["match"],
            "tools_called": distinct_called,
            "tool_used": used,
            "missing_tools": missing,
            "outcome_correct": correct,
            "outcome_signal": signal,
            "outcome_score": round(score, 2) if score is not None else None,
            "outcome_threshold": threshold,
            "classification": cls,
            "capability_passed": cls == GENUINE,
            "trace_stats": {
                "steps_total": stats.get("steps_total"),
                "tool_steps": sum(
                    1
                    for s in (cleaned_trace.get("steps") or [])
                    if s.get("kind") == "tool" and s.get("tool_name")
                ),
            },
            "evaluated_at": datetime.utcnow().isoformat(),
            "errors": [],
        }
    except Exception as e:  # noqa: BLE001 — a harness failure must not crash the request
        logger.warning(f"capability eval failed for task {task.id}: {e}")
        result = {
            "schema_version": CAPABILITY_SCHEMA_VERSION,
            "status": "error",
            "category": spec["category"],
            "required_tools": spec["required_tools"],
            "match": spec["match"],
            "evaluated_at": datetime.utcnow().isoformat(),
            "errors": [{"error": str(e)[:500]}],
        }

    if record is not None:
        record.capability_profile = result

    await log_event(
        db,
        "capability_evaluated",
        "system",
        {
            "category": result.get("category"),
            "classification": result.get("classification"),
            "tool_used": result.get("tool_used"),
            "outcome_correct": result.get("outcome_correct"),
            "status": result.get("status"),
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return result


def _blank_counts() -> dict:
    return {GENUINE: 0, CHEATED: 0, FAILED_WITH_TOOL: 0, FAILED_NO_TOOL: 0, "total": 0}


def _with_score(b: dict) -> dict:
    total = b["total"]
    return {**b, "capability_score": round(b[GENUINE] / total, 4) if total else None}


async def aggregate_capability(
    db: AsyncSession,
    *,
    workspace_id,
    category: str | None = None,
    model_used: str | None = None,
    template_id=None,
    suite: str | None = None,
) -> dict:
    """Aggregate capability profiles across a workspace into capability_score(s).

    ``capability_score = genuine / total`` over the matching *scored* profiles, with
    breakdowns by category, model and template — the model breakdown is the
    "compare models by capability_score" view. Filters narrow the population;
    ``suite`` restricts to one Benchmark Case Store suite.
    """
    q = select(QualityRecord).where(
        QualityRecord.workspace_id == workspace_id,
        QualityRecord.capability_profile.isnot(None),
    )
    if model_used:
        q = q.where(QualityRecord.model_used == model_used)
    if template_id is not None:
        q = q.where(QualityRecord.template_id == template_id)
    if suite:
        q = q.where(QualityRecord.benchmark_suite == suite)
    rows = (await db.execute(q)).scalars().all()

    overall = _blank_counts()
    by_category: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_template: dict[str, dict] = {}

    for r in rows:
        prof = r.capability_profile or {}
        if prof.get("status") != "scored":
            continue
        cls = prof.get("classification")
        if cls not in _CLASSES:
            continue
        cat = prof.get("category") or "uncategorized"
        if category and cat != category:
            continue
        model = r.model_used or "unknown"
        tmpl = r.template_name or (str(r.template_id) if r.template_id else "unknown")
        for bucket in (
            overall,
            by_category.setdefault(cat, _blank_counts()),
            by_model.setdefault(model, _blank_counts()),
            by_template.setdefault(tmpl, _blank_counts()),
        ):
            bucket[cls] += 1
            bucket["total"] += 1

    return {
        "workspace_id": str(workspace_id),
        "filters": {
            "category": category,
            "model_used": model_used,
            "template_id": str(template_id) if template_id else None,
            "suite": suite,
        },
        **_with_score(overall),
        "by_category": {k: _with_score(v) for k, v in sorted(by_category.items())},
        "by_model": {k: _with_score(v) for k, v in sorted(by_model.items())},
        "by_template": {k: _with_score(v) for k, v in sorted(by_template.items())},
    }
