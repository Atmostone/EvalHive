"""Memory entity/relation storage with embedding-based deduplication."""

import logging
import uuid

from qdrant_client.models import PointStruct
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.rag import (
    MEMORY_COLLECTION_NAME,
    ensure_collection,
    get_embeddings,
    get_qdrant_client,
)
from app.models.memory import MemoryEntity, MemoryRelation

logger = logging.getLogger(__name__)

DEDUP_THRESHOLD = 0.92


def _entity_text(type_: str, name: str, attributes: dict | None) -> str:
    parts = [f"{type_}:{name}"]
    if attributes:
        parts.extend(f"{k}={v}" for k, v in attributes.items() if isinstance(v, (str, int, float)))
    return " ".join(parts)


async def _embed_one(text: str) -> tuple[list[float], int]:
    vectors = await get_embeddings([text])
    return vectors[0], len(vectors[0])


async def upsert_entity(
    db: AsyncSession,
    type_: str,
    name: str,
    workspace_id: uuid.UUID,
    attributes: dict | None = None,
    created_by: str = "orchestrator",
) -> tuple[MemoryEntity, bool]:
    """Insert entity or merge attributes into a similar existing one (cosine ≥ DEDUP_THRESHOLD).

    Returns (entity, created). created=True when new entity was added.
    Dedup is workspace-scoped: matches are only against entities with same workspace_id.
    """
    text = _entity_text(type_, name, attributes)
    try:
        vector, dim = await _embed_one(text)
    except Exception as e:
        logger.warning(f"Memory embedding failed, falling back to no-dedup insert: {e}")
        vector, dim = None, None

    if vector is not None:
        qdrant = get_qdrant_client()
        try:
            from qdrant_client.models import FieldCondition, Filter, MatchValue

            ensure_collection(qdrant, dim=dim, name=MEMORY_COLLECTION_NAME)
            search = qdrant.query_points(
                collection_name=MEMORY_COLLECTION_NAME,
                query=vector,
                limit=1,
                query_filter=Filter(must=[
                    FieldCondition(
                        key="workspace_id",
                        match=MatchValue(value=str(workspace_id)),
                    ),
                ]),
            )
            top = (search.points or [None])[0]
            if top and top.score >= DEDUP_THRESHOLD:
                payload = top.payload or {}
                if payload.get("type") == type_:
                    existing_id = uuid.UUID(payload["entity_id"])
                    existing = await db.get(MemoryEntity, existing_id)
                    if existing is not None and existing.workspace_id == workspace_id:
                        merged = {**(existing.attributes or {}), **(attributes or {})}
                        existing.attributes = merged
                        await db.commit()
                        await db.refresh(existing)
                        logger.info(
                            f"Merged into existing entity {existing.id} "
                            f"({existing.type}:{existing.name}, score={top.score:.3f})"
                        )
                        return existing, False
        except Exception as e:
            logger.warning(f"Qdrant dedup lookup failed: {e}")

    entity = MemoryEntity(
        type=type_,
        name=name,
        attributes=attributes or {},
        created_by=created_by,
        workspace_id=workspace_id,
    )
    db.add(entity)
    await db.commit()
    await db.refresh(entity)

    if vector is not None:
        try:
            qdrant = get_qdrant_client()
            qdrant.upsert(
                collection_name=MEMORY_COLLECTION_NAME,
                points=[PointStruct(
                    id=str(entity.id),
                    vector=vector,
                    payload={
                        "entity_id": str(entity.id),
                        "type": type_,
                        "name": name,
                        "workspace_id": str(workspace_id),
                    },
                )],
            )
            entity.embedding_id = entity.id
            await db.commit()
            await db.refresh(entity)
        except Exception as e:
            logger.warning(f"Qdrant upsert for entity {entity.id} failed: {e}")

    return entity, True


