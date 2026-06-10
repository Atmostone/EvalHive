"""Pairwise Comparison Framework (E-21).

Pointwise judging (E-02) clusters everything into 7-8 (§7.2). Pairwise — "which
is better, A or B?" — is more reliable and human-natural. This module builds the
A/B comparison pipeline:

* take two task results (two models / templates / prompts) — either two finished
  tasks, or candidate B **generated** on the fly by re-running a source task with
  different ``model_id`` / ``template_id`` / ``soul_md`` (``clone_task_for_rerun``
  + the ``pairwise_run_tick`` scheduler job, mirroring variance / perturbation);
* show them side-by-side and let an **LLM judge** (with position-bias mitigation)
  or a **human** pick the winner;
* feed judged verdicts as **real matches** to the E-19 ranking engine → an **ELO
  leaderboard** (the E-19 → E-21 hand-off), surfaced in the existing Leaderboard.

Position-bias mitigation (the E-18 deliverable that was a deferred no-op): judge
the same pair in both orders — ``(A,B)`` and ``(B,A)`` — and reconcile. Agree → that
winner; disagree → ``tie`` and ``position_bias_detected=true``. Two LLM calls per
judged pair. ``mitigate_position=False`` (one call) is exposed so a caller can
*measure* raw position bias.

Both judge and human verdicts live on the comparison row, so judge↔human
agreement (E-17 linkage) is row-local.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pairwise_comparison import PairwiseComparison
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.orchestrator.rerun import clone_task_for_rerun
from app.plugins.llm import get_llm_provider
from app.quality.judge import _judge_cost, _resolve_judge_model, _tokens_from_response
from app.quality.runs_common import (
    SUCCESS_TASK as _SUCCESS_TASK,
    ensure_child_evaluated as _ensure_child_evaluated,
)
from app.utils.events import log_event

logger = logging.getLogger(__name__)

_SUBJECTS = ("model", "template", "prompt")
_VERDICTS = ("a", "b", "tie")
# Cap each answer handed to the judge to keep the prompt bounded.
_ANSWER_CHAR_CAP = 6000

# Comparison lifecycle
STATUS_PENDING = "pending"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_JUDGED = "judged"
STATUS_FAILED = "failed"
_TERMINAL = {STATUS_JUDGED, STATUS_FAILED}

# Map a verdict from the swapped (B,A) frame back to the canonical (A,B) frame.
_SWAP = {"a": "b", "b": "a", "tie": "tie"}

_PAIR_SYSTEM_PROMPT = (
    "You are a strict, fair judge comparing two answers to the SAME task. Decide "
    "which answer is better overall — correctness, completeness and usefulness — "
    "or 'tie' if they are genuinely equivalent. Judge substance, not length or the "
    "order in which the answers are shown. Use the choose_winner tool."
)

_PAIR_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "choose_winner",
            "description": (
                "Pick the better of two answers to the same task, or 'tie' if "
                "they are equivalent, and justify the choice in one or two sentences."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "winner": {
                        "type": "string",
                        "enum": ["a", "b", "tie"],
                        "description": "Which answer is better: 'a', 'b', or 'tie'.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Brief justification for the choice.",
                    },
                },
                "required": ["winner", "reasoning"],
            },
        },
    }
]


# --------------------------------------------------------------------------- #
# Player identity + context (pure / read-only)
# --------------------------------------------------------------------------- #
def _prompt_label(soul_md: str) -> str:
    """A short, stable leaderboard label for a prompt (soul_md): the first
    non-empty line (capped) plus an 8-char content fingerprint, so different
    prompts never collide on the same label."""
    text = (soul_md or "").strip()
    if not text:
        return ""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"{first[:80]} · {digest}" if first else f"prompt:{digest}"


async def _template_name(db: AsyncSession, task: Task) -> Optional[str]:
    if task.template_id is None:
        return None
    tmpl = await db.get(Template, task.template_id)
    if tmpl is not None and tmpl.name:
        return tmpl.name
    return str(task.template_id)


async def _resolve_player(db: AsyncSession, task: Task, subject: str) -> Optional[str]:
    """The leaderboard identity of a task on the chosen ``subject`` axis.

    ``model`` → the denormalized ``model_used``; ``template`` → the readable
    template name; ``prompt`` → a label for the ``soul_md`` override (falling back
    to the template name). ``None`` when the task can't be placed on that axis."""
    if subject == "template":
        return await _template_name(db, task)
    if subject == "prompt":
        soul = (task.run_config or {}).get("soul_md")
        if soul:
            return _prompt_label(soul)
        return await _template_name(db, task)
    return task.model_used or None


