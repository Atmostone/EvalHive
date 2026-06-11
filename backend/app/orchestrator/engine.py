"""Orchestrator engine: polls for Ready tasks and manages agent lifecycle."""

import asyncio
import logging
import os
import uuid
from datetime import datetime

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import resolve_model_by_id, resolve_workspace_model
from app.api.settings import get_setting
from app.api.templates import template_to_dict
from app.auth.tokens import issue_agent_token
from app.database import async_session
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.orchestrator.llm import (
    decide_decomposition,
    select_template_for_task,
)
from app.plugins.runtime import AgentSpec, get_agent_runtime
from app.utils.events import log_event

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds

# Cap per flat-memory file captured into the spawn snapshot (chars).
_FLAT_MEMORY_CAP = 20000


def _read_flat_memory(workspace_id) -> dict:
    """Read the rules.md / memory.md the agent is mounted at spawn time.

    These shared files are mounted read-only into the container; capturing
    their content here is the only durable record of the flat-memory state the
    agent actually saw. Best-effort: missing files → empty strings.
    """
    from app.config import get_settings

    shared = os.path.join(get_settings().data_dir, "shared", str(workspace_id))
    out = {}
    for fname, key in (("rules.md", "rules_md"), ("memory.md", "memory_md")):
        try:
            with open(os.path.join(shared, fname)) as fh:
                out[key] = fh.read()[:_FLAT_MEMORY_CAP]
        except OSError:
            out[key] = ""
    return out


def _spawn_snapshot(
    task,
    template,
    agent_llm,
    memory_context: str,
    soul_md: str | None = None,
    *,
    tools: list | None = None,
    mcp_servers: list | None = None,
) -> dict:
    """Full state snapshot captured into the `agent_spawned` event.

    Source of truth for the Quality Data Lake (E-01) `execution` section —
    soul_md, tools, MCP, model, resource limits, and the memory/RAG context
    the agent received are not recoverable later, so we record them here.
    ``soul_md`` defaults to the template's prompt but may be a per-run override.
    ``tools``/``mcp_servers`` are the resolved registry set (SPA-41) the agent
    actually received — not the template's references.
    """
    return {
        "template_id": str(template.id),
        "template_name": template.name,
        "soul_md": (soul_md if soul_md is not None else (template.soul_md or "")),
        "tools": list(tools or []),
        "mcp_servers": list(mcp_servers or []),
        "model_api_name": agent_llm.model.api_name,
        "input_price_per_1m_usd": (
            float(agent_llm.model.input_price_per_1m_usd)
            if agent_llm.model.input_price_per_1m_usd is not None else None
        ),
        "output_price_per_1m_usd": (
            float(agent_llm.model.output_price_per_1m_usd)
            if agent_llm.model.output_price_per_1m_usd is not None else None
        ),
        "resource_limits": {
            "max_ram": template.max_ram,
            "max_cpu": template.max_cpu,
            "timeout_minutes": template.timeout_minutes,
        },
        "memory_context": memory_context or "",
        "flat_memory": _read_flat_memory(task.workspace_id),
    }


