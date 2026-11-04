"""Temporary SDK boundary around the original agent loop; no copied LLM loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

from evalhive_harness import ProtocolError
from evalhive_harness.connector import Connector, RunContext, serve_stdio

ADAPTER_VERSION = "0.1.0"


class LegacyConnector(Connector):
    def __init__(self, agent, control, manifest=None, credentials=None):
        self.agent, self.control_state = agent, control
        self.manifest = manifest or json.loads(
            Path(__file__).with_name("manifest.json").read_text()
        )
        self.credentials = (
            credentials
            if credentials is not None
            else json.loads(Path(os.environ["EVALHIVE_CREDENTIALS_FILE"]).read_text())
        )
        self.execution = None

    async def validate(self, spec):
        model = spec.get("model") or {}
        if not model.get("name") or not model.get("endpoint"):
            raise ProtocolError("MODEL_REQUIRED", "Legacy connector requires a model connection")
        if model.get("api", "openai-compatible") != "openai-compatible":
            raise ProtocolError("UNSUPPORTED_MODEL_API", "Legacy requires an OpenAI compatible API")
        reference = model.get("credential_ref")
        if not reference or reference not in self.credentials:
            raise ProtocolError("CREDENTIAL_REQUIRED", "Model credential is unavailable")
        for reference in spec["config"].get("mcp_credential_refs", []):
            try:
                value = json.loads(self.credentials[reference])
                if not isinstance(value, dict):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise ProtocolError(
                    "INVALID_RESOURCE", "MCP configuration is unavailable"
                ) from None
        if spec["cwd"] != "/workspace":
            raise ProtocolError("UNSUPPORTED_CWD", "Legacy requires /workspace")
        return await super().validate(spec)

    def _configure(self, spec):
        model, config = spec["model"], spec["config"]
        values = {
            "TASK_ID": spec["task_id"],
            "TASK_DESCRIPTION": spec["task"]["prompt"],
            "AGENT_SOUL": "\n\n".join(spec.get("instructions", [])),
            "LLM_MODEL": model["name"],
            "OPENAI_BASE_URL": model["endpoint"],
            "OPENAI_API_KEY": self.credentials[model["credential_ref"]],
            "LLM_SSL_VERIFY": "1",
            "AGENT_MEMORY_CONTEXT": config.get("memory_context", ""),
            "AGENT_MAX_ITERATIONS": str(spec.get("limits", {}).get("max_turns", 20)),
            "MCP_SERVERS": json.dumps(
                [
                    json.loads(self.credentials[reference])
                    for reference in config.get("mcp_credential_refs", [])
                ]
            ),
        }
        if "tools" in config:
            values["AGENT_TOOLS"] = json.dumps(config["tools"])
            values["AGENT_TOOLS_EXPLICIT"] = "1"
        for key, native in (("temperature", "LLM_TEMPERATURE"), ("seed", "LLM_SEED")):
            if config.get(key) is not None:
                values[native] = str(config[key])
        if config.get("benchmark_mode"):
            values["AGENT_BENCHMARK_MODE"] = "1"
        if config.get("tool_injection"):
            values["AGENT_TOOL_INJECTION"] = config["tool_injection"]
        if config.get("pg_host"):
            values["PGHOST"] = values["PG_HOST"] = config["pg_host"]
        os.environ.update(values)
        # The SDK owns all writes. The legacy loop can only use its read-only
        # knowledge credential; it never receives the runner token or webhook URL.
        os.environ.pop("WEBHOOK_URL", None)
        if "knowledge_token" in self.credentials:
            os.environ["EVALHIVE_AGENT_TOKEN"] = self.credentials["knowledge_token"]

    async def run(self, context: RunContext):
        self._configure(context.spec)
        self.execution = asyncio.current_task()
        turn = 0

        async def native(name, payload):
            await context.emit(
                "native",
                {
                    "name": name,
                    "payload": payload,
                    "adapter_version": ADAPTER_VERSION,
                },
            )

        original_completion = self.agent._acompletion_with_retry

        async def completion(*args, **kwargs):
            nonlocal turn
            result = await original_completion(*args, **kwargs)
            turn += 1
            payload = result.model_dump(mode="json")
            await native("completion", payload)
            choice = payload["choices"][0]
            message = choice["message"]
            # The original loop extracts inline reasoning and emits it through
            # _send_log_chunk. Keep the authoritative answer on message.end.
            _, content = self.agent._extract_reasoning(result.choices[0].message)
            await context.emit(
                "message.end",
                {
                    "message_id": f"turn-{turn}",
                    "role": "assistant",
                    "content": content if content is not None else message.get("content"),
                    "stop_reason": choice.get("finish_reason"),
                },
            )
            usage = payload.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            await context.emit(
                "usage",
                {
                    "usage_id": f"turn-{turn}",
                    "mode": "cumulative",
                    "purpose": "agent",
                    "model": kwargs.get("model", "").removeprefix("openai/"),
                    "input_tokens": usage.get("prompt_tokens"),
                    "output_tokens": usage.get("completion_tokens"),
                    "reasoning_tokens": self.agent._reasoning_tokens(result.usage),
                    "cache_read_tokens": details.get("cached_tokens"),
                    "cache_write_tokens": None,
                    "cost": None,
                },
            )
            return result

        async def tool_call(name, arguments, call_id, seq_iter, *, reasoning=None):
            if reasoning:
                await context.emit(
                    "message.end",
                    {
                        "message_id": f"reasoning-{turn}",
                        "role": "assistant",
                        "channel": "reasoning",
                        "content": reasoning,
                    },
                )
            await context.emit(
                "tool.start",
                {
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                    "message_id": f"turn-{turn}",
                },
            )

        async def log_chunk(
            content,
            name,
            seq_iter,
            *,
            arguments=None,
            tool_call_id=None,
            part_offset=0,
            reasoning=None,
        ):
            if reasoning:
                await context.emit(
                    "message.end",
                    {
                        "message_id": f"reasoning-{turn}",
                        "role": "assistant",
                        "channel": "reasoning",
                        "content": reasoning,
                    },
                )
            if tool_call_id:
                await context.emit(
                    "tool.end",
                    {
                        "call_id": tool_call_id,
                        "result": content,
                        "status": "error" if content.startswith("ERROR:") else "completed",
                    },
                )

        self.agent._acompletion_with_retry = completion
        self.agent._send_tool_call = tool_call
        self.agent._send_log_chunk = log_chunk
        self.agent._send_progress = lambda payload: native("progress", payload)
        with redirect_stdout(sys.stderr):
            result = await self.agent.run_agent()
        await native("result", result)
        data = result.get("data", {})
        if result["event"] == "completed":
            return {"status": "success", "summary": data.get("result_summary", "Completed")}
        if result["event"] == "aborted":
            return {"status": "cancelled", "summary": data.get("reason", "Cancelled")}
        return {
            "status": "failed",
            "summary": data.get("error", "Legacy agent failed"),
            "error": {"code": "LEGACY_FAILURE", "message": data.get("error", "Agent failed")},
        }

    async def control(self, context, name, payload):
        if name == "abort":
            context.cancelled.set()
            if self.execution is not None:
                self.execution.cancel()
        elif name == "feedback":
            await self.control_state.queue.put({"type": "feedback", "message": payload["text"]})
        else:
            raise ProtocolError("UNSUPPORTED_CAPABILITY", "Command is unavailable")
        return {"accepted": True}


if __name__ == "__main__":
    # The loop has no stdout logging contract. Reserve stdout exclusively for
    # JSONL; logging uses stderr, which the runtime never persists as raw logs.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    os.environ["LLM_SSL_VERIFY"] = "1"
    sys.path.insert(0, "/app")
    with redirect_stdout(sys.stderr):
        import agent
        import feedback_server

    asyncio.run(serve_stdio(LegacyConnector(agent, feedback_server)))
