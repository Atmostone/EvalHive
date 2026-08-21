"""LLM-driven extraction of entities/relations from completed task results."""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.plugins.llm import get_llm_provider
from app.utils.tool_args import extract_tool_args

from app.api.settings import get_setting
from app.database import async_session
from app.memory.store import upsert_entity
from app.models.memory import MemoryEntity, MemoryRelation
from app.models.task import Task
from app.utils.events import log_event

logger = logging.getLogger(__name__)

EXTRACT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "extract_memory_facts",
            "description": (
                "Extract structured entities and relations mentioned in the task result. "
                "Only include items that are stable facts likely to be useful in future tasks. "
                "Skip transient or trivial details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "description": "Entity category (e.g., person, project, decision, technology, location).",
                                },
                                "name": {"type": "string"},
                                "attributes": {
                                    "type": "object",
                                    "description": "Flat key-value scalars.",
                                },
                            },
                            "required": ["type", "name"],
                        },
                    },
                    "relations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string", "description": "Entity name from `entities`."},
                                "to": {"type": "string"},
                                "type": {"type": "string"},
                                "attributes": {"type": "object"},
                            },
                            "required": ["from", "to", "type"],
                        },
                    },
                },
                "required": ["entities"],
            },
        },
    }
]


async def _llm_extract(
    title: str, description: str, result_summary: str, llm
) -> dict:
    """``llm`` is a ResolvedModel (provider+model) from app.api._resolve_model."""
    messages = [
        {
            "role": "system",
            "content": (
                "You analyze a completed task result and extract durable facts about entities and "
                "relations between them. Use the extract_memory_facts tool. Stay concise."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task title: {title}\n"
                f"Task description: {description or '(none)'}\n\n"
                f"Result summary:\n{result_summary or '(empty)'}"
            ),
        },
    ]
    try:
        resp = await get_llm_provider().acompletion(
            model=llm.model.api_name,
            messages=messages,
            tools=EXTRACT_TOOLS,
            tool_choice={"type": "function", "function": {"name": "extract_memory_facts"}},
            api_key=llm.provider.api_key,
            api_base=llm.provider.endpoint,
        )
    except Exception as e:
        logger.error(f"Memory extraction LLM call failed: {e}")
        return {"entities": [], "relations": []}

    choice = resp.choices[0].message
    try:
        return extract_tool_args(choice)
    except Exception as e:  # noqa: BLE001 — memory extraction never fails a run
        logger.warning(f"Memory extraction returned no usable facts: {e}")
        return {"entities": [], "relations": []}


async def extract_memory(task_id: uuid.UUID | str) -> None:
    """Background coroutine: extract entities/relations from a completed task."""
    if isinstance(task_id, str):
        task_id = uuid.UUID(task_id)

    async with async_session() as db:
        memory_mode = await get_setting(db, "memory_mode", "flat")
        if memory_mode != "structured":
            return

        task = await db.get(Task, task_id)
        if not task or not task.result_summary:
            return

        from app.api._resolve_model import resolve_workspace_model

        try:
            memory_llm = await resolve_workspace_model(
                db, task.workspace_id, "memory_extractor"
            )
        except Exception as e:
            logger.info(f"memory extraction skipped — model not configured: {e}")
            return
        extracted = await _llm_extract(
            task.title, task.description or "", task.result_summary, memory_llm
        )

        new_entities = 0
        merged_entities = 0
        name_to_id: dict[str, uuid.UUID] = {}

        for ent in extracted.get("entities", []):
            type_ = (ent.get("type") or "").strip().lower()
            name = (ent.get("name") or "").strip()
            if not type_ or not name:
                continue
            attrs = ent.get("attributes") or {}
            entity, created = await upsert_entity(
                db,
                type_=type_,
                name=name,
                workspace_id=task.workspace_id,
                attributes=attrs,
                created_by="orchestrator",
            )
            name_to_id[name] = entity.id
            if created:
                new_entities += 1
            else:
                merged_entities += 1

        new_relations = 0
        for rel in extracted.get("relations", []):
            from_name = (rel.get("from") or "").strip()
            to_name = (rel.get("to") or "").strip()
            rel_type = (rel.get("type") or "").strip()
            if not (from_name and to_name and rel_type):
                continue
            from_id = name_to_id.get(from_name)
            to_id = name_to_id.get(to_name)
            if not from_id:
                from_id = await _resolve_entity_id_by_name(db, from_name, task.workspace_id)
            if not to_id:
                to_id = await _resolve_entity_id_by_name(db, to_name, task.workspace_id)
            if not (from_id and to_id):
                continue
            db.add(
                MemoryRelation(
                    from_id=from_id,
                    to_id=to_id,
                    relation_type=rel_type,
                    attributes=rel.get("attributes") or {},
                    workspace_id=task.workspace_id,
                )
            )
            new_relations += 1
        await db.commit()

        await log_event(
            db,
            "memory_extracted",
            "orchestrator",
            {
                "new_entities": new_entities,
                "merged_entities": merged_entities,
                "new_relations": new_relations,
            },
            task_id=task.id,
        )
        logger.info(
            f"Memory extracted for task {task.id}: "
            f"+{new_entities} entities, ~{merged_entities} merged, +{new_relations} relations"
        )


async def _resolve_entity_id_by_name(
    db: AsyncSession, name: str, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    row = (
        await db.execute(
            select(MemoryEntity.id)
            .where(
                MemoryEntity.name == name,
                MemoryEntity.workspace_id == workspace_id,
            )
            .limit(1)
        )
    ).first()
    return row[0] if row else None
