"""Orchestrator engine: polls for Ready tasks and manages agent lifecycle."""

import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import resolve_workspace_model
from app.api.settings import get_setting
from app.api.templates import template_to_dict
from app.database import async_session
from app.models.experiment import ExperimentRun
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.orchestrator.llm import (
    decide_decomposition,
    select_template_for_task,
)
from app.plugins.runtime import get_agent_runtime
from app.runtime import get_process_runtime
from app.utils.events import log_event
from app.utils.failures import (
    FAILURE_INFRA,
    FAILURE_TIMEOUT,
    is_contaminated,
    merge_failure_type,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds

async def _record_run_condition(
    db: AsyncSession, task: Task, full: str, core: str | None = None
) -> None:
    """Stamp the spawn-time condition onto the experiment cell this task belongs to."""
    ref = ((task.run_config or {}).get("experiment") or {}) if task.run_config else {}
    if not ref.get("id"):
        return
    try:
        experiment_id = uuid.UUID(str(ref["id"]))
    except (ValueError, TypeError):
        return
    await db.execute(
        update(ExperimentRun)
        .where(
            ExperimentRun.experiment_id == experiment_id,
            ExperimentRun.config_key == ref.get("config_key"),
            ExperimentRun.case_key == ref.get("case_key"),
            ExperimentRun.run_index == ref.get("run_index"),
            ExperimentRun.task_id == task.id,
        )
        .values(condition_fingerprint=full, core_condition_fingerprint=core)
    )


async def _spawn_agent_for_template(db: AsyncSession, task: Task, template: Template):
    """All task modes use the installed connector and common runner."""
    task.template_id = template.id
    await _spawn_sdk_task(db, task, template)


def _subtask_run_config(run_config: dict | None) -> dict | None:
    """Run-level overrides a decomposition child inherits from its parent.

    Keeps the keys that apply to any leaf (benchmark_mode, model, sampling,
    memory) and drops the template-relative ones (template_id pin,
    tools_override) — the orchestrator selects each child's template, so the
    parent's tool override would not map onto it.
    """
    if not run_config:
        return None
    inherited = {
        k: v
        for k, v in run_config.items()
        if k in ("benchmark_mode", "model_id", "temperature", "seed", "memory_mode",
                 "harness_config", "harness_profile_id", "execution_mode")
        and v is not None
    }
    return inherited or None


async def _spawn_sdk_task(db, task, template=None):
    from app.harnesses.attempts import AttemptError
    from app.harnesses.catalog import CatalogError
    from app.harnesses.launch import launch_task
    from app.harnesses.profiles import ProfileError
    from app.models.quality_record import QualityRecord
    from app.runtime.docker import RuntimeFailure

    archived = await db.scalar(select(QualityRecord).where(QualityRecord.task_id == task.id))
    if archived is not None:
        task.status = archived.final_status
        await db.commit()
        await log_event(db, "rerun_requires_new_task", "runner", {"reason": "results_archived"},
                        task_id=task.id, workspace_id=task.workspace_id)
        return

    try:
        await launch_task(db, task, template)
    except Exception as error:
        if isinstance(error, AttemptError) and error.code == "CAPACITY":
            await db.rollback()
            await db.refresh(task)
            task.status = TaskStatus.READY.value
            await db.commit()
            return
        # Public diagnostics must not expose resolved provider/resource values.
        await db.rollback()
        await db.refresh(task)
        task.status = TaskStatus.FAILED.value
        task.failure_type = merge_failure_type(task.failure_type, FAILURE_INFRA)
        task.result_summary = str(error) if isinstance(error, (AttemptError, CatalogError, ProfileError, RuntimeFailure)) else "Execution configuration could not be launched"
        task.completed_at = datetime.utcnow()
        await db.commit()
        await log_event(db, "agent_failed", "runner", {"reason": "launch_configuration"},
                        task_id=task.id, workspace_id=task.workspace_id)
        await check_parent_task_completion(db, task)


async def process_ready_task(db: AsyncSession, task: Task):
    """Process a single Ready task: decompose or select template + spawn agent."""
    logger.info(f"Processing task {task.id}: {task.title}")

    from app.harnesses.launch import execution_mode, selected_template

    try:
        pinned = await selected_template(db, task)
    except ValueError:
        task.status = TaskStatus.FAILED.value
        task.failure_type = merge_failure_type(task.failure_type, FAILURE_INFRA)
        task.result_summary = "Role template is unavailable in this workspace"
        task.completed_at = datetime.utcnow()
        await db.commit()
        await check_parent_task_completion(db, task)
        return
    if execution_mode(task, pinned) == "direct":
        await _spawn_sdk_task(db, task, pinned)
        return

    # Mark as decomposing while we decide
    task.status = TaskStatus.DECOMPOSING.value
    await db.commit()

    await log_event(
        db, "orchestrator_decision", "orchestrator",
        {"action": "processing_task", "title": task.title},
        task_id=task.id,
    )

    # Pinned-template fast path: re-run / variance / replay children (and
    # retries, which already carry a template_id) reproduce the scenario as-is —
    # skip orchestrator decomposition + selection and spawn the pinned template
    # directly. Default tasks reach here with template_id=None.
    if task.template_id:
        pinned = await db.get(Template, task.template_id)
        if pinned is not None:
            await _spawn_agent_for_template(db, task, pinned)
            return
        logger.warning(
            f"pinned template {task.template_id} for task {task.id} not found; "
            "falling back to orchestrator selection"
        )

    # Resolve orchestrator model from the workspace; surface 400 if not configured.
    try:
        orchestrator_llm = await resolve_workspace_model(
            db, task.workspace_id, "orchestrator"
        )
    except Exception as e:
        logger.error(f"orchestrator model not configured for task {task.id}: {e}")
        task.status = TaskStatus.FAILED.value
        task.failure_type = merge_failure_type(task.failure_type, FAILURE_INFRA)
        await db.commit()
        await log_event(
            db, "orchestrator_decision", "orchestrator",
            {"action": "failed", "reason": "orchestrator model not configured"},
            task_id=task.id,
        )
        # Terminal without an agent: nothing will webhook, so roll the parent up here.
        await check_parent_task_completion(db, task)
        return

    result = await db.execute(
        select(Template).where(Template.workspace_id == task.workspace_id)
    )
    templates = result.scalars().all()

    if not templates:
        logger.error("No templates available, cannot process task")
        task.status = TaskStatus.FAILED.value
        task.failure_type = merge_failure_type(task.failure_type, FAILURE_INFRA)
        await db.commit()
        await log_event(
            db, "orchestrator_decision", "orchestrator",
            {"action": "failed", "reason": "No templates available"},
            task_id=task.id,
        )
        # Terminal without an agent: nothing will webhook, so roll the parent up here.
        await check_parent_task_completion(db, task)
        return

    templates_list = [template_to_dict(t) for t in templates]

    # Step 1: Decide whether to decompose (only if task has no parent — avoid nested decomposition)
    decomposition_enabled = bool(await get_setting(db, "decomposition_enabled", True))
    if decomposition_enabled and not task.parent_id and len(templates_list) > 1:
        subtasks = await decide_decomposition(
            task.title, task.description or "", templates_list, orchestrator_llm,
            db=db, task_id=task.id,
        )
        if subtasks:
            # Validate dependency indices are acyclic (forward-only references)
            for i, st in enumerate(subtasks):
                deps = st.get("depends_on_indices") or []
                for d in deps:
                    if not isinstance(d, int) or d < 0 or d >= i:
                        await log_event(
                            db, "decomposition_failed_cycle", "orchestrator",
                            {"subtask_index": i, "bad_index": d, "subtask_count": len(subtasks)},
                            task_id=task.id,
                        )
                        task.status = TaskStatus.FAILED.value
                        await db.commit()
                        # Terminal without an agent — roll the parent up here.
                        await check_parent_task_completion(db, task)
                        return

            # Benchmark roots (SPA-40, orchestrator:on cells) must keep every
            # leaf on the benchmark path: children inherit the run-level
            # overrides and never retry, or the cell would stall in the
            # approval flow / distort the run count.
            sub_run_config = _subtask_run_config(task.run_config)
            benchmark = bool((task.run_config or {}).get("benchmark_mode"))
            created_subs: list[Task] = []
            for st in subtasks:
                sub = Task(
                    parent_id=task.id,
                    title=st["title"],
                    description=st.get("description", ""),
                    priority=task.priority,
                    status=TaskStatus.READY.value,
                    workspace_id=task.workspace_id,
                    origin=task.origin,
                    run_config=sub_run_config,
                    harness_profile_id=task.harness_profile_id,
                    execution_mode=task.execution_mode,
                )
                if benchmark:
                    sub.max_retries = 0
                db.add(sub)
                created_subs.append(sub)

            await db.flush()
            for st, sub in zip(subtasks, created_subs, strict=True):
                deps = [
                    created_subs[i].id
                    for i in (st.get("depends_on_indices") or [])
                    if 0 <= i < len(created_subs)
                ]
                sub.depends_on = deps

            task.status = TaskStatus.IN_PROGRESS.value
            task.started_at = datetime.utcnow()
            await db.commit()

            await log_event(
                db, "orchestrator_decision", "orchestrator",
                {"action": "decomposed", "subtask_count": len(subtasks),
                 "subtasks": [s["title"] for s in subtasks]},
                task_id=task.id,
            )
            logger.info(f"Decomposed task {task.id} into {len(subtasks)} subtasks")
            return

    # Step 2: Select template
    selection = await select_template_for_task(
        task.title, task.description or "", templates_list, orchestrator_llm,
        db=db, task_id=task.id,
    )
    if not selection:
        task.status = TaskStatus.FAILED.value
        await db.commit()
        await log_event(
            db, "orchestrator_decision", "orchestrator",
            {"action": "failed", "reason": "Template selection failed"},
            task_id=task.id,
        )
        # Terminal without an agent: nothing will webhook, so roll the parent up here.
        await check_parent_task_completion(db, task)
        return

    template_id = selection["template_id"]
    template = await db.get(Template, uuid.UUID(template_id))
    if not template:
        logger.error(f"Selected template {template_id} not found")
        task.status = TaskStatus.FAILED.value
        task.failure_type = merge_failure_type(task.failure_type, FAILURE_INFRA)
        await db.commit()
        # Terminal without an agent: nothing will webhook, so roll the parent up here.
        await check_parent_task_completion(db, task)
        return

    task.template_id = template.id

    await log_event(
        db, "orchestrator_decision", "orchestrator",
        {"action": "template_selected", "template_id": str(template.id),
         "template_name": template.name, "reasoning": selection.get("reasoning", "")},
        task_id=task.id,
    )

    # Step 3: Spawn agent
    await _spawn_agent_for_template(db, task, template)


async def check_parent_task_completion(db: AsyncSession, task: Task, *, commit: bool = True):
    """Check if all subtasks of a parent are done, and update parent accordingly."""
    if not task.parent_id:
        return

    parent = await db.scalar(
        select(Task).where(Task.id == task.parent_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    if not parent:
        return

    # Get all sibling subtasks
    result = await db.execute(
        select(Task).where(Task.parent_id == parent.id).execution_options(populate_existing=True)
    )
    subtasks = result.scalars().all()

    all_done = all(s.status in (TaskStatus.DONE.value, TaskStatus.FAILED.value) for s in subtasks)
    if not all_done:
        return

    any_failed = any(s.status == TaskStatus.FAILED.value for s in subtasks)
    parent.status = (
        TaskStatus.FAILED.value if any_failed else TaskStatus.DONE.value
    )
    # Under orchestrator:on the ExperimentRun denormalizes from the PARENT, so a
    # child a provider quota got to reached the report as an ordinary result —
    # the status rolled up and the reason did not (SPA-87). Inherited regardless
    # of the parent's own outcome: a child that fell back to a substituted
    # template after a 429 SUCCEEDS, and a successful run under a condition
    # nobody chose is exactly the case that must not count. Only an
    # infrastructure reason is inherited — a child that failed on its own merits
    # is a result — and it is merged, never assigned, so it cannot wipe a
    # contamination the parent already carries from its own orchestrator call.
    contaminated = sorted(
        (s for s in subtasks if is_contaminated(s.failure_type)),
        key=lambda s: (s.created_at or datetime.min, str(s.id)),
    )
    if contaminated:
        parent.failure_type = merge_failure_type(
            parent.failure_type, contaminated[0].failure_type
        )
    # Benchmark roots (SPA-40, orchestrator:on cells) are judged end-to-end by
    # the quality pipeline, but decomposed parents have no result of their own
    # — synthesize a rollup from the children so E-02 has something to score.
    if (parent.run_config or {}).get("benchmark_mode") and not parent.result_summary:
        parts = []
        for s in subtasks:
            summary = (s.result_summary or "").strip() or "(no summary)"
            parts.append(f"## {s.title} [{s.status}]\n{summary}")
        parent.result_summary = "\n\n".join(parts)[:20000]
    parent.completed_at = datetime.utcnow()
    if commit:
        await db.commit()

    return await log_event(
        db, "task_status_changed", "system",
        {"new_status": parent.status, "reason": "all subtasks completed"},
        task_id=parent.id, workspace_id=parent.workspace_id, commit=commit,
    )


async def check_timed_out_tasks():
    """Kill containers for tasks that exceeded their timeout."""
    from app.api.settings import get_setting

    try:
        async with async_session() as db:
            from app.harnesses.executor import reconcile_attempts

            await reconcile_attempts(db)
            timeout_minutes = await get_setting(db, "task_timeout_minutes", 60)

            result = await db.execute(
                select(Task).where(
                    Task.status == TaskStatus.IN_PROGRESS.value,
                    Task.started_at.isnot(None),
                    Task.current_attempt_id.is_(None),
                )
            )
            tasks = result.scalars().all()

            for task in tasks:
                if not task.started_at:
                    continue
                elapsed = (datetime.utcnow() - task.started_at).total_seconds() / 60
                if elapsed > timeout_minutes:
                    logger.warning(f"Task {task.id} timed out ({elapsed:.0f}m > {timeout_minutes}m)")

                    if task.agent_container_id:
                        get_agent_runtime().kill(task.agent_container_id)

                    task.status = TaskStatus.FAILED.value
                    # Typed, but deliberately NOT contaminating (SPA-87): a run
                    # that hit the wall clock may have been looping, and dropping
                    # it from the aggregates would quietly forgive that. It only
                    # stops being indistinguishable from a provider outage.
                    task.failure_type = merge_failure_type(
                        task.failure_type, FAILURE_TIMEOUT
                    )
                    task.completed_at = datetime.utcnow()
                    await db.commit()

                    await log_event(
                        db, "task_timeout", "system",
                        {"elapsed_minutes": round(elapsed), "timeout": timeout_minutes},
                        task_id=task.id,
                        agent_container_id=task.agent_container_id,
                    )
                    # The reaper is this task's terminal event; no webhook follows
                    # it, so the parent would wait for a child that is already
                    # finished (SPA-87).
                    await check_parent_task_completion(db, task)
    except Exception as e:
        logger.error(f"Timeout check error: {e}")


async def orchestrator_loop():
    """Main polling loop: check for Ready tasks every POLL_INTERVAL seconds.

    Uses SELECT ... FOR UPDATE SKIP LOCKED on the chosen row to make this safe
    against accidental concurrent runs (e.g. dev hot-reload, replicas).
    """
    logger.info("Orchestrator engine started")
    timeout_check_counter = 0

    while True:
        try:
            async with async_session() as db:
                # Find tasks with status=ready, prioritized, deps satisfied
                priority_order = case(
                    (Task.priority == "urgent", 1),
                    (Task.priority == "high", 2),
                    (Task.priority == "medium", 3),
                    (Task.priority == "low", 4),
                    else_=5,
                )
                result = await db.execute(
                    select(Task.id, Task.depends_on)
                    .where(Task.status == TaskStatus.READY.value)
                    .order_by(priority_order, Task.created_at)
                )
                candidates = result.all()
                picked_id = None
                for cand_id, deps in candidates:
                    deps = deps or []
                    if not deps:
                        picked_id = cand_id
                        break
                    dep_rows = (
                        await db.execute(
                            select(Task.status).where(Task.id.in_(deps))
                        )
                    ).all()
                    if (
                        len(dep_rows) == len(deps)
                        and all((row[0] == TaskStatus.DONE.value) for row in dep_rows)
                    ):
                        picked_id = cand_id
                        break

                if picked_id is not None:
                    # Re-fetch with row lock; SKIP LOCKED so concurrent orchestrators
                    # never pick the same row.
                    locked = (
                        await db.execute(
                            select(Task)
                            .where(
                                Task.id == picked_id,
                                Task.status == TaskStatus.READY.value,
                            )
                            .with_for_update(skip_locked=True)
                        )
                    ).scalar_one_or_none()
                    if locked is not None:
                        from app.api.settings import get_setting
                        from app.config import get_settings

                        max_agents = min(int(await get_setting(db, "max_concurrent_agents", 3)),
                                         get_settings().runtime_max_concurrency)
                        active = len(await asyncio.to_thread(get_process_runtime().list_active))
                        if active < int(max_agents):
                            await process_ready_task(db, locked)
                        else:
                            logger.debug(
                                f"concurrency limit reached: {active}/{max_agents}, "
                                f"task {locked.id} stays in READY"
                            )

            # Reconcile runner heartbeats and pending result workflows every minute.
            timeout_check_counter += 1
            if timeout_check_counter >= 12:
                timeout_check_counter = 0
                await check_timed_out_tasks()

        except Exception as e:
            logger.error(f"Orchestrator loop error: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)
