"""SDK-only lifecycle fixture; event-driven waits exercise real cancellation."""

import asyncio
import os

from evalhive_benchmark import Connector, ProtocolError, serve_stdio

AGENT = {
    "mode": "connector_container",
    "interaction": "autonomous",
    "cwd": "/tmp",
    "prompt": "Fixture",
    "instructions": [],
    "resources": [],
}
MANIFEST = {
    "protocol_version": "1.0",
    "kind": "benchmark",
    "id": "fixture.driver",
    "version": "1.0",
    "display_name": "Lifecycle fixture",
    "entrypoint": ["fixture"],
    "image": "fixture",
    "config_schema": {"type": "object"},
    "requirements": {},
    "capabilities": {
        method: "native"
        for method in ["prepare", "drive", "capture", "evaluate", "cleanup", "regrade"]
    },
}


class Worker(Connector):
    manifest = MANIFEST

    def __init__(self):
        self.config = {}

    async def handle(self, method, params, context):
        if "spec" in params:
            self.spec = params["spec"]
            self.config = params["spec"]["config"]
        scope = params.get("scope") or params.get("spec", {}).get("scope")
        if scope:
            await context.emit(scope, "phase.started", {"phase": method})
        prefix = "regrade_" if getattr(self, "spec", {}).get("purpose") == "evaluation" else ""
        if self.config.get(prefix + "die_phase") == method:
            os._exit(7)
        if self.config.get(prefix + "wait_phase") == method:
            await context.cancelled.wait()
            raise ProtocolError("CANCELLED", "Fixture stopped", "execution")
        if self.config.get(prefix + "fail_phase") == method:
            raise ProtocolError(
                "FIXTURE_FAILED",
                "Fixture operation failed",
                "grading"
                if method == "evaluate"
                else "cleanup"
                if method == "cleanup"
                else "execution",
            )
        if method == "validate":
            return {
                "accepted": True,
                "effective_config": {
                    **{"effective_marker": "resolved", **self.config},
                    **(
                        {"effective_marker": "changed"}
                        if prefix and self.config.get("regrade_default_drift")
                        else {}
                    ),
                },
            }
        if method == "plan":
            assert self.config["effective_marker"] == "resolved"
            plan = {
                "resources": [],
                "agent": AGENT,
                "outputs": [],
                "requirements": {},
                "grading_mode": "snapshot",
            }
            if self.spec.get("purpose") == "evaluation":
                del plan["agent"]
                plan["purpose"] = "evaluation"
            return plan
        if method == "prepare":
            assert self.config["effective_marker"] == "resolved"
            prepared = {
                "ready": True,
                "session_handle": "fixture-session",
                "agent": AGENT,
                "native_state": {},
                "artifacts": [],
            }
            if self.spec.get("purpose") == "evaluation":
                checkpoint = self.spec["checkpoint"]
                if checkpoint["state_schema"] != "fixture.inline.v1" or checkpoint["state"] != {
                    "captured": True
                }:
                    raise ProtocolError(
                        "RESTORE_FAILED", "Unsupported fixture checkpoint", "preparation"
                    )
                del prepared["agent"]
                prepared.update(purpose="evaluation", native_state=checkpoint["state"])
            return prepared
        if method == "drive":
            if self.config.get("use_host_agent"):
                result = await context.agent(
                    params["scope"], "run", {}, request_id="native.agent", timeout=20
                )
                return {"agent_outcome": result["outcome"]}
            return {"finished": True}
        if method == "capture":
            if self.config.get("malformed_capture"):
                return {"invalid": True}
            result = {"artifacts": [], "regrade": "available"}
            if self.config.get("checkpoint"):
                result["checkpoint"] = {
                    "format_version": "1.0",
                    "connector": {"id": MANIFEST["id"], "version": MANIFEST["version"]},
                    "case": self.spec["case"]["ref"],
                    "source_digest": self.spec["case"]["source_digest"],
                    "state_schema": "fixture.inline.v1",
                    "complete": True,
                    "artifacts": [],
                    "evaluator_roles": [],
                    "state": {"captured": True},
                }
            return result
        if method == "evaluate":
            correct = self.config.get("correct", True)
            return {
                "status": "completed",
                "success": correct,
                "metrics": [{"name": "success", "value": int(correct)}],
                "native": {"correct": correct},
                "artifacts": [],
                "completeness": {"state": "captured"},
            }
        if method == "cancel":
            return {"cancelled": True}
        if method == "cleanup":
            return {"cleaned": True}
        raise ProtocolError("UNSUPPORTED_CAPABILITY", "Fixture method unavailable")


if __name__ == "__main__":
    asyncio.run(serve_stdio(Worker()))