def _answer_block(task: Task) -> str:
    summary = (task.result_summary or "").strip()
    if len(summary) > _ANSWER_CHAR_CAP:
        summary = summary[:_ANSWER_CHAR_CAP] + "\n…[truncated]"
    files = [str(f) for f in (task.result_files or [])]
    parts = []
    if files:
        parts.append(f"Files: {', '.join(files)}")
    parts.append(summary or "(empty)")
    return "\n".join(parts)


def build_pair_context(task_a: Task, task_b: Task, *, reference: Optional[str] = None) -> str:
    """Pure: the side-by-side context for the judge — the shared task input once,
    an optional reference answer, then Answer A and Answer B (each capped). The
    A/B order is exactly the order the two tasks are passed in, which is what the
    position-bias swap relies on."""
    parts = [
        f"Task: {task_a.title}",
        f"Description: {task_a.description or '(none)'}",
    ]
    if reference:
        ref = str(reference)
        if len(ref) > _ANSWER_CHAR_CAP:
            ref = ref[:_ANSWER_CHAR_CAP] + "…[truncated]"
        parts.append(f"Reference answer: {ref}")
    parts += [
        "",
        "=== Answer A ===",
        _answer_block(task_a),
        "",
        "=== Answer B ===",
        _answer_block(task_b),
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# LLM judge (position-bias mitigated)
# --------------------------------------------------------------------------- #
async def _judge_call(judge_llm, context: str) -> dict:
    """One forced ``choose_winner`` tool-call. Returns the winner ('a'|'b'|'tie'),
    reasoning and token usage in the A/B frame of the given context."""
    messages = [
        {"role": "system", "content": _PAIR_SYSTEM_PROMPT},
        {"role": "user", "content": context},
    ]
    resp = await get_llm_provider().acompletion(
        model=judge_llm.model.api_name,
        messages=messages,
        tools=_PAIR_TOOL,
        tool_choice={"type": "function", "function": {"name": "choose_winner"}},
        api_key=judge_llm.provider.api_key,
        api_base=judge_llm.provider.endpoint,
    )
    choice = resp.choices[0].message
    args = json.loads(choice.tool_calls[0].function.arguments)
    winner = str(args.get("winner") or "tie").lower()
    if winner not in _VERDICTS:
        winner = "tie"
    inp, out = _tokens_from_response(resp)
    return {
        "winner": winner,
        "reasoning": str(args.get("reasoning") or "")[:1000],
        "input_tokens": inp,
        "output_tokens": out,
    }


def _reconcile(first: str, mapped: str) -> tuple[str, bool]:
    """Reconcile the two per-order verdicts (both in the canonical A/B frame).
    Agree on a winner → that winner, no bias. Agree on tie → tie, no bias.
    Disagree → tie, position bias detected."""
    if first == mapped:
        return first, False
    return "tie", True


async def judge_pair_llm(
    db: AsyncSession,
    task_a: Task,
    task_b: Task,
    *,
    mitigate_position: bool = True,
    reference: Optional[str] = None,
) -> dict:
    """LLM-judge a pair. With ``mitigate_position`` (default) the same pair is
    judged in both orders and reconciled (agree → winner, disagree → tie +
    ``position_bias_detected``) — two LLM calls. With it off, a single call returns
    the raw A/B verdict (used to *measure* position bias). Returns
    ``{verdict, detail, cost_usd}``; never persists. Raises if no judge model."""
    judge_llm = await _resolve_judge_model(db, task_a.workspace_id)
    if judge_llm is None:
        raise ValueError("no judge or orchestrator model configured")

    ctx_ab = build_pair_context(task_a, task_b, reference=reference)
    first = await _judge_call(judge_llm, ctx_ab)
    in_tok, out_tok = first["input_tokens"], first["output_tokens"]

    detail = {
        "judge_model": judge_llm.model.api_name,
        "mitigate_position": mitigate_position,
        "orders": {"ab": {"winner": first["winner"], "reasoning": first["reasoning"]}},
    }

    if not mitigate_position:
        verdict = first["winner"]
        detail["position_bias_detected"] = None
    else:
        ctx_ba = build_pair_context(task_b, task_a, reference=reference)
        second = await _judge_call(judge_llm, ctx_ba)
        in_tok += second["input_tokens"]
        out_tok += second["output_tokens"]
        mapped = _SWAP[second["winner"]]
        detail["orders"]["ba"] = {
            "winner": second["winner"],  # raw, in the swapped (B,A) frame
            "winner_mapped": mapped,  # back in the canonical (A,B) frame
            "reasoning": second["reasoning"],
        }
        verdict, bias = _reconcile(first["winner"], mapped)
        detail["position_bias_detected"] = bias

    detail["input_tokens"] = in_tok
    detail["output_tokens"] = out_tok
    cost = _judge_cost(judge_llm, in_tok, out_tok)
    detail["cost_usd"] = cost
    return {"verdict": verdict, "detail": detail, "cost_usd": cost}


async def _judge_comparison(
    db: AsyncSession, comp: PairwiseComparison, task_a: Task, task_b: Task
) -> None:
    """Run the LLM judge for a ready comparison and persist the verdict. A judge
    failure stores the error in ``judge_detail`` and leaves the comparison
    ``ready`` (retryable) — it never raises out to the caller / API."""
    try:
        result = await judge_pair_llm(
            db, task_a, task_b, mitigate_position=True, reference=task_a.reference_answer
        )
    except Exception as e:  # noqa: BLE001 — a failed judge must not 500 / wedge
        await db.rollback()
        logger.warning(f"pairwise: judge failed for comparison {comp.id}: {e}")
        comp.judge_detail = {"error": str(e)[:300]}
        await db.commit()
        return

    comp.judge_verdict = result["verdict"]
    comp.judge_detail = result["detail"]
    comp.cost_usd = (comp.cost_usd or Decimal("0")) + Decimal(str(result["cost_usd"]))
    comp.status = STATUS_JUDGED
    if comp.completed_at is None:
        comp.completed_at = datetime.utcnow()
    await log_event(
        db,
        "pairwise_judged",
        "system",
        {
            "comparison_id": str(comp.id),
            "subject": comp.subject,
            "verdict": comp.judge_verdict,
            "position_bias_detected": (comp.judge_detail or {}).get("position_bias_detected"),
            "cost_usd": float(comp.cost_usd or 0),
        },
        workspace_id=comp.workspace_id,
        commit=False,
    )
    await db.commit()


# --------------------------------------------------------------------------- #
# Create / advance / human verdict
# --------------------------------------------------------------------------- #
async def create_comparison(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    subject: str = "model",
    task_a_id: uuid.UUID,
    task_b_id: Optional[uuid.UUID] = None,
    b_run_config: Optional[dict] = None,
    source_task_id: Optional[uuid.UUID] = None,
    judge_mode: str = "llm",
    created_by: str = "user",
) -> PairwiseComparison:
    """Create a comparison.

    **Direct** (``task_b_id`` given): both candidates are existing tasks →
    ``status="ready"``; an ``llm`` comparison is judged immediately → ``judged``.
    **Generated** (``task_b_id`` omitted, ``b_run_config`` given): candidate B is
    produced by re-running ``source_task_id`` (defaulting to ``task_a_id``) with the
    overrides → ``status="generating"``; the scheduler tick clones B, then judges.
    Returns the persisted row.
    """
    subject = subject if subject in _SUBJECTS else "model"
    judge_mode = "human" if judge_mode == "human" else "llm"

    task_a = await db.get(Task, task_a_id)
    if task_a is None or task_a.workspace_id != workspace_id:
        raise ValueError("task A not found in workspace")

    if task_b_id is None:
        # Generated mode — B is created by the tick from a rerun of the source.
        if not b_run_config:
            raise ValueError("a generated comparison requires task_b_id or b_run_config")
        src_id = source_task_id or task_a_id
        source = await db.get(Task, src_id)
        if source is None or source.workspace_id != workspace_id:
            raise ValueError("source task not found in workspace")
        comp = PairwiseComparison(
            workspace_id=workspace_id,
            subject=subject,
            source_task_id=src_id,
            task_a_id=task_a_id,
            task_b_id=None,
            b_run_config=dict(b_run_config),
            player_a=await _resolve_player(db, task_a, subject),
            player_b=None,
            status=STATUS_GENERATING,
            judge_mode=judge_mode,
            created_by=created_by,
        )
        db.add(comp)
        await db.commit()
        await db.refresh(comp)
        await advance_comparison(db, comp)
        await db.refresh(comp)
        return comp

    # Direct mode — both candidates already exist.
    task_b = await db.get(Task, task_b_id)
    if task_b is None or task_b.workspace_id != workspace_id:
        raise ValueError("task B not found in workspace")
    comp = PairwiseComparison(
        workspace_id=workspace_id,
        subject=subject,
        source_task_id=source_task_id,
        task_a_id=task_a_id,
        task_b_id=task_b_id,
        b_run_config=None,
        player_a=await _resolve_player(db, task_a, subject),
        player_b=await _resolve_player(db, task_b, subject),
        status=STATUS_READY,
        judge_mode=judge_mode,
        created_by=created_by,
    )
    db.add(comp)
    await db.commit()
    await db.refresh(comp)
    if judge_mode == "llm":
        await _judge_comparison(db, comp, task_a, task_b)
        await db.refresh(comp)
    return comp


async def advance_comparison(db: AsyncSession, comp: PairwiseComparison) -> None:
    """One step of the generated-B state machine (idempotent). Only ``generating``
    comparisons do anything: create B from a rerun of the source, wait for it to
    finish, then (llm mode) auto-judge. Terminalizes to ``failed`` if B fails or
    the source disappears — never wedges."""
    if comp.status != STATUS_GENERATING:
        return

    # 1) Create candidate B from a rerun of the source (once).
    if comp.task_b_id is None:
        source = await db.get(Task, comp.source_task_id) if comp.source_task_id else None
        if source is None:
            comp.status = STATUS_FAILED
            comp.judge_detail = {"error": "source task not available for generation"}
            comp.completed_at = datetime.utcnow()
            await db.commit()
            return
        child = await clone_task_for_rerun(
            db, source, run_config=comp.b_run_config, title_suffix=" [pairwise B]", commit=True
        )
        comp.task_b_id = child.id
        await db.commit()
        return  # let B run; a later tick continues

    # 2) B exists — react to its terminal state.
    task_b = await db.get(Task, comp.task_b_id)
    if task_b is None:
        comp.status = STATUS_FAILED
        comp.judge_detail = {"error": "candidate B disappeared"}
        comp.completed_at = datetime.utcnow()
        await db.commit()
        return

    if task_b.status in _SUCCESS_TASK:
        await _ensure_child_evaluated(db, task_b)
        comp.player_b = await _resolve_player(db, task_b, comp.subject)
        comp.status = STATUS_READY
        await db.commit()
        if comp.judge_mode == "llm":
            task_a = await db.get(Task, comp.task_a_id)
            if task_a is not None:
                await _judge_comparison(db, comp, task_a, task_b)
    elif task_b.status == TaskStatus.FAILED.value:
        comp.status = STATUS_FAILED
        comp.judge_detail = {"error": "candidate B run failed"}
        comp.completed_at = datetime.utcnow()
        await db.commit()
    # else still running — nothing to do this tick.


async def advance_active_comparisons(db: AsyncSession) -> int:
    """Advance every ``generating`` comparison; used by the scheduler tick."""
    comps = (
        await db.execute(
            select(PairwiseComparison).where(PairwiseComparison.status == STATUS_GENERATING)
        )
    ).scalars().all()
    advanced = 0
    for comp in comps:
        try:
            await advance_comparison(db, comp)
            advanced += 1
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            logger.warning(f"pairwise: advance failed for comparison {comp.id}: {e}")
    return advanced


async def judge_comparison_by_id(
    db: AsyncSession, comp_id: uuid.UUID, *, workspace_id: uuid.UUID
) -> PairwiseComparison:
    """Force / redo the LLM judge for a comparison whose B is ready."""
    comp = await db.get(PairwiseComparison, comp_id)
    if comp is None or comp.workspace_id != workspace_id:
        raise ValueError("comparison not found")
    if comp.task_a_id is None or comp.task_b_id is None:
        raise ValueError("comparison is not ready to judge (missing a candidate)")
    task_a = await db.get(Task, comp.task_a_id)
    task_b = await db.get(Task, comp.task_b_id)
    if task_a is None or task_b is None:
        raise ValueError("a candidate task is missing")
    await _judge_comparison(db, comp, task_a, task_b)
    await db.refresh(comp)
    return comp


async def record_human_verdict(
    db: AsyncSession,
    comp_id: uuid.UUID,
    *,
    verdict: str,
    reasoning: Optional[str] = None,
    submitted_by: str = "user",
    workspace_id: Optional[uuid.UUID] = None,
) -> PairwiseComparison:
    """Record a human winner ('a'|'b'|'tie') on the comparison row (E-17 linkage).
    Requires the comparison to be ready/judged; a ready comparison becomes
    ``judged``. The judge verdict, if any, is preserved alongside."""
    verdict = (verdict or "").lower()
    if verdict not in _VERDICTS:
        raise ValueError("verdict must be 'a', 'b', or 'tie'")
    comp = await db.get(PairwiseComparison, comp_id)
    if comp is None or (workspace_id is not None and comp.workspace_id != workspace_id):
        raise ValueError("comparison not found")
    if comp.status not in (STATUS_READY, STATUS_JUDGED):
        raise ValueError("comparison is not ready for a verdict")

    comp.human_verdict = verdict
    comp.human_by = submitted_by
    comp.human_reasoning = reasoning
    if comp.status == STATUS_READY:
        comp.status = STATUS_JUDGED
    if comp.completed_at is None:
        comp.completed_at = datetime.utcnow()
    await log_event(
        db,
        "pairwise_human_verdict",
        "user",
        {"comparison_id": str(comp.id), "subject": comp.subject, "verdict": verdict},
        workspace_id=comp.workspace_id,
        commit=False,
    )
    await db.commit()
    await db.refresh(comp)
    return comp


# --------------------------------------------------------------------------- #
# Leaderboard hand-off to E-19
# --------------------------------------------------------------------------- #
def comparisons_to_matches(comps, *, source: str = "judge") -> list[dict]:
    """Pure: judged comparisons → E-19 match dicts. ``source`` selects the
    ``judge`` or ``human`` verdict. Comparisons without that verdict, without both
    players, or where both players are the same identity (a self-match E-19 drops)
    are skipped. Ties are emitted as ``outcome="tie"``."""
    out: list[dict] = []
    for c in comps:
        verdict = getattr(c, "human_verdict", None) if source == "human" else getattr(
            c, "judge_verdict", None
        )
        a = getattr(c, "player_a", None)
        b = getattr(c, "player_b", None)
        if verdict not in _VERDICTS or not a or not b or a == b:
            continue
        out.append({"player_a": a, "player_b": b, "outcome": verdict, "weight": 1})
    return out


async def run_pairwise_leaderboard(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    subject: str = "model",
    method: str = "elo",
    source: str = "judge",
    created_by: str = "user",
) -> dict:
    """Collect judged comparisons on the ``subject`` axis, turn their verdicts into
    real head-to-head matches, and rank them via the E-19 engine — persisting a
    ``ranking_report`` (``source="explicit"``) shown in the existing Leaderboard
    tab. This is the ELO acceptance, reusing E-19. ``subject`` is coerced to
    model/template (E-19's axes); prompt comparisons produce verdicts but not an
    ELO board."""
    from app.quality.ranking import run_ranking

    subject = "template" if subject == "template" else "model"
    rows = (
        await db.execute(
            select(PairwiseComparison).where(
                PairwiseComparison.workspace_id == workspace_id,
                PairwiseComparison.subject == subject,
                PairwiseComparison.status == STATUS_JUDGED,
            )
        )
    ).scalars().all()
    matches = comparisons_to_matches(rows, source=source)
    report = await run_ranking(
        db,
        workspace_id=workspace_id,
        subject=subject,
        method=method,
        matches=matches,
        created_by=created_by,
    )
    report["pairwise"] = {
        "source": source,
        "n_judged_comparisons": len(rows),
        "n_matches": len(matches),
    }
    return report


# --------------------------------------------------------------------------- #
# Serialization / read
# --------------------------------------------------------------------------- #
def _serialize(comp: PairwiseComparison) -> dict:
    return {
        "id": str(comp.id),
        "workspace_id": str(comp.workspace_id),
        "subject": comp.subject,
        "source_task_id": str(comp.source_task_id) if comp.source_task_id else None,
        "task_a_id": str(comp.task_a_id) if comp.task_a_id else None,
        "task_b_id": str(comp.task_b_id) if comp.task_b_id else None,
        "b_run_config": comp.b_run_config,
        "player_a": comp.player_a,
        "player_b": comp.player_b,
        "status": comp.status,
        "judge_mode": comp.judge_mode,
        "judge_verdict": comp.judge_verdict,
        "human_verdict": comp.human_verdict,
        "judge_detail": comp.judge_detail,
        "human_by": comp.human_by,
        "human_reasoning": comp.human_reasoning,
        "cost_usd": float(comp.cost_usd or 0),
        "created_by": comp.created_by,
        "created_at": comp.created_at.isoformat() if comp.created_at else None,
        "updated_at": comp.updated_at.isoformat() if comp.updated_at else None,
        "completed_at": comp.completed_at.isoformat() if comp.completed_at else None,
    }


async def comparison_sides(db: AsyncSession, comp: PairwiseComparison) -> dict:
    """The side-by-side payload for the UI: each side's title / model / status /
    answer / outcome score, loaded from the candidate tasks + their quality
    records."""
    ids = [i for i in (comp.task_a_id, comp.task_b_id) if i]
    tasks: dict = {}
    recs: dict = {}
    if ids:
        trows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
        tasks = {t.id: t for t in trows}
        rrows = (
            await db.execute(select(QualityRecord).where(QualityRecord.task_id.in_(ids)))
        ).scalars().all()
        recs = {r.task_id: r for r in rrows}

    def side(tid, player):
        if tid is None:
            return None
        t = tasks.get(tid)
        if t is None:
            return {"task_id": str(tid), "player": player, "missing": True}
        rec = recs.get(tid)
        score = None
        if rec is not None and rec.quality_profile:
            score = rec.quality_profile.get("weighted_score")
        return {
            "task_id": str(tid),
            "player": player,
            "title": t.title,
            "model_used": t.model_used,
            "status": t.status,
            "result_summary": (t.result_summary or "")[:2000],
            "weighted_score": score,
        }

    return {"a": side(comp.task_a_id, comp.player_a), "b": side(comp.task_b_id, comp.player_b)}


async def get_comparison(
    db: AsyncSession, comp_id: uuid.UUID, *, workspace_id: uuid.UUID, with_sides: bool = False
) -> Optional[dict]:
    comp = await db.get(PairwiseComparison, comp_id)
    if comp is None or comp.workspace_id != workspace_id:
        return None
    out = _serialize(comp)
    if with_sides:
        out["side_by_side"] = await comparison_sides(db, comp)
    return out


async def list_comparisons(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    subject: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    q = select(PairwiseComparison).where(PairwiseComparison.workspace_id == workspace_id)
    if subject:
        q = q.where(PairwiseComparison.subject == subject)
    if status:
        q = q.where(PairwiseComparison.status == status)
    q = q.order_by(PairwiseComparison.created_at.desc()).limit(limit)
    rows = (await db.execute(q)).scalars().all()
    return [_serialize(r) for r in rows]


def judge_agreement(comps) -> dict:
    """Judge↔human agreement over comparisons carrying both verdicts (E-17
    linkage): how often the LLM judge and the human picked the same winner."""
    paired = [
        (getattr(c, "judge_verdict", None), getattr(c, "human_verdict", None))
        for c in comps
        if getattr(c, "judge_verdict", None) and getattr(c, "human_verdict", None)
    ]
    n = len(paired)
    if n == 0:
        return {"n": 0, "agreements": 0, "agreement": None}
    agree = sum(1 for j, h in paired if j == h)
    return {"n": n, "agreements": agree, "agreement": round(agree / n, 3)}
