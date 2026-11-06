"""Knowledge base API: rules.md, memory.md, RAG documents."""

import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_workspace, require_role
from app.auth.tokens import verify_agent_token
from app.config import get_settings
from app.database import get_db
from app.models.knowledge_document import KnowledgeDocument
from app.models.task import Task
from app.models.workspace import Workspace
from app.utils.events import log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _write_file(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


# --- rules.md --- (per-workspace shared file)

def _shared_path(workspace: Workspace, name: str) -> str:
    settings = get_settings()
    return os.path.join(settings.data_dir, "shared", str(workspace.id), name)


@router.get("/rules")
async def get_rules(workspace: Workspace = Depends(get_current_workspace)):
    return {"content": _read_file(_shared_path(workspace, "rules.md"))}


class ContentUpdate(BaseModel):
    content: str


@router.put("/rules", dependencies=[Depends(require_role("owner", "admin"))])
async def update_rules(
    body: ContentUpdate,
    workspace: Workspace = Depends(get_current_workspace),
):
    _write_file(_shared_path(workspace, "rules.md"), body.content)
    return {"status": "ok"}


# --- memory.md ---

@router.get("/memory")
async def get_memory(workspace: Workspace = Depends(get_current_workspace)):
    return {"content": _read_file(_shared_path(workspace, "memory.md"))}


@router.put("/memory", dependencies=[Depends(require_role("owner", "admin", "member"))])
async def update_memory(
    body: ContentUpdate,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    _write_file(_shared_path(workspace, "memory.md"), body.content)
    await log_event(
        db, "memory_updated", "user",
        {"size_chars": len(body.content)},
        workspace_id=workspace.id,
    )
    return {"status": "ok"}


# --- Documents (RAG) ---

@router.get("/documents")
async def list_documents(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(KnowledgeDocument)
        .where(KnowledgeDocument.workspace_id == workspace.id)
        .order_by(KnowledgeDocument.created_at.desc())
    )
    docs = result.scalars().all()
    return [
        {
            "id": str(d.id),
            "filename": d.filename,
            "s3_path": d.s3_path,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in docs
    ]


@router.post(
    "/documents",
    dependencies=[Depends(require_role("owner", "admin", "member"))],
)
async def upload_document(
    file: UploadFile = File(...),
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Upload a document: store in MinIO, chunk text, index in Qdrant."""
    from app.knowledge.rag import process_document

    content = await file.read()
    filename = file.filename or "unknown"

    try:
        doc = await process_document(db, filename, content, workspace_id=workspace.id)
        return {
            "id": str(doc.id),
            "filename": doc.filename,
            "chunk_count": doc.chunk_count,
        }
    except Exception as e:
        logger.error(f"Document upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from None


@router.delete(
    "/documents/{doc_id}",
    dependencies=[Depends(require_role("owner", "admin"))],
)
async def delete_document(
    doc_id: str,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Delete document from DB, MinIO, and Qdrant."""
    from app.knowledge.rag import delete_document_data

    doc = await db.get(KnowledgeDocument, uuid.UUID(doc_id))
    if not doc or doc.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="Document not found")

    await delete_document_data(db, doc)
    return {"status": "deleted"}


@router.post("/search")
async def search_knowledge(
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Search documents via Qdrant vector similarity.

    Authenticates with either:
      • Bearer user JWT + X-Workspace-Id header (regular UI use), OR
      • Bearer agent service token + task_id in body (agent in-container use).
    """
    from app.knowledge.rag import search_documents

    query = body.get("query", "")
    limit = body.get("limit", 5)
    if not query.strip():
        raise HTTPException(status_code=400, detail="Query is required")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth[7:]

    workspace_id: uuid.UUID | None = None

    # Try agent token first (requires task_id in body)
    task_id_str = body.get("task_id")
    if task_id_str:
        try:
            task_uuid = uuid.UUID(task_id_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid task_id") from None
        st = await verify_agent_token(db, plain=token, task_id=task_uuid)
        if st:
            task = await db.get(Task, task_uuid)
            if task:
                workspace_id = task.workspace_id

    if workspace_id is None:
        # Fall back to user JWT auth
        from app.auth.dependencies import get_current_user
        from app.auth.dependencies import get_current_workspace as _gcws

        user = await get_current_user(request, db)
        ws = await _gcws(request, user, db)
        workspace_id = ws.id

    results = await search_documents(query, workspace_id=workspace_id, limit=limit)
    return {"results": results}


@router.post("/reset", dependencies=[Depends(require_role("owner", "admin"))])
async def reset_knowledge(
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Drop Qdrant collection + delete all documents (DB+MinIO). Used when switching embedding providers."""
    from app.knowledge.rag import reset_collection

    result = await reset_collection(db, workspace_id=workspace.id)
    await log_event(db, "knowledge_reset", "user", result, workspace_id=workspace.id)
    return result
