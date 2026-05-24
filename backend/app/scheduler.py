"""APScheduler integration: load enabled scheduled_jobs into AsyncIOScheduler."""

import logging
import uuid
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.database import async_session
from app.models.scheduled_job import ScheduledJob
from app.models.workspace import DEFAULT_WORKSPACE_ID

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler | None:
    return _scheduler


async def _job_runner(job_id: str):
    """Execute a job by id: dispatch on its kind/payload, log to agent_events."""
    from app.utils.events import log_event
    from app.plugins.runtime import get_agent_runtime
    from sqlalchemy import func

    async with async_session() as db:
        job = await db.get(ScheduledJob, uuid.UUID(job_id))
        if not job or not job.enabled:
            return
        action = (job.payload or {}).get("action", "noop")

        if action == "daily_cost_rollup":
            from app.models.task import Task

            since = datetime.utcnow() - timedelta(days=1)
            row = (
                await db.execute(
                    select(
                        func.coalesce(func.sum(Task.cost_usd), 0),
                        func.count(Task.id),
                    ).where(
                        Task.completed_at >= since,
                        Task.workspace_id == job.workspace_id,
                    )
                )
            ).first()
            await log_event(
                db, "daily_cost_summary", "system",
                {"total_cost_usd": float(row[0] or 0), "task_count": int(row[1] or 0)},
                workspace_id=job.workspace_id,
            )

        elif action == "agent_progress_check":
            runtime = get_agent_runtime()
            for a in runtime.list_active(workspace_id=str(job.workspace_id)):
                cid = a.get("container_id")
                if not cid:
                    continue
                health = await runtime.health(cid)
                if health is not None:
                    await log_event(
                        db, "agent_health", "system",
                        {"current_step": health.get("current_step"),
                         "iteration": health.get("iteration")},
                        agent_container_id=cid,
                        workspace_id=job.workspace_id,
                    )

        elif action == "quality_record_backfill":
            # Quality Data Lake (E-01): build records for any terminal task that
            # has none yet (e.g. user-approved → done, parents, spawn-failures),
            # and reconcile final_status of existing records. Global (all WS).
            from app.models.task import Task, TaskStatus
            from app.models.quality_record import QualityRecord
            from app.quality.data_lake import build_quality_record

            terminal = (TaskStatus.DONE.value, TaskStatus.FAILED.value)
            missing = (
                await db.execute(
                    select(Task).where(
                        Task.status.in_(terminal),
                        Task.id.notin_(select(QualityRecord.task_id)),
                    )
                )
            ).scalars().all()
            built = 0
            for t in missing:
                try:
                    await build_quality_record(db, t, commit=True)
                    built += 1
                except Exception as e:
                    await db.rollback()
                    logger.warning(f"quality backfill failed for task {t.id}: {e}")

            reconciled = (
                await db.execute(
                    select(QualityRecord, Task)
                    .join(Task, Task.id == QualityRecord.task_id)
                    .where(
                        Task.status.in_(terminal),
                        QualityRecord.final_status != Task.status,
                    )
                )
            ).all()
            for rec, t in reconciled:
                rec.final_status = t.status
                rec.cost_usd = t.cost_usd or 0
            await db.commit()
            if built or reconciled:
                await log_event(
                    db, "quality_record_backfill", "system",
                    {"built": built, "reconciled": len(reconciled)},
                    workspace_id=job.workspace_id,
                )

        elif action == "quality_record_retention":
            # Prune records older than data_lake_retention_days (0 = keep
            # forever). public_dataset_opt_in records are never auto-deleted.
            from app.api.settings import get_setting
            from app.models.quality_record import QualityRecord
            from app.storage.minio_client import delete_object

            days = int(await get_setting(db, "data_lake_retention_days", 0) or 0)
            if days > 0:
                cutoff = datetime.utcnow() - timedelta(days=days)
                old = (
                    await db.execute(
                        select(QualityRecord).where(
                            QualityRecord.created_at < cutoff,
                            QualityRecord.public_dataset_opt_in.is_(False),
                        )
                    )
                ).scalars().all()
                deleted = 0
                for rec in old:
                    if rec.record_s3_path:
                        try:
                            delete_object(rec.record_s3_path)
                        except Exception as e:
                            logger.warning(f"retention blob delete failed: {e}")
                    await db.delete(rec)
                    deleted += 1
                await db.commit()
                if deleted:
                    await log_event(
                        db, "quality_record_retention", "system",
                        {"deleted": deleted, "retention_days": days},
                        workspace_id=job.workspace_id,
                    )
        elif action == "quality_judge_evaluate":
            # Multi-dim Quality Rubric Engine (E-02): score terminal `done`
            # records that have no quality_profile yet. Off by default —
            # gated by the `quality_eval_enabled` setting to avoid surprise
            # token spend; the on-demand API button works regardless.
            from app.api.settings import get_setting
            from app.models.task import Task, TaskStatus
            from app.models.quality_record import QualityRecord
            from app.quality.judge import evaluate_task_quality

            if bool(await get_setting(db, "quality_eval_enabled", False)):
                pending = (
                    await db.execute(
                        select(QualityRecord)
                        .where(
                            QualityRecord.final_status == TaskStatus.DONE.value,
                            QualityRecord.quality_profile.is_(None),
                        )
                        .limit(10)
                    )
                ).scalars().all()
                evaluated = 0
                for rec in pending:
                    task = await db.get(Task, rec.task_id)
                    if task is None:
                        continue
                    try:
                        if await evaluate_task_quality(db, task, commit=True):
                            evaluated += 1
                    except Exception as e:
                        await db.rollback()
                        logger.warning(f"quality eval failed for task {rec.task_id}: {e}")
                if evaluated:
                    await log_event(
                        db, "quality_judge_batch", "system",
                        {"evaluated": evaluated},
                        workspace_id=job.workspace_id,
                    )

        elif action == "trajectory_judge_evaluate":
            # 6-axis Trajectory Judge (E-07): score terminal `done` records that
            # have no trajectory_profile yet. Off by default — gated by the
            # `trajectory_eval_enabled` setting to avoid surprise token spend;
            # the on-demand API button works regardless.
            from app.api.settings import get_setting
            from app.models.task import Task, TaskStatus
            from app.models.quality_record import QualityRecord
            from app.quality.trajectory import evaluate_task_trajectory

            if bool(await get_setting(db, "trajectory_eval_enabled", False)):
                pending = (
                    await db.execute(
                        select(QualityRecord)
                        .where(
                            QualityRecord.final_status == TaskStatus.DONE.value,
                            QualityRecord.trajectory_profile.is_(None),
                        )
                        .limit(10)
                    )
                ).scalars().all()
                evaluated = 0
                for rec in pending:
                    task = await db.get(Task, rec.task_id)
                    if task is None:
                        continue
                    try:
                        if await evaluate_task_trajectory(db, task, commit=True):
                            evaluated += 1
                    except Exception as e:
                        await db.rollback()
                        logger.warning(
                            f"trajectory eval failed for task {rec.task_id}: {e}"
                        )
                if evaluated:
                    await log_event(
                        db, "trajectory_judge_batch", "system",
                        {"evaluated": evaluated},
                        workspace_id=job.workspace_id,
                    )

        else:
            await log_event(
                db, "scheduled_job_fired", "system",
                {"name": job.name, "action": action, "payload": job.payload},
                workspace_id=job.workspace_id,
            )

        job.last_fired_at = datetime.utcnow()
        if job.kind == "once":
            job.enabled = False
        await db.commit()


