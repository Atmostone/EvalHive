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
        if self.config.get("die_phase") == method:
            os._exit(7)
        if self.config.get("wait_phase") == method:
            await context.cancelled.wait()
            raise ProtocolError("CANCELLED", "Fixture stopped", "execution")
        if self.config.get("fail_phase") == method:
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
                "effective_config": {"effective_marker": "resolved", **self.config},
            }
        if method == "plan":
            assert self.config["effective_marker"] == "resolved"
            return {
                "resources": [],
                "agent": AGENT,
                "outputs": [],
                "requirements": {},
                "grading_mode": "snapshot",
            }
        if method == "prepare":
            assert self.config["effective_marker"] == "resolved"
            return {
                "ready": True,
                "session_handle": "fixture-session",
                "agent": AGENT,
                "native_state": {},
                "artifacts": [],
            }
        if method == "drive":
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
