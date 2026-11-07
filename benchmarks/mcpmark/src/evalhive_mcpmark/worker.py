"""Drive a Harness against owned native MCPMark preparation and verification."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys
import time
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, validate_spec
from evalhive_benchmark.connector import serve
from evalhive_benchmark.protocol import MAX_LINE_BYTES

from .dataset import InstalledMCPMark
from .instructions import native_instruction
from .native import NativeConfigurationError, canonical
from .transcript import collect
from .world_stage import CHUNK, MAX_BYTES

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
PROVIDER = "evalhive.rootless"
DEFAULTS = {key: value["default"] for key, value in MANIFEST["config_schema"]["properties"].items()}


class MCPMarkConnector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, dataset=None, bindings=None):
        self.root, self.dataset, self.bindings = Path(root), dataset, bindings
        self.spec, self.task, self.config = None, None, None
        self.handles, self.sequence = {}, 0
        self.ready, self.completion, self.captured, self.transcript = False, None, None, None

    def installed(self):
        if self.dataset is None:
            path = self.root / "installation.json"
            if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
                raise NativeConfigurationError("Native installation metadata is unavailable")
            self.dataset = InstalledMCPMark(self.root / "native", json.loads(path.read_text()))
        return self.dataset

    def _load(self, spec):
        validate_spec(self.manifest, spec)
        try:
            installed = self.installed()
            task = installed.resolve(spec["case"])
            postgres = task.service == "postgres"
            assets = {"working", "connection", "world", "task"} | (
                {"network", "data", "secret", "postgres"} if postgres else set()
            )
            if self.bindings is None:
                path = self.root / "runtime.json"
                if path.is_symlink() or path.stat().st_size > 1024**2:
                    raise NativeConfigurationError("Native runtime metadata is unavailable")
                self.bindings = json.loads(path.read_text())
            if (
                self.bindings.get("format_version") != "1.0"
                or self.bindings.get("source_digest") != installed.receipt["source_digest"]
                or set(self.bindings.get("assets", {})) != assets
                or task.service not in {"filesystem", "postgres"}
                or not any(
                    asset.get("id")
                    == ("postgres.backups" if postgres else "filesystem." + task.category_id)
                    for asset in installed.receipt["provenance"].get("initial_assets", [])
                )
                or spec["models"]
                or set(spec["credential_refs"]) != ({"service_credentials"} if postgres else set())
                or spec.get("purpose") == "evaluation"
            ):
                raise NativeConfigurationError(
                    "Native service runtime or initial state is unavailable"
                )
            config = {**DEFAULTS, **spec["config"]}
            effective = {**deepcopy(spec), "config": config}
            if self.spec is not None and self.spec != effective:
                raise NativeConfigurationError("Native run specification is frozen")
            self.spec, self.task, self.config = effective, task, config
        except (NativeConfigurationError, OSError, ValueError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_CONFIGURATION_INVALID",
                "Native MCPMark source, service runtime or initial state is unavailable",
                "configuration",
            ) from None

    def _agent(self):
        return {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": self.handles.get("task", "task"),
            "cwd": "/workspace",
            "prompt": self.installed().manager.get_task_instruction(self.task),
            "instructions": [native_instruction()],
            "resources": [
                {"id": "native-tools", "kind": "tools", "config": {"names": ["mcp__mcpmark__*"]}},
                {
                    "id": "mcpmark",
                    "kind": "mcp",
                    "config": {
                        "transport": "stdio",
                        "command": "/usr/local/bin/python",
                        "args": ["/opt/environment/task_client.py"],
                        "cwd": "/workspace",
                    },
                },
            ],
        }

    def _plan(self):
        resources = [
            {
                "id": name,
                "kind": "volume",
                "provider": PROVIDER,
                "config": {"asset_id": self.bindings["assets"][name]},
            }
            for name in ("working", "connection")
        ]
        resources.extend(
            {
                "id": name,
                "kind": "container",
                "provider": PROVIDER,
                "config": {
                    "asset_id": self.bindings["assets"][name],
                    "storage": {"working": "working", "connection": "connection"},
                },
                "depends_on": ["working", "connection"],
            }
            for name in ("world", "task")
        )
        if self.task.service == "postgres":
            working, connection, world, task = resources
            extras = [
                {
                    "id": name,
                    "kind": kind,
                    "provider": PROVIDER,
                    "config": {"asset_id": self.bindings["assets"][name]},
                }
                for name, kind in (("network", "network"), ("data", "volume"), ("secret", "volume"))
            ]
            postgres = {
                "id": "postgres",
                "kind": "container",
                "provider": PROVIDER,
                "config": {
                    "asset_id": self.bindings["assets"]["postgres"],
                    "storage": {"data": "data", "secret": "secret"},
                    "network": "network",
                    "network_aliases": ["postgres"],
                },
                "depends_on": ["data", "secret", "network"],
            }
            world["config"]["storage"]["secret"] = "secret"
            for member, alias in ((world, "world"), (task, "task")):
                member["config"].update(network="network", network_aliases=[alias])
                member["depends_on"].append("network")
            world["depends_on"].append("secret")
            task["depends_on"].append("postgres")
            resources = [working, connection, *extras, world, postgres, task]
        return {
            "resources": resources,
            "agent": self._agent(),
            "requirements": {"capabilities": ["host.volume_copy_up_v1"]}
            if self.task.service == "postgres"
            else {},
            "grading_mode": "environment",
            "outputs": [
                {
                    "id": name,
                    "resource_id": "world",
                    "path": "/records/" + name,
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation" if name == "report" else "capture",
                }
                for name in ("transcript", "report")
            ]
            + [
                {
                    "id": "working",
                    "resource_id": "task",
                    "path": self.bindings.get("working_path", "/opt/native/.mcpmark_backups"),
                    "visibility": "user",
                    "required": False,
                    "stage": "capture",
                }
            ],
        }

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("NATIVE_PHASE_TIMEOUT", "Native operation expired", "execution")
        return min(remaining, 86400)

    async def broker(self, context, operation, payload=None, *, resource=None):
        self.sequence += 1
        return await context.broker(
            self.spec["scope"],
            PROVIDER,
            operation,
            payload or {},
            **({"handle": self.handles[resource]} if resource else {}),
            request_id="mcpmark.broker." + str(self.sequence),
            timeout=self.remaining() + 5,
            durable_host=True,
        )

    async def execute(self, context, module, args, *, private_refs=None):
        result = await self.broker(
            context,
            "exec",
            {
                "argv": ["/opt/venv/bin/python", "-m", "evalhive_mcpmark." + module, *args],
                "cwd": "/tmp",
                "timeout_seconds": self.remaining(),
                "deadline_policy": "inherit",
                "process_policy": "stop_after_command",
                **({"private_input_refs": private_refs} if private_refs else {}),
            },
            resource="world",
        )
        if result["exit_code"] != 0:
            raise ProtocolError(
                "NATIVE_CONTROL_FAILED", "Private native operation failed", "execution"
            )
        try:
            return json.loads(result["stdout"])
        except (ValueError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_CONTROL_FAILED", "Private native receipt is invalid", "recording"
            ) from None

    async def control(self, context, operation, payload):
        self.sequence += 1
        request = {
            "id": "world." + str(self.sequence),
            "scope": self.spec["scope"],
            "operation": operation,
            "payload": payload,
        }
        return await self.execute(
            context, "world_control", [base64.b64encode(canonical(request)).decode()]
        )

    async def native(self, context, method, params=None):
        started = await self.control(context, "begin", {"method": method, "params": params or {}})
        while True:
            result = await self.control(context, "poll", {"operation_id": started["operation_id"]})
            for effect in result["effects"]:
                if effect["operation"] != "postgres.restore" or self.task.service != "postgres":
                    raise ProtocolError(
                        "NATIVE_EFFECT_UNAVAILABLE",
                        "Native service requires an uninstalled transport",
                        "unsupported",
                    )
                payload = effect["payload"]
                receipt = await self.broker(
                    context,
                    "exec",
                    {
                        "argv": [
                            "sh",
                            "-c",
                            'IFS= read -r PGPASSWORD < /postgres-secret/password; export PGPASSWORD; exec "$@"',
                            "native-pg-restore",
                            *payload["argv"],
                        ],
                        "timeout_seconds": min(payload["timeout_seconds"], self.remaining()),
                        "deadline_policy": "inherit",
                        "process_policy": "stop_after_command",
                    },
                    resource="postgres",
                )
                await self.control(
                    context,
                    "resume",
                    {
                        "effect_id": effect["id"],
                        "result": {key: receipt[key] for key in ("exit_code", "stdout", "stderr")},
                    },
                )
            if result["state"] == "completed":
                return result["result"]
            if result["state"] == "failed":
                exception = result.get("error", {}).get("exception", "UnknownError")
                if not isinstance(exception, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z_0-9]{0,79}", exception
                ):
                    exception = "UnknownError"
                raise ProtocolError(
                    "NATIVE_SERVICE_FAILED",
                    f"Native service {method} failed ({exception})",
                    "execution",
                )
            await asyncio.sleep(min(0.2, self.remaining()))

    async def prepare(self, context):
        self.deadline = time.monotonic() + min(
            self.config["prepare_timeout_seconds"], self.spec["limits"]["wall_time_seconds"]
        )
        for resource in self._plan()["resources"]:
            result = await self.broker(context, "allocate", {"resource_id": resource["id"]})
            self.handles[resource["id"]] = result["handle"]
        if self.task.service == "postgres":
            await self.execute(
                context,
                "world_credentials",
                [],
                private_refs={"EVALHIVE_SERVICE_CREDENTIALS_FILE": "service_credentials"},
            )
        result = await self.native(context, "prepare", {"case": self.spec["case"]})
        if result["prompt"] != self._agent()["prompt"]:
            raise ProtocolError(
                "NATIVE_PROMPT_CHANGED",
                "Native task instruction changed after preparation",
                "preparation",
            )
        self.ready = True
        return {
            "ready": True,
            "session_handle": self.handles["world"],
            "agent": self._agent(),
            "native_state": {
                "source_digest": self.spec["case"]["source_digest"],
                "initial_asset_digest": result["initial_asset_digest"],
            },
            "artifacts": [],
        }

    async def capture(self, context):
        if self.captured is not None:
            return deepcopy(self.captured)
        self.deadline = time.monotonic() + 120
        artifacts = []
        if self.transcript is not None:
            data = canonical(self.transcript["messages"])
            if len(data) > MAX_BYTES:
                raise ProtocolError(
                    "NATIVE_TRANSCRIPT_LIMIT",
                    "Native messages exceed their byte budget",
                    "recording",
                )
            for offset in range(0, len(data), CHUNK):
                await self.execute(
                    context,
                    "world_stage",
                    [
                        "write",
                        base64.b64encode(
                            canonical(
                                {
                                    "offset": offset,
                                    "data": base64.b64encode(
                                        data[offset : offset + CHUNK]
                                    ).decode(),
                                }
                            )
                        ).decode(),
                    ],
                )
            digest = hashlib.sha256(data).hexdigest()
            await self.execute(
                context,
                "world_stage",
                [
                    "seal",
                    base64.b64encode(
                        canonical({"size_bytes": len(data), "sha256": digest})
                    ).decode(),
                ],
            )
            await self.native(
                context,
                "capture",
                {
                    "messages_sha256": digest,
                    "agent_success": self.completion["status"] == "success",
                },
            )
            artifacts.append(
                (
                    await self.broker(
                        context, "export", {"output_id": "transcript"}, resource="world"
                    )
                )["artifact"]
            )
        if "task" in self.handles:
            artifacts.append(
                (await self.broker(context, "export", {"output_id": "working"}, resource="task"))[
                    "artifact"
                ]
            )
        self.captured = {
            "artifacts": artifacts,
            "regrade": "unavailable",
            "regrade_reason": "NATIVE_SERVICE_STATE_NOT_RESTORABLE",
        }
        return deepcopy(self.captured)

    async def evaluate(self, context):
        if self.transcript is None or self.config["evaluation_mode"] == "deferred":
            return {
                "status": "unavailable",
                "success": None,
                "metrics": [],
                "artifacts": [],
                "native": {
                    "reason": "NATIVE_MESSAGES_UNAVAILABLE"
                    if self.transcript is None
                    else "QUALITY_EVALUATION_DEFERRED"
                },
                "completeness": {"native_verdict": False},
            }
        self.deadline = time.monotonic() + min(360, self.spec["limits"]["wall_time_seconds"])
        result = await self.native(context, "verify")
        artifact = (
            await self.broker(context, "export", {"output_id": "report"}, resource="world")
        )["artifact"]
        complete = result["native_verdict_complete"]
        return {
            "status": "completed" if complete else "error",
            "success": result["native_result"]["success"] if complete else None,
            "metrics": [],
            "artifacts": [artifact],
            "native": {
                "verifier_exit_code": result["verifier_exit_code"],
                "messages_interface": result["messages_interface"],
            },
            "completeness": {"native_verdict": complete},
        }

    async def handle(self, method, params, context):
        if method in {"list_cases", "resolve_case"}:
            installed = self.installed()
            cases = installed.receipt["cases"]
            if method == "resolve_case":
                selected = next((case for case in cases if case["ref"] == params["case"]), None)
                if selected is None:
                    raise ProtocolError(
                        "CASE_UNAVAILABLE", "Native case is not installed", "configuration"
                    )
                installed.resolve(selected)
                return deepcopy(selected)
            if (params["dataset_id"], params["revision"]) != (
                installed.receipt["dataset_id"],
                installed.receipt["revision"],
            ):
                raise ProtocolError(
                    "DATASET_UNAVAILABLE", "Native dataset is not installed", "configuration"
                )
            cursor = params.get("cursor") or "0"
            if (
                not cursor.isdecimal()
                or len(cursor) > 8
                or str(int(cursor)) != cursor
                or int(cursor) > len(cases)
            ):
                raise ProtocolError(
                    "INVALID_CURSOR", "Native case cursor is invalid", "configuration"
                )
            start, end, size = int(cursor), int(cursor), 4096
            for case in cases[start : start + params.get("limit", 100)]:
                size += len(canonical(case))
                if size > MAX_LINE_BYTES:
                    break
                end += 1
            return {
                "cases": deepcopy(cases[start:end]),
                "next_cursor": str(end) if end < len(cases) else None,
            }
        if "spec" in params:
            self._load(params["spec"])
        if method == "validate":
            return {"accepted": True, "effective_config": self.config}
        if method == "plan":
            return self._plan()
        if method == "prepare":
            return await self.prepare(context)
        if method == "drive":
            if not self.ready:
                raise ProtocolError(
                    "NOT_PREPARED", "Native environment is unavailable", "execution"
                )
            self.deadline = time.monotonic() + self.spec["limits"]["wall_time_seconds"]
            self.completion = await context.agent(
                self.spec["scope"], "run", {}, request_id="mcpmark.agent", timeout=self.remaining()
            )
            # Agent APIs are scoped to driving. Capture persists this confirmed
            # transcript after the host has advanced to the capture phase.
            self.transcript = await collect(
                context, self.spec["scope"], self.completion, timeout=self.remaining()
            )
            return {"agent_outcome": self.completion["outcome"]}
        if method == "capture":
            return await self.capture(context)
        if method == "evaluate":
            return await self.evaluate(context)
        if method == "cleanup":
            if "world" in self.handles:
                self.deadline = time.monotonic() + 120
                return await self.native(context, "cleanup")
            return {"cleaned": True}
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Native MCPMark operation is unsupported", "unsupported"
        )


async def main():
    wire = sys.stdout.buffer
    sys.stdout = sys.stderr
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES + 2)
    await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )

    async def lines():
        while line := await reader.readline():
            yield line

    async def write(frame):
        wire.write(frame)
        wire.flush()

    await serve(MCPMarkConnector(), lines(), write)


if __name__ == "__main__":
    asyncio.run(main())
