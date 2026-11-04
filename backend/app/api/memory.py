"""Memory entities and relations CRUD + manual extract trigger."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace
from app.database import get_db
from app.memory.extractor import extract_memory
from app.memory.store import (
    entity_to_dict,
    relation_to_dict,
    upsert_entity,
)
from app.models.memory import MemoryEntity, MemoryRelation
from app.models.task import Task
from app.models.workspace import Workspace

router = APIRouter(prefix="/api/memory", tags=["memory"])


class EntityCreate(BaseModel):
    type: str
    name: str
    attributes: dict = {}


class EntityUpdate(BaseModel):
    type: str | None = None
    name: str | None = None
    attributes: dict | None = None


class RelationCreate(BaseModel):
    from_id: uuid.UUID
    to_id: uuid.UUID
    relation_type: str
    attributes: dict = {}


@router.get("/entities")
async def list_entities(
    type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    query = select(MemoryEntity).where(MemoryEntity.workspace_id == workspace.id)
    if type:
        query = query.where(MemoryEntity.type == type)
    if search:
        like = f"%{search}%"
        query = query.where(
            or_(MemoryEntity.name.ilike(like), MemoryEntity.type.ilike(like))
        )
    query = query.order_by(MemoryEntity.updated_at.desc()).limit(limit)
    rows = (await db.execute(query)).scalars().all()
    return [entity_to_dict(e) for e in rows]


async def _get_scoped_entity(
    entity_id: uuid.UUID, workspace: Workspace, db: AsyncSession
) -> MemoryEntity:
    entity = await db.get(MemoryEntity, entity_id)
    if not entity or entity.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.get("/entities/{entity_id}")
async def get_entity(
    entity_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    entity = await _get_scoped_entity(entity_id, workspace, db)
    rels = (
        await db.execute(
            select(MemoryRelation).where(
                MemoryRelation.workspace_id == workspace.id,
                or_(
                    MemoryRelation.from_id == entity_id,
                    MemoryRelation.to_id == entity_id,
                ),
            )
        )
    ).scalars().all()
    return {
        **entity_to_dict(entity),
        "relations": [relation_to_dict(r) for r in rels],
    }


@router.post("/entities", status_code=201)
async def create_entity(
    body: EntityCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    entity, _ = await upsert_entity(
        db,
        type_=body.type,
        name=body.name,
        workspace_id=workspace.id,
        attributes=body.attributes,
        created_by="user",
    )
    return entity_to_dict(entity)


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: uuid.UUID,
    body: EntityUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    entity = await _get_scoped_entity(entity_id, workspace, db)
    if body.type is not None:
        entity.type = body.type
    if body.name is not None:
        entity.name = body.name
    if body.attributes is not None:
        entity.attributes = body.attributes
    await db.commit()
    await db.refresh(entity)
    return entity_to_dict(entity)


@router.delete("/entities/{entity_id}", status_code=204)
async def delete_entity(
    entity_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    entity = await _get_scoped_entity(entity_id, workspace, db)
    await db.delete(entity)
    await db.commit()


@router.get("/relations")
async def list_relations(
    from_id: uuid.UUID | None = Query(default=None),
    to_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    query = select(MemoryRelation).where(MemoryRelation.workspace_id == workspace.id)
    if from_id:
        query = query.where(MemoryRelation.from_id == from_id)
    if to_id:
        query = query.where(MemoryRelation.to_id == to_id)
    query = query.limit(limit)
    rows = (await db.execute(query)).scalars().all()
    return [relation_to_dict(r) for r in rows]


@router.post("/relations", status_code=201)
async def create_relation(
    body: RelationCreate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    src = await db.get(MemoryEntity, body.from_id)
    if not src or src.workspace_id != workspace.id:
        raise HTTPException(status_code=400, detail="from_id not found")
    dst = await db.get(MemoryEntity, body.to_id)
    if not dst or dst.workspace_id != workspace.id:
        raise HTTPException(status_code=400, detail="to_id not found")
    rel = MemoryRelation(
        from_id=body.from_id,
        to_id=body.to_id,
        relation_type=body.relation_type,
        attributes=body.attributes,
        workspace_id=workspace.id,
    )
    db.add(rel)
    await db.commit()
    await db.refresh(rel)
    return relation_to_dict(rel)


@router.delete("/relations/{relation_id}", status_code=204)
async def delete_relation(
    relation_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rel = await db.get(MemoryRelation, relation_id)
    if not rel or rel.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Relation not found")
    await db.delete(rel)
    await db.commit()


@router.post("/extract")
async def trigger_extract(
    task_id: uuid.UUID,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger memory extraction on a completed task."""
    task = await db.get(Task, task_id)
    if not task or task.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Task not found")
    asyncio.create_task(extract_memory(task_id))
    return {"status": "scheduled", "task_id": str(task_id)}
