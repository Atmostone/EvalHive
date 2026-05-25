"""Re-run / replay core (E-11 layer A).

A small, reusable primitive that clones a finished task into a fresh task and
drops it into the normal orchestrator queue. The clone reproduces the source's
input (title / description / reference answer / canonical trajectory) and, by
default, pins the same template so the orchestrator skips decomposition +
selection and runs the scenario as-is.

``run_config`` is an open override blob ({template_id?, model_id?, soul_md?,
seed?, temperature?}) honored at spawn time by the engine — the seam that lets
E-21 (pairwise), E-24 (prompt patches) and a future full U-03 replay UX vary a
single parameter without touching this module or the engine. E-11 (variance)
only ever pins ``template_id``.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskStatus


def _coerce_uuid(value) -> Optional[uuid.UUID]:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def clone_task_for_rerun(
    db: AsyncSession,
    source: Task,
    *,
    run_config: Optional[dict] = None,
    status: str = TaskStatus.READY.value,
    title_suffix: str = "",
    max_retries: int = 0,
    commit: bool = True,
) -> Task:
    """Clone ``source`` into a fresh task linked via ``replay_of_task_id``.

    The pinned template is taken from ``run_config['template_id']`` when given,
    else from the source (so a replay reproduces the template the source
    actually ran). When neither is available (e.g. a decomposed root that never
    ran an agent) the child goes through normal orchestrator selection.

    ``max_retries`` defaults to 0 — a re-run is a single deliberate execution;
    auto-retries would distort variance distributions and replay comparisons.
    """
    rc = dict(run_config) if run_config else None

    pinned_template_id = None
    if rc and rc.get("template_id"):
        pinned_template_id = _coerce_uuid(rc["template_id"])
    elif source.template_id is not None:
        pinned_template_id = source.template_id
        rc = {**(rc or {}), "template_id": str(source.template_id)}

    clone = Task(
        title=f"{source.title}{title_suffix}"[:500],
        description=source.description,
        priority=source.priority,
        reference_answer=source.reference_answer,
        canonical_trajectory=source.canonical_trajectory,
        workspace_id=source.workspace_id,
        replay_of_task_id=source.id,
        run_config=rc,
        template_id=pinned_template_id,
        max_retries=max_retries,
        status=status,
    )
    db.add(clone)
    if commit:
        await db.commit()
        await db.refresh(clone)
    else:
        await db.flush()
    return clone
