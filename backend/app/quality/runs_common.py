"""Shared helpers for poll-driven multi-run harnesses.

Both the variance harness (E-11) and the perturbation judge (E-12) create N
plain child tasks, let the orchestrator loop drain them under
``max_concurrent_agents``, then evaluate the finished ones and aggregate. The
child-lifecycle plumbing is identical, so it lives here once:

* terminal-state sets for a child task;
* pure statistics helpers (percentile / distribution);
* best-effort outcome + trajectory scoring of a finished child;
* accumulated cost (agent runs + judge evals);
* the in-flight target derived from ``max_concurrent_agents``.
"""

from __future__ import annotations

import logging
import statistics
from decimal import Decimal
from math import ceil, floor
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus

logger = logging.getLogger(__name__)

# A child stops progressing on its own at DONE, FAILED, or AWAITING_APPROVAL —
# the last is a successful agent run that auto-review approved and that merely
# awaits a human click, so for these harnesses it counts as finished + success.
SUCCESS_TASK = {TaskStatus.DONE.value, TaskStatus.AWAITING_APPROVAL.value}
TERMINAL_TASK = SUCCESS_TASK | {TaskStatus.FAILED.value}


# --------------------------------------------------------------------------- #
# Statistics helpers (pure Python — no numpy dependency)
# --------------------------------------------------------------------------- #
def percentile(sorted_vals: list[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile (p in 0..100) over a pre-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo, hi = floor(k), ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo))


def distribution(values: list[float]) -> dict:
    """Summary distribution over the run samples (drops Nones)."""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return {"n": 0, "values": []}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 3),
        "std": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
        "min": round(vals[0], 3),
        "p25": round(percentile(vals, 25), 3),
        "p50": round(percentile(vals, 50), 3),
        "p75": round(percentile(vals, 75), 3),
        "p95": round(percentile(vals, 95), 3),
        "max": round(vals[-1], 3),
        "values": [round(v, 3) for v in vals],
    }


# --------------------------------------------------------------------------- #
# Child lifecycle
# --------------------------------------------------------------------------- #
async def ensure_child_evaluated(db: AsyncSession, child: Task) -> None:
    """Best-effort outcome + trajectory scoring of a finished child.

    Skips work cheaply when already scored or when no judge is configured;
    never raises (mirrors the scheduler-job error handling).
    """
    from app.quality.data_lake import build_quality_record
    from app.quality.judge import evaluate_task_quality
    from app.quality.trajectory import evaluate_task_trajectory

    try:
        await build_quality_record(db, child, commit=True)
    except Exception as e:
        await db.rollback()
        logger.warning(f"runs: record build failed for {child.id}: {e}")
        return

    rec = (
        await db.execute(
            select(QualityRecord).where(QualityRecord.task_id == child.id)
        )
    ).scalar_one_or_none()

    if rec is not None and rec.quality_profile is None:
        try:
            await evaluate_task_quality(db, child, commit=True)
        except Exception as e:
            await db.rollback()
            logger.warning(f"runs: outcome eval failed for {child.id}: {e}")
    if rec is not None and rec.trajectory_profile is None:
        try:
            await evaluate_task_trajectory(db, child, commit=True)
        except Exception as e:
            await db.rollback()
            logger.warning(f"runs: trajectory eval failed for {child.id}: {e}")


async def accumulated_cost(db: AsyncSession, children: list[Task]) -> Decimal:
    """Agent-run cost + judge cost across all children so far."""
    total = Decimal("0")
    for c in children:
        total += Decimal(c.cost_usd or 0)
    if not children:
        return total
    recs = (
        await db.execute(
            select(QualityRecord).where(
                QualityRecord.task_id.in_([c.id for c in children])
            )
        )
    ).scalars().all()
    for rec in recs:
        for prof in (rec.quality_profile, rec.trajectory_profile):
            if prof:
                total += Decimal(str(prof.get("judge_cost_usd") or 0))
    return total


async def inflight_target(db: AsyncSession, *, parallel: bool) -> int:
    """How many children may run at once: serial → 1, else max_concurrent_agents."""
    if not parallel:
        return 1
    from app.api.settings import get_setting

    try:
        return max(1, int(await get_setting(db, "max_concurrent_agents", 3)))
    except (TypeError, ValueError):
        return 3
