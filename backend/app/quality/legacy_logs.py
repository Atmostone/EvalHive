"""Compaction for historical agent logs; new executions use attempt journals."""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task

logger = logging.getLogger(__name__)


async def compact_agent_log(db: AsyncSession, task: Task) -> None:
    """Serialize all agent_log_chunks for this task → MinIO blob, then prune from DB.

    Uses the JSON-lines archive format (`encode_log_archive`) so each chunk keeps its
    `tool_name` — the cleaned trace (E-06) and trajectory matcher (E-09) stay tool-aware
    after compaction. Atomic: upload first, set s3_path, DELETE rows in same transaction —
    if upload fails, nothing changes.
    """
    from app.models.agent_log import AgentLogChunk
    from app.storage.minio_client import encode_log_archive, upload_log_archive

    if task.log_archive_s3_path:
        return  # idempotent — already compacted on a prior delivery

    result = await db.execute(
        select(AgentLogChunk)
        .where(AgentLogChunk.task_id == task.id)
        .order_by(AgentLogChunk.chunk_seq)
    )
    chunks = result.scalars().all()
    if not chunks:
        return

    blob = encode_log_archive(chunks)
    s3_path = await asyncio.to_thread(upload_log_archive, str(task.id), blob)

    task.log_archive_s3_path = s3_path
    await db.execute(
        AgentLogChunk.__table__.delete().where(AgentLogChunk.task_id == task.id)
    )
    await db.commit()
    logger.info(
        f"Compacted {len(chunks)} log chunks for task {task.id} → {s3_path}"
    )


