"""HTTP control plane (port 8080) for orchestrator → agent commands.

Runs in parallel to the LLM tool-loop. Communicates with `agent.py` via:
- a shared mutable `state` dict (current step, tokens, abort flag)
- an asyncio.Queue of pending commands consumed by the loop
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

# Shared state mutated by agent.py
state: dict[str, Any] = {
    "started_at": time.time(),
    "current_step": "initializing",
    "tokens_input": 0,
    "tokens_output": 0,
    "iteration": 0,
    "abort_requested": False,
    "abort_reason": None,
}

queue: asyncio.Queue[dict] = asyncio.Queue()

app = FastAPI(title="EvalHive agent control")


class FeedbackBody(BaseModel):
    message: str


class SwitchModelBody(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class AbortBody(BaseModel):
    reason: str = "user requested"


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "uptime_sec": int(time.time() - state["started_at"]),
        "current_step": state["current_step"],
        "iteration": state["iteration"],
        "tokens_used": {
            "input": state["tokens_input"],
            "output": state["tokens_output"],
        },
        "abort_requested": state["abort_requested"],
    }


@app.post("/feedback")
async def feedback(body: FeedbackBody) -> dict:
    await queue.put({"type": "feedback", "message": body.message})
    return {"status": "queued"}


@app.post("/switch_model")
async def switch_model(body: SwitchModelBody) -> dict:
    await queue.put({
        "type": "switch_model",
        "model": body.model,
        "base_url": body.base_url,
        "api_key": body.api_key,
    })
    return {"status": "queued"}


@app.post("/abort")
async def abort(body: AbortBody) -> dict:
    state["abort_requested"] = True
    state["abort_reason"] = body.reason
    await queue.put({"type": "abort", "reason": body.reason})
    return {"status": "abort_signaled"}


async def start_feedback_server() -> asyncio.Task:
    """Launch uvicorn as an asyncio task. Does not block."""
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="warning")
    server = uvicorn.Server(config)
    return asyncio.create_task(server.serve())
