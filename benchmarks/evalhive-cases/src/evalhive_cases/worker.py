"""Native case lifecycle over the public Benchmark SDK; no agent loop lives here."""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, serve_stdio, validate, validate_spec
from evalhive_benchmark.protocol import MAX_LINE_BYTES

from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, InstalledCases
from .format import CaseFormatError

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
PROVIDER = "evalhive.rootless"
STATE_SCHEMA = "evalhive-cases.answer.v1"
ANSWER_BYTES = 256 * 1024


def wire_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def unavailable(code):
    return {
        "status": "unavailable",
        "success": None,
        "metrics": [],
        "native": {"reason": code},
        "artifacts": [],
        "completeness": {"native_verdict": False},
    }


def normalized(value):
    return re.sub(r"\s+", " ", value.strip().lower())


class CasesConnector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, dataset=None):
        self.root, self.dataset = root, dataset
        self.spec, self.case, self.config = None, None, None
        self.answer, self.answer_status = None, "unknown"
        self.artifacts = []
        self.ready = False

    def installed(self):
        if self.dataset is None:
            self.dataset = InstalledCases(self.root)
        return self.dataset

    def _load(self, spec):
        validate_spec(self.manifest, spec)
        try:
            self.case = self.installed().resolve(spec["case"])
        except (CaseFormatError, OSError, ValueError):
            raise ProtocolError(
                "CASE_SOURCE_CHANGED",
                "Native case installation is unavailable or changed",
                "configuration",
            ) from None
        self.config = {
            "evaluation_mode": "deferred",
            "similarity_threshold": 0.9,
            "max_answer_chars": 262144,
            "resources": [],
            **spec["config"],
        }
        effective = {**deepcopy(spec), "config": deepcopy(self.config)}
        if self.spec is not None and self.spec != effective:
            raise ProtocolError(
                "SPEC_CONFLICT", "Worker run specification is frozen", "configuration"
            )
        self.spec = effective
        raw = self.case.native_data()
        if self.case.external_eval is not None:
            raise ProtocolError(
                "NATIVE_CONNECTOR_REQUIRED",
                "Executable Toolathlon cases require the toolathlon-gym connector",
                "unsupported",
            )
        if (raw.get("environment") or {}).get("required_services"):
            raise ProtocolError(
                "NATIVE_SERVICES_REQUIRED",
                "This case requires a specialized environment connector",
                "unsupported",
            )
        if raw["input"].get("context"):
            raise ProtocolError(
                "CONTEXT_BINDING_REQUIRED",
                "Native context references require an explicit environment binding",
                "unsupported",
            )
        required = set((raw.get("environment") or {}).get("mcp_servers", []))
        available = {
            resource["id"] for resource in self.config["resources"] if resource["kind"] == "mcp"
        }
        if required - available:
            raise ProtocolError(
                "MCP_BINDING_REQUIRED",
                "Required native MCP resources are not configured",
                "configuration",
            )
        if (
            self.config["evaluation_mode"] != "deferred"
            and not str((raw.get("gold") or {}).get("reference_answer") or "").strip()
        ):
            raise ProtocolError(
                "REFERENCE_REQUIRED",
                "Explicit reference matching requires a reference answer",
                "configuration",
            )
        if spec.get("purpose") == "evaluation":
            checkpoint = spec["checkpoint"]
            if checkpoint["state_schema"] != STATE_SCHEMA or not checkpoint["complete"]:
                raise ProtocolError(
                    "RESTORE_UNAVAILABLE",
                    "Native answer checkpoint is not restorable",
                    "configuration",
                )

    def _agent(self):
        return {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": "task",
            "cwd": "/workspace",
            "prompt": self.case.agent_input()["prompt"],
            "instructions": [],
            "resources": deepcopy(self.config["resources"]),
        }

    def _plan(self):
        if self.spec.get("purpose") == "evaluation":
            return {
                "purpose": "evaluation",
                "resources": [],
                "outputs": [],
                "requirements": {},
                "grading_mode": "snapshot",
            }
        return {
            "resources": [
                {
                    "id": "task",
                    "kind": "container",
                    "provider": PROVIDER,
                    "config": {"asset_id": "evalhive-cases.task"},
                }
            ],
            "agent": self._agent(),
            "outputs": [
                {
                    "id": "workspace",
                    "resource_id": "task",
                    "path": "/workspace",
                    "visibility": "user",
                    "required": True,
                }
            ],
            "requirements": {},
            "grading_mode": "snapshot",
        }

    async def _broker(self, context, scope, operation, payload, *, handle=None, request_id):
        return await context.broker(
            scope,
            PROVIDER,
            operation,
            payload,
            handle=handle,
            request_id=request_id,
            durable_host=True,
        )

    async def _prepare(self, params, context):
        spec = params["spec"]
        if spec.get("purpose") == "evaluation":
            state = spec["checkpoint"]["state"]
            answer = state.get("answer")
            if (
                not isinstance(answer, str)
                or not answer.strip()
                or len(answer) > self.config["max_answer_chars"]
                or wire_size(answer) > ANSWER_BYTES
            ):
                raise ProtocolError(
                    "RESTORE_FAILED", "Native answer checkpoint is invalid", "grading"
                )
            self.answer, self.answer_status = answer, "complete"
            self.ready = True
            return {
                "purpose": "evaluation",
                "ready": True,
                "session_handle": "answer-" + spec["scope"]["trial_id"],
                "native_state": {"answer_recording": "complete"},
                "artifacts": [],
            }
        allocated = await self._broker(
            context, spec["scope"], "allocate", {"resource_id": "task"}, request_id="prepare.task"
        )
        inspected = await self._broker(
            context,
            spec["scope"],
            "inspect",
            {},
            handle=allocated["handle"],
            request_id="prepare.inspect",
        )
        if (
            inspected.get("state") != "allocated"
            or (inspected.get("native") or {}).get("status") != "running"
        ):
            raise ProtocolError(
                "TASK_NOT_READY", "Native task environment is not ready", "preparation"
            )
        self.ready = True
        return {
            "ready": True,
            "session_handle": allocated["handle"],
            "agent": {**self._agent(), "environment_handle": allocated["handle"]},
            "native_state": {"case_source_digest": self.case.source_digest},
            "artifacts": [],
        }

    async def _drive(self, params, context):
        if not self.ready or self.spec.get("purpose") == "evaluation":
            raise ProtocolError(
                "NOT_PREPARED", "Execution environment is not prepared", "execution"
            )
        result = await context.agent(
            params["scope"],
            "run",
            {},
            request_id="case.agent",
            timeout=self.spec["limits"]["wall_time_seconds"],
        )
        cursor = 0
        while True:
            page = await context.agent(
                params["scope"],
                "events",
                {"cursor": cursor, "limit": 128},
                session_id=result["session_id"],
                request_id="case.events." + str(cursor),
            )
            for event in page["events"]:
                data = event.get("data", {})
                content = (
                    data.get("content")
                    if (
                        event.get("event") == "message.end"
                        and data.get("role") == "assistant"
                        and data.get("channel", "text") == "text"
                        and not event.get("parent_session_id")
                    )
                    else None
                )
                if isinstance(content, str) and content.strip():
                    if (
                        len(content) > self.config["max_answer_chars"]
                        or wire_size(content) > ANSWER_BYTES
                    ):
                        self.answer, self.answer_status = None, "limit_exceeded"
                    else:
                        self.answer, self.answer_status = content, "complete"
            if page["cursor"] == cursor:
                if not page["closed"]:
                    raise ProtocolError(
                        "HARNESS_RUNNING",
                        "Agent returned without a closed event stream",
                        "recording",
                    )
                break
            cursor = page["cursor"]
        return {"agent_outcome": result["outcome"]}

    async def _capture(self, params, context):
        if self.spec.get("purpose") != "evaluation":
            saved = await self._broker(
                context,
                params["scope"],
                "export",
                {"output_id": "workspace"},
                handle=params["payload"]["session_handle"],
                request_id="capture.workspace",
            )
            self.artifacts = [saved["artifact"]]
        result = {
            "artifacts": self.artifacts,
            "regrade": "available" if self.answer_status == "complete" else "unavailable",
        }
        if self.answer_status == "complete":
            result["checkpoint"] = {
                "format_version": "1.0",
                "connector": {"id": CONNECTOR_ID, "version": CONNECTOR_VERSION},
                "case": self.spec["case"]["ref"],
                "source_digest": self.case.source_digest,
                "state_schema": STATE_SCHEMA,
                "complete": True,
                "artifacts": [],
                "evaluator_roles": [],
                "state": {"answer": self.answer},
            }
        return result

    def _evaluate(self):
        mode = self.config["evaluation_mode"]
        if mode == "deferred":
            return unavailable("QUALITY_EVALUATION_DEFERRED")
        if self.answer_status != "complete":
            return unavailable("AGENT_ANSWER_" + self.answer_status.upper())
        reference = self.case.native_data()["gold"]["reference_answer"]
        score = (
            float(normalized(self.answer) == normalized(reference))
            if mode == "reference_exact"
            else SequenceMatcher(None, normalized(self.answer), normalized(reference)).ratio()
        )
        return {
            "status": "completed",
            "success": bool(score)
            if mode == "reference_exact"
            else score >= self.config["similarity_threshold"],
            "primary_metric": "reference_similarity",
            "metrics": [
                {
                    "name": "reference_similarity",
                    "value": score,
                    "direction": "higher",
                    "aggregation": "mean",
                }
            ],
            "native": {"evaluation_mode": mode, "legacy_gold_retained": True},
            "artifacts": [],
            "completeness": {"native_verdict": True, "answer_recording": "complete"},
        }

    async def handle(self, method, params, context):
        if method == "list_cases":
            installed = self.installed()
            if (params["dataset_id"], params["revision"]) != (
                installed.receipt["dataset_id"],
                installed.receipt["revision"],
            ):
                raise ProtocolError(
                    "DATASET_UNAVAILABLE",
                    "Requested dataset revision is not installed",
                    "configuration",
                )
            try:
                offset = int(params.get("cursor") or "0")
            except ValueError:
                raise ProtocolError(
                    "INVALID_CURSOR", "Case cursor is invalid", "configuration"
                ) from None
            if offset < 0 or str(offset) != (params.get("cursor") or "0"):
                raise ProtocolError("INVALID_CURSOR", "Case cursor is invalid", "configuration")
            cases = installed.receipt["cases"]
            if offset > len(cases):
                raise ProtocolError(
                    "INVALID_CURSOR", "Case cursor exceeds the installed dataset", "configuration"
                )
            end, size = offset, 4096
            for case in cases[offset : offset + params.get("limit", 100)]:
                size += wire_size(case) + 1
                if size > MAX_LINE_BYTES:
                    if end == offset:
                        raise ProtocolError(
                            "CASE_TOO_LARGE",
                            "Installed case cannot fit a protocol reply",
                            "configuration",
                        )
                    break
                end += 1
            return {
                "cases": deepcopy(cases[offset:end]),
                "next_cursor": str(end) if end < len(cases) else None,
            }
        if method == "resolve_case":
            descriptor = self.installed().descriptor(params["case"])
            self.installed().resolve(descriptor)
            return deepcopy(descriptor)
        if "spec" in params:
            self._load(params["spec"])
        if method == "validate":
            return {"accepted": True, "effective_config": self.config}
        if method == "plan":
            return self._plan()
        if method == "prepare":
            return await self._prepare(params, context)
        if method == "drive":
            return await self._drive(params, context)
        if method == "capture":
            return await self._capture(params, context)
        if method == "evaluate":
            return self._evaluate()
        if method == "aggregate":
            payload = params["payload"]
            results = payload.get("results", [])
            expected = payload.get("expected", len(results) if isinstance(results, list) else 0)
            if (
                set(payload) - {"results", "expected"}
                or not isinstance(results, list)
                or type(expected) is not int
                or expected < len(results)
            ):
                raise ProtocolError(
                    "INVALID_AGGREGATE", "Native aggregate population is invalid", "grading"
                )
            for result in results:
                validate("evaluation_result", result)
            known = [
                result["success"]
                for result in results
                if result["status"] == "completed" and result["success"] is not None
            ]
            return {
                "metrics": [
                    {
                        "name": "accuracy",
                        "value": sum(known) / len(known) if known else None,
                        "direction": "higher",
                        "aggregation": "mean",
                    }
                ],
                "expected": expected,
                "observed": len(results),
                "native": {"scored": len(known), "unscored": len(results) - len(known)},
            }
        if method == "cleanup":
            return {"cleaned": True}
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Unsupported native case method", "unsupported"
        )


if __name__ == "__main__":
    asyncio.run(serve_stdio(CasesConnector()))
