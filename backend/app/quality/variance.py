"""Variance / Robustness Harness (E-11, type R1).

Runs one scenario N times and measures the dispersion of the result rather than
a single point estimate — an agent that is sometimes brilliant and sometimes
fails is worse than a stably-mediocre one (§3.4 R1).

The harness is poll-driven, reusing the existing machinery:

* children are plain tasks created via the re-run core (``clone_task_for_rerun``)
  or from a fresh spec, dropped into the queue as READY;
* the orchestrator loop spawns them respecting ``max_concurrent_agents`` — we
  never manage a thread pool here;
* ``advance_variance_run`` is the per-run state machine driven by the
  ``variance_run_tick`` scheduler job: it creates the next children while under
  the cost cap, evaluates finished children (outcome E-02 / trajectory E-07 when
  a judge is configured) and aggregates the distribution once all children are
  terminal.

Trajectory length, success rate and tool-selection stability are derived
cheaply from the cleaned trace (E-06) and task status — no extra LLM cost. Only
the outcome-score and trajectory-score dimensions need a judge; they degrade
gracefully to "unavailable" when none is configured.
"""

from __future__ import annotations

import logging
import statistics
import uuid
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.variance_run import VarianceRun
from app.orchestrator.rerun import clone_task_for_rerun
from app.quality.runs_common import (
    SUCCESS_TASK as _SUCCESS_TASK,
    TERMINAL_TASK as _TERMINAL_TASK,
    accumulated_cost as _accumulated_cost,
    distribution as _distribution,
    ensure_child_evaluated as _ensure_child_evaluated,
    inflight_target,
)

logger = logging.getLogger(__name__)

AGGREGATE_SCHEMA_VERSION = 1

# Run lifecycle
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_CAPPED = "capped"
STATUS_FAILED = "failed"
_TERMINAL_RUN = {STATUS_DONE, STATUS_CAPPED, STATUS_FAILED}

N_MIN = 2
N_MAX = 50


