"""Standalone external connector: only the SDK is imported, never EvalHive backend."""

import asyncio
import json
from pathlib import Path

from evalhive_harness.connector import Connector, RunContext, serve_stdio
from evalhive_harness.protocol import ProtocolError


class FakeConnector(Connector):
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text())

    async def validate(self, spec):
        return {"accepted": True, "effective_config": {"prefix": "", "wait": False, "fail": False, **spec["config"]}}

    async def run(self, context: RunContext) -> dict:
        if context.conversation is not None:
            conversation = context.conversation
            summary = "Conversation closed"
            while incoming := await conversation.next_input():
                content = "\n".join(message["content"] for message in incoming["messages"])
                if content == "tools":
                    results = await conversation.request_tools("message-" + incoming["turn_id"], "batch-" + incoming["turn_id"], [
                        {"call_id": "first", "name": "echo", "arguments": {"text": "one"}},
                        {"call_id": "second", "name": "echo", "arguments": {"text": "two"}},
                    ])
                    summary = json.dumps([result["content"] for result in results])
                elif content == "wait":
                    await context.cancelled.wait()
                    continue
                else:
                    summary = "Echo: " + content
                await conversation.complete_turn("final-" + incoming["turn_id"], summary)
            return {"status": "success", "summary": summary}
        config = context.spec["config"]
        await context.emit(
            "message.end",
            {
                "message_id": "answer",
                "role": "assistant",
                "content": config.get("prefix", "") + "started",
            },
        )
        if config.get("wait"):
            await context.cancelled.wait()
            return {"status": "cancelled", "summary": "Run cancelled"}
        if config.get("fail"):
            raise RuntimeError("Internal diagnostics must not leak into protocol errors")
        await context.emit("artifact", {"path": "result.txt"})
        return {"status": "success", "summary": "Fake completed"}

    async def control(self, context: RunContext, name: str, payload: dict):
        if name == "abort":
            context.cancelled.set()
        elif name == "feedback":
            await context.emit(
                "message.end",
                {"message_id": "feedback", "role": "user", "content": payload["text"]},
            )
        else:
            raise ProtocolError("UNSUPPORTED_CAPABILITY", "Unavailable command")
        return {"accepted": True}


if __name__ == "__main__":
    asyncio.run(serve_stdio(FakeConnector()))
