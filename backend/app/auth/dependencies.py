import uuid

import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.database import get_db
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    token = auth[7:]
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token payload")
    user = await db.get(User, uuid.UUID(user_id))
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found or inactive")

    request.state.user = user
    return user


async def get_current_workspace(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    x_workspace_id: str | None = Header(default=None, alias="X-Workspace-Id"),
) -> Workspace:
    """Resolve workspace from X-Workspace-Id header (or JWT default), check membership."""
    ws_id_str = x_workspace_id
    if not ws_id_str:
        # fallback: token's default workspace
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            try:
                payload = decode_access_token(auth[7:])
                ws_id_str = payload.get("ws")
            except jwt.InvalidTokenError:
                pass
    if not ws_id_str:
        # last fallback: any membership
        result = await db.execute(
            select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id).limit(1)
        )
        row = result.scalar_one_or_none()
        if not row:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user has no workspace")
        ws_id_str = str(row)

    try:
        ws_id = uuid.UUID(ws_id_str)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid workspace id") from None

    member = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.workspace_id == ws_id,
        )
    )
    membership = member.scalar_one_or_none()
    if not membership:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not a member of this workspace")

    workspace = await db.get(Workspace, ws_id)
    if not workspace:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")

    request.state.workspace = workspace
    request.state.role = membership.role
    return workspace


def require_role(*allowed: str):
    """Dependency factory: ensures the current member's role is in allowed."""

    async def _check(
        user: User = Depends(get_current_user),
        workspace: Workspace = Depends(get_current_workspace),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        result = await db.execute(
            select(WorkspaceMember.role).where(
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.workspace_id == workspace.id,
            )
        )
        role = result.scalar_one_or_none()
        if role not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"requires role in {allowed}, have {role}",
            )

    return _check