# --------------------------------------------------------------------------- #
# Statistics helpers (pure Python — no numpy dependency)
# --------------------------------------------------------------------------- #
def _tool_stability(tool_sequences: list[list[str]]) -> dict:
    """How consistent tool usage is across runs.

    Reports the share of runs sharing the most common ordered tool signature
    (1.0 = perfectly reproducible path), the count of distinct signatures, and
    per-tool usage spread (mean/std of times-used across runs).
    """
    runs = len(tool_sequences)
    if runs == 0:
        return {"runs": 0, "distinct_signatures": 0, "modal_share": None,
                "per_tool": [], "signatures": []}

    sig_counter = Counter(tuple(seq) for seq in tool_sequences)
    modal_sig, modal_count = sig_counter.most_common(1)[0]

    all_tools = sorted({t for seq in tool_sequences for t in seq})
    per_tool = []
    for tool in all_tools:
        counts = [seq.count(tool) for seq in tool_sequences]
        per_tool.append({
            "tool": tool,
            "mean": round(statistics.fmean(counts), 3),
            "std": round(statistics.pstdev(counts), 3) if runs > 1 else 0.0,
            "present_in_runs": sum(1 for c in counts if c > 0),
        })

    signatures = [
        {"tools": list(sig), "count": cnt}
        for sig, cnt in sig_counter.most_common()
    ]
    return {
        "runs": runs,
        "distinct_signatures": len(sig_counter),
        "modal_share": round(modal_count / runs, 3),
        "per_tool": per_tool,
        "signatures": signatures,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
async def run_variance(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    source_task_id: Optional[uuid.UUID] = None,
    source_spec: Optional[dict] = None,
    n: int = 10,
    parallel: bool = True,
    cost_cap_usd: Optional[Decimal] = None,
    template_id: Optional[uuid.UUID] = None,
) -> VarianceRun:
    """Create a variance run and kick off the first batch of children.

    Exactly one of ``source_task_id`` (replay an existing task) or
    ``source_spec`` ({title, description?, reference_answer?}) must be given.
    Returns the persisted :class:`VarianceRun`; the scheduler tick advances it.
    """
    if (source_task_id is None) == (source_spec is None):
        raise ValueError("provide exactly one of source_task_id or source_spec")
    if not (N_MIN <= int(n) <= N_MAX):
        raise ValueError(f"n must be between {N_MIN} and {N_MAX}")

    resolved_template = template_id
    spec_payload = None
    if source_task_id is not None:
        source = await db.get(Task, source_task_id)
        if source is None or source.workspace_id != workspace_id:
            raise ValueError("source task not found in workspace")
        if resolved_template is None:
            resolved_template = source.template_id
    else:
        title = (source_spec or {}).get("title")
        if not title:
            raise ValueError("source_spec requires a title")
        spec_payload = {
            "title": title,
            "description": (source_spec or {}).get("description") or "",
            "reference_answer": (source_spec or {}).get("reference_answer"),
        }

    run = VarianceRun(
        workspace_id=workspace_id,
        source_task_id=source_task_id,
        source_spec=spec_payload,
        template_id=resolved_template,
        n=int(n),
        parallel=bool(parallel),
        cost_cap_usd=cost_cap_usd,
        status=STATUS_PENDING,
        child_task_ids=[],
        accumulated_cost_usd=Decimal("0"),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    await advance_variance_run(db, run)
    # advance commits again (status / child list / onupdate timestamp), which
    # expires server-side columns — reload so callers can serialize safely.
    await db.refresh(run)
    return run


async def advance_variance_run(db: AsyncSession, run: VarianceRun) -> None:
    """One step of the run state machine (idempotent; safe to call repeatedly)."""
    if run.status in _TERMINAL_RUN:
        return

    children = await _load_children(db, run)

    # 1) Evaluate freshly-finished (successful) children so the aggregate has scores.
    for child in children:
        if child.status in _SUCCESS_TASK:
            await _ensure_child_evaluated(db, child)

    # 2) Recompute accumulated cost (agent runs + judge evals).
    run.accumulated_cost_usd = await _accumulated_cost(db, children)
    cost_exceeded = (
        run.cost_cap_usd is not None
        and run.accumulated_cost_usd >= run.cost_cap_usd
    )

    created = len(children)
    in_flight = [c for c in children if c.status not in _TERMINAL_TASK]

    # 3) Create more children while there's room and budget.
    if created < run.n and not cost_exceeded:
        target = await inflight_target(db, parallel=run.parallel)
        slots = max(0, target - len(in_flight))
        to_create = min(slots, run.n - created)
        for i in range(to_create):
            child = await _make_child(db, run, idx=created + i)
            run.child_task_ids = list(run.child_task_ids) + [str(child.id)]
        if to_create:
            run.status = STATUS_RUNNING
            await db.commit()
            return  # let them run; finalize on a later tick

    # 4) Finalize once we won't create more and nothing is in flight.
    stop_creating = cost_exceeded or created >= run.n
    if stop_creating and not in_flight:
        if created == 0:
            run.status = STATUS_FAILED
            run.aggregate = {"error": "no runs executed (cost cap too low or n=0)"}
            run.completed_at = datetime.utcnow()
            await db.commit()
            return
        await _finalize(
            db, run, children, capped=(cost_exceeded and created < run.n)
        )
    else:
        run.status = STATUS_RUNNING
        await db.commit()


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
async def _load_children(db: AsyncSession, run: VarianceRun) -> list[Task]:
    if not run.child_task_ids:
        return []
    ids = [uuid.UUID(x) for x in run.child_task_ids]
    rows = (await db.execute(select(Task).where(Task.id.in_(ids)))).scalars().all()
    # Preserve creation order for stable indexing in the UI.
    by_id = {str(t.id): t for t in rows}
    return [by_id[i] for i in run.child_task_ids if i in by_id]


async def _make_child(db: AsyncSession, run: VarianceRun, *, idx: int) -> Task:
    suffix = f" [variance {idx + 1}/{run.n}]"
    run_config = (
        {"template_id": str(run.template_id)} if run.template_id else None
    )
    if run.source_task_id is not None:
        source = await db.get(Task, run.source_task_id)
        if source is None:
            raise ValueError("variance source task disappeared")
        return await clone_task_for_rerun(
            db, source, run_config=run_config, title_suffix=suffix, commit=True
        )

    spec = run.source_spec or {}
    child = Task(
        title=f"{spec.get('title', 'Variance run')}{suffix}"[:500],
        description=spec.get("description") or "",
        reference_answer=spec.get("reference_answer"),
        workspace_id=run.workspace_id,
        template_id=run.template_id,
        run_config=run_config,
        replay_of_task_id=None,
        max_retries=0,
        status=TaskStatus.READY.value,
    )
    db.add(child)
    await db.commit()
    await db.refresh(child)
    return child


async def _finalize(
    db: AsyncSession, run: VarianceRun, children: list[Task], *, capped: bool
) -> None:
    run.aggregate = await _aggregate(db, run, children, capped=capped)
    run.status = STATUS_CAPPED if capped else STATUS_DONE
    run.completed_at = datetime.utcnow()
    await db.commit()


async def _aggregate(
    db: AsyncSession, run: VarianceRun, children: list[Task], *, capped: bool
) -> dict:
    from app.quality.trace_cleaner import build_cleaned_trace

    done = [c for c in children if c.status in _SUCCESS_TASK]
    failed = [c for c in children if c.status == TaskStatus.FAILED.value]
    created = len(children)

    recs = {}
    if children:
        rows = (
            await db.execute(
                select(QualityRecord).where(
                    QualityRecord.task_id.in_([c.id for c in children])
                )
            )
        ).scalars().all()
        recs = {str(r.task_id): r for r in rows}

    outcome_scores: list[float] = []
    trajectory_scores: list[float] = []
    trajectory_lengths: list[float] = []
    tool_sequences: list[list[str]] = []

    for c in done:
        rec = recs.get(str(c.id))
        if rec and rec.quality_profile:
            ws = rec.quality_profile.get("weighted_score")
            if ws is not None:
                outcome_scores.append(ws)
        if rec and rec.trajectory_profile:
            ov = rec.trajectory_profile.get("overall_score")
            if ov is not None:
                trajectory_scores.append(ov)

        # Length + tool sequence come straight from the cleaned trace (no LLM).
        try:
            cleaned = await build_cleaned_trace(db, c)
            steps = cleaned.get("steps") or []
            trajectory_lengths.append((cleaned.get("stats") or {}).get("steps_total", len(steps)))
            tool_sequences.append([s["tool_name"] for s in steps if s.get("tool_name")])
        except Exception as e:
            logger.warning(f"variance: trace read failed for {c.id}: {e}")

    dimensions = [
        {
            "key": "outcome_score",
            "name": "Outcome score",
            "unit": "0-10",
            "available": bool(outcome_scores),
            "dist": _distribution(outcome_scores),
        },
        {
            "key": "trajectory_length",
            "name": "Trajectory length",
            "unit": "steps",
            "available": bool(trajectory_lengths),
            "dist": _distribution(trajectory_lengths),
        },
        {
            "key": "trajectory_score",
            "name": "Trajectory score",
            "unit": "0-10",
            "available": bool(trajectory_scores),
            "dist": _distribution(trajectory_scores),
        },
    ]

    return {
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "n_requested": run.n,
        "n_executed": created,
        "n_success": len(done),
        "n_failed": len(failed),
        "success_rate": round(len(done) / created, 3) if created else 0.0,
        "accumulated_cost_usd": float(run.accumulated_cost_usd or 0),
        "capped": capped,
        "dimensions": dimensions,
        "tool_stability": _tool_stability(tool_sequences),
        "generated_at": datetime.utcnow().isoformat(),
    }


async def advance_active_runs(db: AsyncSession) -> int:
    """Advance every non-terminal variance run; used by the scheduler tick."""
    runs = (
        await db.execute(
            select(VarianceRun).where(VarianceRun.status.notin_(list(_TERMINAL_RUN)))
        )
    ).scalars().all()
    advanced = 0
    for run in runs:
        try:
            await advance_variance_run(db, run)
            advanced += 1
        except Exception as e:
            await db.rollback()
            logger.warning(f"variance: advance failed for run {run.id}: {e}")
    return advanced