async def _spawn_agent_for_template(db: AsyncSession, task: Task, template: Template):
    """Build the AgentSpec for ``template`` and spawn the agent.

    Shared by the normal selection path and the pinned-template path (re-run /
    variance / replay children). Honors optional per-run overrides in
    ``task.run_config`` — ``model_id`` (else the template's model) and
    ``soul_md`` (else the template's prompt). Sets the task FAILED and logs on
    any spawn error, mirroring the previous inline behavior.
    """
    task.template_id = template.id
    run_config = task.run_config or {}
    try:
        # Build description with feedback if this is a retry after rejection
        desc_parts = [task.title, "", task.description or ""]
        if task.user_feedback:
            desc_parts.append(f"\n\n--- USER FEEDBACK (from rejection) ---\n{task.user_feedback}\nPlease address this feedback. Previous workspace files are available.")
        if task.orchestrator_feedback:
            desc_parts.append(f"\n\n--- ORCHESTRATOR FEEDBACK ---\n{task.orchestrator_feedback}")

        if task.depends_on:
            dep_rows = (
                await db.execute(select(Task).where(Task.id.in_(task.depends_on)))
            ).scalars().all()
            for dep in dep_rows:
                if dep.status == TaskStatus.DONE.value and (dep.result_summary or dep.result_files):
                    short_id = str(dep.id)[:8]
                    files_line = f"Files: {', '.join(dep.result_files or []) or 'none'}"
                    desc_parts.append(
                        f"\n\n## Dependency: {dep.title} (#{short_id})\n"
                        f"{dep.result_summary or '(no summary)'}\n\n{files_line}"
                    )

        memory_context = ""

        # Per-run memory override (run_config.memory_mode: off|flat|structured),
        # else the workspace setting. 'off' and 'flat' both skip the structured
        # context block; flat memory files are mounted regardless.
        memory_mode = run_config.get("memory_mode") or await get_setting(
            db, "memory_mode", "flat"
        )
        if memory_mode == "structured":
            from app.memory.store import build_memory_context

            try:
                memory_context = await build_memory_context(
                    db,
                    query_text=f"{task.title}\n{task.description or ''}",
                    workspace_id=task.workspace_id,
                )
            except Exception as e:
                logger.warning(f"Memory context build failed for task {task.id}: {e}")

        # Per-run model override (run_config.model_id), else the template's model.
        model_id = run_config.get("model_id") or template.model_id
        try:
            agent_llm = await resolve_model_by_id(db, model_id)
        except Exception as e:
            logger.error(f"template {template.id} has no model configured: {e}")
            task.status = TaskStatus.FAILED.value
            await db.commit()
            await log_event(
                db, "orchestrator_decision", "orchestrator",
                {"action": "spawn_failed", "error": "template model not configured"},
                task_id=task.id,
            )
            return

        # Per-run prompt override (run_config.soul_md), else the template's prompt.
        soul = run_config.get("soul_md")
        if soul is None:
            soul = template.soul_md or ""

        # Resolve tools & MCP from the workspace registry (SPA-41), applying any
        # task-level run_config.tools_override. Yields the exact shapes the agent
        # container consumes (builtin tool names + MCP server dicts with secrets).
        from app.registry.resolver import resolve_template_tools

        resolved_tools, resolved_mcp = await resolve_template_tools(
            db, template, run_config=run_config
        )

        agent_token = await issue_agent_token(
            db, task_id=task.id, workspace_id=task.workspace_id
        )
        await db.commit()

        # Perturbation judge (E-12): a poisoned tool response injected at runtime.
        extra_env = {}
        tool_injection = run_config.get("tool_injection")
        if tool_injection:
            extra_env["AGENT_TOOL_INJECTION"] = str(tool_injection)
        # Per-run sampling overrides (SPA-40 experiment axes); the agent applies
        # them to its completion calls when present.
        if run_config.get("temperature") is not None:
            extra_env["LLM_TEMPERATURE"] = str(run_config["temperature"])
        if run_config.get("seed") is not None:
            extra_env["LLM_SEED"] = str(run_config["seed"])

        runtime = get_agent_runtime()
        spec = AgentSpec(
            task_id=str(task.id),
            task_description="\n".join(desc_parts),
            template_name=template.name,
            template_id=str(template.id),
            soul_md=soul,
            tools=resolved_tools,
            mcp_servers=resolved_mcp,
            env={
                "OPENAI_API_KEY": agent_llm.provider.api_key,
                "OPENAI_BASE_URL": agent_llm.provider.endpoint,
                "LLM_MODEL": agent_llm.model.api_name,
            },
            resource_limits={
                "max_ram": template.max_ram,
                "max_cpu": template.max_cpu,
            },
            workspace_id=str(task.workspace_id),
            agent_token=agent_token,
            memory_context=memory_context,
            extra_env=extra_env,
        )
        container_id = runtime.spawn(spec)
        task.agent_container_id = container_id
        task.model_used = agent_llm.model.api_name
        task.input_price_per_1m_usd = agent_llm.model.input_price_per_1m_usd
        task.output_price_per_1m_usd = agent_llm.model.output_price_per_1m_usd
        task.status = TaskStatus.IN_PROGRESS.value
        task.started_at = datetime.utcnow()
        await db.commit()

        await log_event(
            db, "agent_spawned", "orchestrator",
            {"container_id": container_id,
             **_spawn_snapshot(task, template, agent_llm, memory_context, soul_md=soul,
                               tools=resolved_tools, mcp_servers=resolved_mcp)},
            task_id=task.id, agent_container_id=container_id,
        )
        logger.info(f"Spawned agent {container_id[:12]} for task {task.id}")

    except Exception as e:
        logger.error(f"Failed to spawn agent: {e}", exc_info=True)
        task.status = TaskStatus.FAILED.value
        await db.commit()
        await log_event(
            db, "orchestrator_decision", "orchestrator",
            {"action": "spawn_failed", "error": str(e)},
            task_id=task.id,
        )


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
        if k in ("benchmark_mode", "model_id", "temperature", "seed", "memory_mode")
        and v is not None
    }
    return inherited or None