def _trigger_for(job: ScheduledJob):
    if job.kind == "cron" and job.cron_expr:
        return CronTrigger.from_crontab(job.cron_expr)
    if job.kind == "interval" and job.interval_seconds:
        return IntervalTrigger(seconds=int(job.interval_seconds))
    if job.kind == "once" and job.fire_at:
        return DateTrigger(run_date=job.fire_at)
    return None


def _add_job(scheduler: AsyncIOScheduler, job: ScheduledJob) -> bool:
    trigger = _trigger_for(job)
    if trigger is None:
        logger.warning(f"job {job.id} has no valid trigger ({job.kind})")
        return False
    scheduler.add_job(
        _job_runner,
        trigger=trigger,
        id=str(job.id),
        args=[str(job.id)],
        replace_existing=True,
    )
    return True


async def reload_jobs() -> int:
    if _scheduler is None:
        return 0
    async with async_session() as db:
        rows = (await db.execute(select(ScheduledJob).where(ScheduledJob.enabled.is_(True)))).scalars().all()
    for j in list(_scheduler.get_jobs()):
        _scheduler.remove_job(j.id)
    count = sum(1 for j in rows if _add_job(_scheduler, j))
    logger.info(f"scheduler: loaded {count} job(s)")
    return count


async def seed_default_jobs():
    """Create the two built-in jobs if missing (attached to default workspace)."""
    async with async_session() as db:
        rows = (await db.execute(select(ScheduledJob))).scalars().all()
        names = {r.name for r in rows}
        if "daily_cost_rollup" not in names:
            db.add(ScheduledJob(
                name="daily_cost_rollup", kind="cron", cron_expr="0 0 * * *",
                payload={"action": "daily_cost_rollup"},
                workspace_id=DEFAULT_WORKSPACE_ID,
            ))
        if "agent_progress_check" not in names:
            db.add(ScheduledJob(
                name="agent_progress_check", kind="interval", interval_seconds=60,
                payload={"action": "agent_progress_check"},
                workspace_id=DEFAULT_WORKSPACE_ID,
            ))
        if "quality_record_backfill" not in names:
            db.add(ScheduledJob(
                name="quality_record_backfill", kind="interval", interval_seconds=300,
                payload={"action": "quality_record_backfill"},
                workspace_id=DEFAULT_WORKSPACE_ID,
            ))
        if "quality_record_retention" not in names:
            db.add(ScheduledJob(
                name="quality_record_retention", kind="cron", cron_expr="30 0 * * *",
                payload={"action": "quality_record_retention"},
                workspace_id=DEFAULT_WORKSPACE_ID,
            ))
        if "quality_judge_evaluate" not in names:
            db.add(ScheduledJob(
                name="quality_judge_evaluate", kind="interval", interval_seconds=600,
                payload={"action": "quality_judge_evaluate"},
                workspace_id=DEFAULT_WORKSPACE_ID,
            ))
        if "trajectory_judge_evaluate" not in names:
            db.add(ScheduledJob(
                name="trajectory_judge_evaluate", kind="interval", interval_seconds=600,
                payload={"action": "trajectory_judge_evaluate"},
                workspace_id=DEFAULT_WORKSPACE_ID,
            ))
        await db.commit()


async def start_scheduler():
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    await seed_default_jobs()
    await reload_jobs()


def stop_scheduler():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
