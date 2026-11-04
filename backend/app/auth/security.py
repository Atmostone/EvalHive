import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.config import get_settings

_BCRYPT_MAX_BYTES = 72


def _truncate(plain: str) -> bytes:
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_truncate(plain), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _secret() -> str:
    s = get_settings().jwt_secret
    if not s:
        raise RuntimeError("JWT_SECRET is not configured")
    return s


def create_access_token(user_id: uuid.UUID, default_workspace_id: uuid.UUID | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expires_minutes)).timestamp()),
    }
    if default_workspace_id is not None:
        payload["ws"] = str(default_workspace_id)
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, _secret(), algorithms=[settings.jwt_algorithm])