async def process_ready_task(db: AsyncSession, task: Task):
    """Process a single Ready task: decompose or select template + spawn agent."""
    logger.info(f"Processing task {task.id}: {task.title}")

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
        await db.commit()
        await log_event(
            db, "orchestrator_decision", "orchestrator",
            {"action": "failed", "reason": "orchestrator model not configured"},
            task_id=task.id,
        )
        return

    result = await db.execute(
        select(Template).where(Template.workspace_id == task.workspace_id)
    )
    templates = result.scalars().all()

    if not templates:
        logger.error("No templates available, cannot process task")
        task.status = TaskStatus.FAILED.value
        await db.commit()
        await log_event(
            db, "orchestrator_decision", "orchestrator",
            {"action": "failed", "reason": "No templates available"},
            task_id=task.id,
        )
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
                )
                if benchmark:
                    sub.max_retries = 0
                db.add(sub)
                created_subs.append(sub)

            await db.flush()
            for st, sub in zip(subtasks, created_subs):
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
        return

    template_id = selection["template_id"]
    template = await db.get(Template, uuid.UUID(template_id))
    if not template:
        logger.error(f"Selected template {template_id} not found")
        task.status = TaskStatus.FAILED.value
        await db.commit()
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


async def check_parent_task_completion(db: AsyncSession, task: Task):
    """Check if all subtasks of a parent are done, and update parent accordingly."""
    if not task.parent_id:
        return

    parent = await db.get(Task, task.parent_id)
    if not parent:
        return

    # Get all sibling subtasks
    result = await db.execute(
        select(Task).where(Task.parent_id == parent.id)
    )
    subtasks = result.scalars().all()

    all_done = all(s.status in (TaskStatus.DONE.value, TaskStatus.FAILED.value) for s in subtasks)
    if not all_done:
        return

    any_failed = any(s.status == TaskStatus.FAILED.value for s in subtasks)
    if any_failed:
        parent.status = TaskStatus.FAILED.value
    else:
        parent.status = TaskStatus.DONE.value
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
    await db.commit()

    await log_event(
        db, "task_status_changed", "system",
        {"new_status": parent.status, "reason": "all subtasks completed"},
        task_id=parent.id,
    )


async def check_timed_out_tasks():
    """Kill containers for tasks that exceeded their timeout."""
    from app.api.settings import get_setting

    try:
        async with async_session() as db:
            timeout_minutes = await get_setting(db, "task_timeout_minutes", 60)

            result = await db.execute(
                select(Task).where(
                    Task.status == TaskStatus.IN_PROGRESS.value,
                    Task.started_at.isnot(None),
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
                    task.completed_at = datetime.utcnow()
                    await db.commit()

                    await log_event(
                        db, "task_timeout", "system",
                        {"elapsed_minutes": round(elapsed), "timeout": timeout_minutes},
                        task_id=task.id,
                        agent_container_id=task.agent_container_id,
                    )
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
                        max_agents = await get_setting(db, "max_concurrent_agents", 3)
                        active = len(get_agent_runtime().list_active())
                        if active < int(max_agents):
                            await process_ready_task(db, locked)
                        else:
                            logger.debug(
                                f"concurrency limit reached: {active}/{max_agents}, "
                                f"task {locked.id} stays in READY"
                            )

            # Check for timed out tasks every ~60 seconds (12 * 5s)
            timeout_check_counter += 1
            if timeout_check_counter >= 12:
                timeout_check_counter = 0
                await check_timed_out_tasks()

        except Exception as e:
            logger.error(f"Orchestrator loop error: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)
