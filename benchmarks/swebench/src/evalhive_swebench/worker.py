"""Harness-neutral classic repository trials with private native evaluation."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, validate_spec
from evalhive_benchmark.connector import serve
from evalhive_benchmark.installation import validate_installation
from evalhive_benchmark.protocol import MAX_LINE_BYTES
from evalhive_benchmark.world_client import WorldClient

from .dataset import CONNECTOR_ID, CONNECTOR_VERSION
from .evaluation import MAX_PATCH_BYTES
from .native import REVISION, TASK_REVISION, VERSION, canonical
from .task_transfer import regular

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
DEFAULTS = {key: item["default"] for key, item in MANIFEST["config_schema"]["properties"].items()}
PROVIDER, STATE_SCHEMA = "evalhive.rootless", "swebench.patch.v1"


class SWEbenchConnector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, receipt=None, bindings=None):
        self.root, self.receipt, self.bindings = Path(root), receipt, bindings
        self.spec = self.config = self.binding = self.world = self.captured = None
        self.handles, self.sequence, self.ready = {}, 0, False
        self.deadline = 0

    def installed(self):
        if self.receipt is None:
            self.receipt = validate_installation(
                json.loads(regular(self.root / "installation.json", maximum=64 * 1024**2))
            )
        if self.bindings is None:
            self.bindings = json.loads(regular(self.root / "runtime.json", maximum=64 * 1024**2))
        return self.receipt

    def load_spec(self, spec):
        validate_spec(self.manifest, spec)
        try:
            receipt = self.installed()
            provenance = receipt["provenance"]
            cases = {case["ref"]["case_id"]: case for case in receipt["cases"]}
            selected = spec["case"]
            identity = selected["ref"]["case_id"]
            if (
                receipt["connector_id"] != CONNECTOR_ID
                or receipt["connector_version"] != CONNECTOR_VERSION
                or provenance["native_revision"] != REVISION
                or provenance["task_revision"] != TASK_REVISION
                or provenance["native_version"] != VERSION
                or cases.get(identity) != selected
                or self.bindings.get("format_version") != "1.0"
                or self.bindings.get("source_digest") != receipt["source_digest"]
                or set(self.bindings["cases"]) != set(cases)
                or spec["models"]
                or spec["credential_refs"]
            ):
                raise ValueError("Native catalog differs from its installed identity")
            binding = self.bindings["cases"][identity]
            if binding.get("available") is not True:
                raise ProtocolError(
                    "NATIVE_IMAGE_NOT_INSTALLED",
                    "The native instance image is not installed",
                    "unsupported",
                )
            if (
                binding["source_digest"] != selected["source_digest"]
                or selected["metadata"]["execution_availability"] != "installed"
            ):
                raise ValueError("Native image binding differs from its source")
            effective = {**deepcopy(spec), "config": {**DEFAULTS, **spec["config"]}}
            if self.spec is not None and self.spec != effective:
                raise ValueError("Native run configuration is frozen")
            if spec.get("purpose") == "evaluation":
                checkpoint = spec["checkpoint"]
                state = checkpoint["state"]
                if (
                    not checkpoint["complete"]
                    or checkpoint["state_schema"] != STATE_SCHEMA
                    or checkpoint["source_digest"] != selected["source_digest"]
                    or checkpoint["case"] != selected["ref"]
                    or len(checkpoint["artifacts"]) != 1
                    or effective["config"]["evaluation_mode"] != "native"
                    or checkpoint["connector"] != {"id": CONNECTOR_ID, "version": CONNECTOR_VERSION}
                    or set(state)
                    != {
                        "source_digest",
                        "base_commit",
                        "image_commit",
                        "patch_sha256",
                        "patch_size_bytes",
                    }
                    or state.get("source_digest") != selected["source_digest"]
                    or state.get("base_commit") != selected["native_ref"]["base_commit"]
                    or state.get("image_commit") != binding["image_commit"]
                    or not isinstance(state.get("patch_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", state["patch_sha256"])
                    or type(state.get("patch_size_bytes")) is not int
                    or not 0 <= state["patch_size_bytes"] <= MAX_PATCH_BYTES
                ):
                    raise ValueError(
                        "Native evaluation requires its complete frozen patch checkpoint"
                    )
            self.spec, self.config, self.binding = effective, effective["config"], binding
        except (ValueError, OSError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_CONFIGURATION_INVALID",
                "Native SWE-bench installation or configuration is invalid",
                "configuration",
            ) from None

    def agent(self):
        case = self.spec["case"]
        return {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": self.handles.get("task", "task"),
            "cwd": "/testbed",
            "prompt": case["description"],
            "instructions": [
                "Resolve the reported issue in the repository at /testbed. The repository starts at commit "
                + case["native_ref"]["base_commit"]
                + ". The testbed environment is installed. Edit the repository; your final patch is collected and evaluated independently."
            ],
            "resources": [
                {
                    "id": "repository-tools",
                    "kind": "tools",
                    "config": {"names": ["bash", "read", "write", "edit", "grep"]},
                }
            ],
            "process_policy": "retain_until_cleanup",
        }

    def plan(self):
        evaluation = self.spec.get("purpose") == "evaluation"
        resources = [
            {
                "id": name,
                "kind": "volume",
                "provider": PROVIDER,
                "config": {"asset_id": "swebench." + name},
            }
            for name in ("capture", "stage", "logs")
        ]
        config = {
            "asset_id": "swebench.world",
            "storage": {key: key for key in ("capture", "stage", "logs")},
        }
        if evaluation:
            config["retained_inputs"] = [self.spec["checkpoint"]["artifacts"][0]["id"]]
        resources.append(
            {
                "id": "world",
                "kind": "container",
                "provider": PROVIDER,
                "config": config,
                "depends_on": ["capture", "stage", "logs"],
            }
        )
        if not evaluation:
            resources.append(
                {
                    "id": "task",
                    "kind": "container",
                    "provider": PROVIDER,
                    "config": {"asset_id": self.binding["task"], "storage": {"capture": "capture"}},
                    "depends_on": ["capture", "world"],
                }
            )
        resources.append(
            {
                "id": "evaluator",
                "kind": "container",
                "provider": PROVIDER,
                "config": {
                    "asset_id": self.binding["evaluator"],
                    "storage": {"stage": "stage", "logs": "logs"},
                },
                "depends_on": ["stage", "logs", "world"],
            }
        )
        result = {
            "resources": resources,
            "grading_mode": "container_native",
            "requirements": {
                "capabilities": [
                    "host.retained_agent_processes_v1",
                    "host.exec_deadline_inheritance_v1",
                ]
            },
            "outputs": [
                {
                    "id": "patch",
                    "resource_id": "world",
                    "path": "/checkpoint",
                    "visibility": "user",
                    "required": False,
                    "stage": "capture",
                },
                {
                    "id": "report",
                    "resource_id": "world",
                    "path": "/records",
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation",
                },
            ],
        }
        if evaluation:
            result["purpose"] = "evaluation"
        else:
            result["agent"] = self.agent()
        return result

    def phase(self, seconds):
        self.deadline = time.monotonic() + min(seconds, self.spec["limits"]["wall_time_seconds"])
        if self.world is not None:
            self.world.deadline = self.deadline

    async def broker(self, context, operation, payload, *, resource=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError(
                "NATIVE_PHASE_TIMEOUT", "Native operation exceeded its deadline", "execution"
            )
        self.sequence += 1
        return await context.broker(
            self.spec["scope"],
            PROVIDER,
            operation,
            payload,
            **({"handle": self.handles[resource]} if resource else {}),
            request_id="swebench.broker." + str(self.sequence),
            timeout=remaining + 5,
            durable_host=True,
        )

    async def effect(self, context, pending):
        payload = pending["payload"]
        if (
            pending["operation"] != "exec"
            or set(payload) != {"resource", "argv", "cwd"}
            or payload["resource"] not in {"task", "evaluator"}
            or payload["resource"] not in self.handles
            or payload["cwd"] != "/testbed"
        ):
            raise ProtocolError(
                "NATIVE_EFFECT_UNAVAILABLE",
                "Native evaluator requested unavailable transport",
                "unsupported",
            )
        return await self.broker(
            context,
            "exec",
            {
                "argv": payload["argv"],
                "cwd": payload["cwd"],
                "timeout_seconds": self.deadline - time.monotonic(),
                "deadline_policy": "inherit",
                "process_policy": "stop_after_command",
            },
            resource=payload["resource"],
        )

    async def call(self, context, method, payload=None):
        return await self.world.call(
            context, method, payload, effect=lambda pending: self.effect(context, pending)
        )

    async def allocate(self, context, *, evaluator=False):
        for resource in self.plan()["resources"]:
            name = resource["id"]
            if name in self.handles or (name == "evaluator") != evaluator:
                continue
            result = await self.broker(context, "allocate", {"resource_id": name})
            self.handles[name] = result["handle"]

    async def prepare(self, context):
        self.phase(self.config["prepare_timeout_seconds"])
        await self.allocate(context)
        self.world = WorldClient(
            self.spec["scope"], self.handles["world"], deadline=self.deadline, prefix="swebench"
        )
        payload = {"case": self.spec["case"]}
        evaluation = self.spec.get("purpose") == "evaluation"
        if evaluation:
            payload["checkpoint"] = {
                **self.spec["checkpoint"]["state"],
                "artifact_id": self.spec["checkpoint"]["artifacts"][0]["id"],
            }
        result = await self.call(context, "prepare", payload)
        if result != {"prepared": True, "source_digest": self.spec["case"]["source_digest"]}:
            raise ProtocolError(
                "NATIVE_SOURCE_CHANGED", "Native evaluation source changed", "preparation"
            )
        self.ready = True
        return {
            "ready": True,
            "session_handle": self.handles["world"],
            "native_state": {"source_digest": self.spec["case"]["source_digest"]},
            "artifacts": [],
            **({"purpose": "evaluation"} if evaluation else {"agent": self.agent()}),
        }

    async def capture(self, context):
        if self.captured is not None:
            return deepcopy(self.captured)
        if not self.ready or self.spec.get("purpose") == "evaluation":
            return {"artifacts": [], "regrade": "unavailable"}
        self.phase(180)
        proof = await self.call(context, "capture")
        artifact = (await self.broker(context, "export", {"output_id": "patch"}, resource="world"))[
            "artifact"
        ]
        self.captured = {
            "artifacts": [artifact],
            "regrade": "available",
            "checkpoint": {
                "format_version": "1.0",
                "connector": {"id": CONNECTOR_ID, "version": CONNECTOR_VERSION},
                "case": self.spec["case"]["ref"],
                "source_digest": self.spec["case"]["source_digest"],
                "state_schema": STATE_SCHEMA,
                "complete": True,
                "artifacts": [artifact],
                "evaluator_roles": [],
                "state": proof,
            },
        }
        return deepcopy(self.captured)

    async def evaluate(self, context):
        if not self.ready or self.config["evaluation_mode"] == "deferred":
            return {
                "status": "unavailable",
                "success": None,
                "metrics": [],
                "native": {"reason": "NATIVE_EVALUATION_DEFERRED_OR_NOT_PREPARED"},
                "artifacts": [],
                "completeness": {"native_verdict": False},
            }
        self.phase(self.spec["limits"]["wall_time_seconds"])
        await self.allocate(context, evaluator=True)
        result = await self.call(context, "verify")
        report = (await self.broker(context, "export", {"output_id": "report"}, resource="world"))[
            "artifact"
        ]
        details = result["native"]["report"][self.spec["case"]["ref"]["case_id"]]
        infrastructure = details.get("infra_failure") is True
        return {
            "status": "error" if infrastructure else "completed",
            "success": None if infrastructure else result["success"],
            "metrics": []
            if infrastructure
            else [
                {
                    "name": "resolved",
                    "value": int(result["success"]),
                    "direction": "higher",
                    "aggregation": "native",
                }
            ],
            "native": result["native"],
            "artifacts": [report],
            "completeness": {"native_verdict": not infrastructure},
            **(
                {
                    "error": {
                        "code": "NATIVE_INFRASTRUCTURE_FAILURE",
                        "message": "The native evaluator classified an environment failure",
                        "kind": "grading",
                    }
                }
                if infrastructure
                else {}
            ),
        }

    async def handle(self, method, params, context):
        if method in {"list_cases", "resolve_case"}:
            receipt = self.installed()
            if method == "resolve_case":
                selected = next(
                    (case for case in receipt["cases"] if case["ref"] == params["case"]), None
                )
                if selected is None:
                    raise ProtocolError(
                        "CASE_UNAVAILABLE", "Native case is not installed", "configuration"
                    )
                return deepcopy(selected)
            if (params["dataset_id"], params["revision"]) != (
                receipt["dataset_id"],
                receipt["revision"],
            ):
                raise ProtocolError(
                    "DATASET_UNAVAILABLE", "Native dataset is not installed", "configuration"
                )
            cursor = params.get("cursor") or "0"
            if (
                not cursor.isdecimal()
                or len(cursor) > 8
                or str(int(cursor)) != cursor
                or int(cursor) > len(receipt["cases"])
            ):
                raise ProtocolError(
                    "INVALID_CURSOR", "Native catalog cursor is invalid", "configuration"
                )
            start = end = int(cursor)
            size = 4096
            for case in receipt["cases"][start : start + params.get("limit", 100)]:
                size += len(canonical(case))
                if size > MAX_LINE_BYTES:
                    break
                end += 1
            return {
                "cases": deepcopy(receipt["cases"][start:end]),
                "next_cursor": str(end) if end < len(receipt["cases"]) else None,
            }
        if "spec" in params:
            self.load_spec(params["spec"])
        if method == "validate":
            return {"accepted": True, "effective_config": self.config}
        if method == "plan":
            return self.plan()
        if method == "prepare":
            return await self.prepare(context)
        if method == "drive":
            if not self.ready:
                raise ProtocolError("NOT_PREPARED", "Native task is unavailable", "execution")
            self.phase(self.spec["limits"]["wall_time_seconds"])
            result = await context.agent(
                self.spec["scope"],
                "run",
                {},
                request_id="swebench.agent",
                timeout=self.spec["limits"]["wall_time_seconds"],
            )
            return {"agent_outcome": result["outcome"]}
        if method == "capture":
            return await self.capture(context)
        if method == "evaluate":
            return await self.evaluate(context)
        if method == "cancel":
            return {"cancelled": True}
        if method == "cleanup":
            if self.world is not None:
                self.phase(60)
                await self.call(context, "cleanup")
            return {"cleaned": True, "artifacts": []}
        return await super().handle(method, params, context)


def main():
    asyncio.run(serve(SWEbenchConnector(), sys.stdin.buffer, sys.stdout.buffer))


if __name__ == "__main__":
    main()
