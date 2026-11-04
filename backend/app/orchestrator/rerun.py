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
from copy import deepcopy

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskStatus


def _coerce_uuid(value) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


async def clone_task_for_rerun(
    db: AsyncSession,
    source: Task,
    *,
    run_config: dict | None = None,
    status: str = TaskStatus.READY.value,
    title_suffix: str = "",
    title: str | None = None,
    description: str | None = None,
    max_retries: int = 0,
    commit: bool = True,
) -> Task:
    """Clone ``source`` into a fresh task linked via ``replay_of_task_id``.

    The pinned template is taken from ``run_config['template_id']`` when given,
    else from the source (so a replay reproduces the template the source
    actually ran). When neither is available (e.g. a decomposed root that never
    ran an agent) the child goes through normal orchestrator selection.

    ``title`` / ``description`` override the source's input when given — the
    seam the perturbation judge (E-12) uses to replay a paraphrased / noised /
    reordered prompt while keeping the same template and reference answer. When
    omitted the source's input is reproduced verbatim.

    ``max_retries`` defaults to 0 — a re-run is a single deliberate execution;
    auto-retries would distort variance distributions and replay comparisons.
    """
    rc = deepcopy(source.run_config or {})
    # A manual rerun is not another execution of its source experiment cell.
    rc.pop("experiment", None)
    rc.pop("network_mode", None)
    for key in ("harness_snapshot", "new_harness_conditions", "replay_input_override", "replay_overrides"):
        rc.pop(key, None)
    rc.update(deepcopy(run_config or {}))
    rc["replay_overrides"] = sorted((run_config or {}).keys())
    if "harness_profile_id" in (run_config or {}):
        rc["new_harness_conditions"] = True
    if title is not None or description is not None:
        rc["replay_input_override"] = True
    if source.current_attempt_id and not ((run_config or {}).get("harness_profile_id") or rc.get("new_harness_conditions") or (run_config or {}).get("harness_snapshot")):
        from app.harnesses.provenance import execution_context
        from app.models.execution_attempt import ExecutionAttempt

        attempt = await db.get(ExecutionAttempt, source.current_attempt_id)
        if attempt is not None:
            rc["harness_snapshot"] = execution_context(attempt)["harness"]
            rc["harness_snapshot"]["credential_roles"] = list(attempt.run_spec.get("credential_refs", {}))
    if rc.get("new_harness_conditions"):
        rc.pop("harness_snapshot", None)
        rc.pop("harness_config", None)
        rc.pop("agent_image", None)

    pinned_template_id = None
    if rc and rc.get("template_id"):
        pinned_template_id = _coerce_uuid(rc["template_id"])
    elif source.template_id is not None:
        pinned_template_id = source.template_id
        rc = {**(rc or {}), "template_id": str(source.template_id)}

    base_title = title if title is not None else source.title
    frozen_profile = (rc.get("harness_snapshot") or {}).get("profile_id")
    profile_id = _coerce_uuid(rc.get("harness_profile_id")) if rc.get("new_harness_conditions") else _coerce_uuid(frozen_profile) or getattr(source, "harness_profile_id", None)
    if profile_id:
        from app.models.harness_profile import HarnessProfile

        profile = await db.get(HarnessProfile, profile_id)
        if profile is None or profile.workspace_id != source.workspace_id:
            raise ValueError("Recorded harness profile is unavailable; create a run with new conditions")
    clone = Task(
        title=f"{base_title}{title_suffix}"[:500],
        description=description if description is not None else source.description,
        priority=source.priority,
        reference_answer=source.reference_answer,
        canonical_trajectory=source.canonical_trajectory,
        capability_spec=getattr(source, "capability_spec", None),
        workspace_id=source.workspace_id,
        replay_of_task_id=source.id,
        run_config=rc or None,
        template_id=pinned_template_id,
        harness_profile_id=profile_id,
        execution_mode=rc.get("execution_mode") or (rc.get("harness_snapshot") or {}).get("execution_mode") or getattr(source, "execution_mode", None),
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