async def find_relevant_entities(
    db: AsyncSession,
    query_text: str,
    workspace_id: uuid.UUID,
    limit: int = 10,
    threshold: float = 0.7,
) -> list[MemoryEntity]:
    """Embedding-based search across memory_entities (workspace-scoped)."""
    try:
        vector, dim = await _embed_one(query_text)
    except Exception as e:
        logger.warning(f"Memory search embedding failed: {e}")
        return []

    qdrant = get_qdrant_client()
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        ensure_collection(qdrant, dim=dim, name=MEMORY_COLLECTION_NAME)
        results = qdrant.query_points(
            collection_name=MEMORY_COLLECTION_NAME,
            query=vector,
            limit=limit,
            query_filter=Filter(must=[
                FieldCondition(
                    key="workspace_id",
                    match=MatchValue(value=str(workspace_id)),
                ),
            ]),
        )
    except Exception as e:
        logger.warning(f"Memory search failed: {e}")
        return []

    ids: list[uuid.UUID] = []
    for point in results.points:
        if point.score < threshold:
            continue
        payload = point.payload or {}
        ent_id = payload.get("entity_id")
        if ent_id:
            try:
                ids.append(uuid.UUID(ent_id))
            except ValueError:
                continue
    if not ids:
        return []

    rows = (
        await db.execute(
            select(MemoryEntity).where(
                MemoryEntity.id.in_(ids),
                MemoryEntity.workspace_id == workspace_id,
            )
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def expand_with_relations(
    db: AsyncSession, entities: list[MemoryEntity]
) -> tuple[list[MemoryEntity], list[MemoryRelation]]:
    """1-hop graph traversal. Returns (entities including 1-hop neighbours, relations)."""
    if not entities:
        return [], []
    seed_ids = {e.id for e in entities}
    rels = (
        await db.execute(
            select(MemoryRelation).where(
                (MemoryRelation.from_id.in_(seed_ids))
                | (MemoryRelation.to_id.in_(seed_ids))
            )
        )
    ).scalars().all()

    extra_ids = set()
    for r in rels:
        if r.from_id not in seed_ids:
            extra_ids.add(r.from_id)
        if r.to_id not in seed_ids:
            extra_ids.add(r.to_id)

    extras: list[MemoryEntity] = []
    if extra_ids:
        extras = (
            await db.execute(select(MemoryEntity).where(MemoryEntity.id.in_(extra_ids)))
        ).scalars().all()

    return [*entities, *extras], list(rels)


def serialize_context_md(
    entities: list[MemoryEntity], relations: list[MemoryRelation], max_chars: int = 8000
) -> str:
    """Compact markdown rendering of memory subgraph (≤~2k tokens at default cap)."""
    if not entities:
        return ""
    lines = ["# Relevant context"]
    by_id: dict[uuid.UUID, MemoryEntity] = {e.id: e for e in entities}
    grouped: dict[str, list[MemoryEntity]] = {}
    for e in entities:
        grouped.setdefault(e.type, []).append(e)

    for _type, items in grouped.items():
        for e in items:
            lines.append(f"## {e.type}:{e.name}")
            for k, v in (e.attributes or {}).items():
                if isinstance(v, (str, int, float, bool)):
                    lines.append(f"{k}: {v}")
    if relations:
        lines.append("### Relations")
        for r in relations:
            a = by_id.get(r.from_id)
            b = by_id.get(r.to_id)
            if not (a and b):
                continue
            lines.append(f"- {a.type}:{a.name} {r.relation_type} {b.type}:{b.name}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n# (truncated)"
    return text


async def build_memory_context(
    db: AsyncSession,
    query_text: str,
    workspace_id: uuid.UUID,
    top_k: int = 10,
    threshold: float = 0.7,
    max_chars: int = 8000,
) -> str:
    """Full pipeline: search → 1-hop expand → markdown."""
    seeds = await find_relevant_entities(
        db, query_text, workspace_id=workspace_id, limit=top_k, threshold=threshold
    )
    if not seeds:
        return ""
    expanded, rels = await expand_with_relations(db, seeds)
    return serialize_context_md(expanded, rels, max_chars=max_chars)


def entity_to_dict(e: MemoryEntity) -> dict:
    return {
        "id": str(e.id),
        "type": e.type,
        "name": e.name,
        "attributes": e.attributes or {},
        "created_by": e.created_by,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def relation_to_dict(r: MemoryRelation) -> dict:
    return {
        "id": str(r.id),
        "from_id": str(r.from_id),
        "to_id": str(r.to_id),
        "relation_type": r.relation_type,
        "attributes": r.attributes or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
