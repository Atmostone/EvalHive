import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator


# 256 KB cap per chunk — agent splits longer outputs into N consecutive chunks.
MAX_CHUNK_BYTES = 256 * 1024

# Tool-call arguments (SPA-86). The agent already clips these before sending;
# this is the same guard applied server-side, because "the client promised" is
# not a size limit. Kept generous — the judge's own, much tighter token cap is
# applied later by the trace cleaner.
ARG_VALUE_MAX_CHARS = 8 * 1024
ARGS_MAX_BYTES = 64 * 1024


def clip_arguments(args: dict | None) -> tuple[dict | None, bool]:
    """Shrink oversized tool-call arguments. Long string values are cut to a head
    plus an explicit marker; keys are never dropped, because which parameters were
    passed is the signal. Idempotent: already-clipped arguments pass through
    unchanged. Returns (clipped, was_truncated)."""
    if not isinstance(args, dict) or not args:
        return (args if isinstance(args, dict) else None), False

    truncated = False

    def _clip(v: Any, limit: int) -> Any:
        nonlocal truncated
        if isinstance(v, str) and len(v) > limit:
            truncated = True
            return f"{v[:limit]}…[truncated {len(v) - limit} chars]…"
        if isinstance(v, dict):
            return {k: _clip(x, limit) for k, x in v.items()}
        if isinstance(v, list):
            return [_clip(x, limit) for x in v]
        return v

    limit = ARG_VALUE_MAX_CHARS
    out = args
    for _ in range(8):
        out = {k: _clip(v, limit) for k, v in args.items()}
        try:
            size = len(json.dumps(out, ensure_ascii=False, default=str).encode("utf-8"))
        except Exception:
            return {"_unserializable": str(list(args.keys()))}, True
        if size <= ARGS_MAX_BYTES:
            return out, truncated
        limit = max(64, limit // 2)
    return out, True


class AgentLogChunkIn(BaseModel):
    chunk_seq: int = Field(..., ge=0)
    content: str = Field(..., max_length=MAX_CHUNK_BYTES)
    tool_name: Optional[str] = Field(None, max_length=255)
    # SPA-86: the call that produced this output. `arguments` is what the process
    # judge needs to score parameter_quality at all; `tool_call_id` + `part_index`
    # let the cleaner re-join a split output into the single call it came from.
    arguments: Optional[dict] = None
    arguments_truncated: bool = False
    tool_call_id: Optional[str] = Field(None, max_length=128)
    part_index: int = Field(0, ge=0)
    part_total: int = Field(1, ge=1)
    idempotency_key: str = Field(..., min_length=1, max_length=64)

    @model_validator(mode="after")
    def _clip(self):
        clipped, was_clipped = clip_arguments(self.arguments)
        self.arguments = clipped
        # OR, never overwrite: the agent may have already clipped a value the
        # server-side pass now finds short enough, and that truncation still
        # happened.
        self.arguments_truncated = bool(self.arguments_truncated or was_clipped)
        return self


class AgentLogChunkOut(BaseModel):
    id: str
    chunk_seq: int
    content: str
    tool_name: Optional[str] = None
    arguments: Optional[dict] = None
    arguments_truncated: bool = False
    tool_call_id: Optional[str] = None
    part_index: int = 0
    part_total: int = 1
    created_at: datetime
