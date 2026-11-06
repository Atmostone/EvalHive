"""External benchmark package: only the SDK is imported."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark.connector import Connector, serve_stdio
from evalhive_benchmark.protocol import ProtocolError

FIXTURE = json.loads(Path(__file__).with_name("example.json").read_text())


class FakeConnector(Connector):
    manifest = FIXTURE["manifest"]

    async def handle(self, method, params, context):
        if method == "list_cases":
            return {"cases": [FIXTURE["case"]], "next_cursor": None}
        if method == "resolve_case":
            return FIXTURE["case"]
        if method == "validate":
            return {
                "accepted": True,
                "effective_config": {"prefix": "", **params["spec"]["config"]},
            }
        if method == "plan":
            if params["spec"].get("purpose") == "evaluation":
                return {
                    "purpose": "evaluation",
                    **{key: value for key, value in FIXTURE["plan"].items() if key != "agent"},
                }
            return FIXTURE["plan"]
        if method == "prepare":
            spec = params["spec"]
            if spec["config"].get("wait"):
                await context.cancelled.wait()
                raise ProtocolError("CANCELLED", "Preparation cancelled")
            result = await context.broker(
                spec["scope"],
                "example.provider",
                "allocate",
                {"resource": params["plan"]["resources"][0]},
                request_id="example-allocate",
            )
            await context.emit(
                spec["scope"], "environment.ready", {"session_handle": result["handle"]}
            )
            prepared = {
                "ready": True,
                "session_handle": result["handle"],
                "native_state": {"fixture": True},
                "artifacts": [],
            }
            if spec.get("purpose") == "evaluation":
                prepared.update(
                    purpose="evaluation", native_state=deepcopy(spec["checkpoint"]["state"])
                )
            else:
                prepared["agent"] = FIXTURE["plan"]["agent"]
            return prepared
        if method == "drive":
            if operation := params["payload"].get("agent_operation"):
                request_id = params["payload"].get("agent_request_id", "fixture-agent-call")
                data = params["payload"].get("agent_payload", {})
                session_id = params["payload"].get("agent_session")
                first = await context.agent(
                    params["scope"], operation, data, session_id=session_id, request_id=request_id
                )
                if params["payload"].get("repeat_agent"):
                    first = await context.agent(
                        params["scope"],
                        operation,
                        data,
                        session_id=session_id,
                        request_id=request_id,
                    )
                return {"finished": True, "agent": first}
            await context.emit(
                params["scope"], "agent.request", {"input_id": "turn-1", "text": "Fixture input"}
            )
            return {"finished": True}
        if method == "capture":
            return {"artifacts": [], "regrade": "available", "checkpoint": FIXTURE["checkpoint"]}
        if method == "evaluate":
            if params["payload"].get("agent_operation"):
                await context.agent(params["scope"], "run", {})
            return FIXTURE["evaluation"]
        if method == "aggregate":
            return {
                "metrics": [{"name": "success_rate", "value": 1}],
                "expected": 1,
                "observed": 1,
                "native": {},
            }
        if method == "cleanup":
            return {"cleaned": True}
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Unknown operation")


if __name__ == "__main__":
    asyncio.run(serve_stdio(FakeConnector()))
