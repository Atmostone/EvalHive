"""Run any installed harness against an isolated native AppWorld MCP service."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, validate_spec
from evalhive_benchmark.connector import serve
from evalhive_benchmark.protocol import MAX_LINE_BYTES
from evalhive_benchmark.world_client import WorldClient

from .dataset import InstalledAppWorld
from .groups import native_groups
from .native import canonical

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
DEFAULTS = {key: item["default"] for key, item in MANIFEST["config_schema"]["properties"].items()}
PROVIDER = "evalhive.rootless"
ASSETS = {"working", "connection", "state", "network", "world", "task"}


class AppWorldConnector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, dataset=None, bindings=None):
        self.root, self.dataset, self.bindings = Path(root), dataset, bindings
        self.spec = self.task = self.config = self.world = self.completion = self.captured = None
        self.handles, self.sequence, self.ready = {}, 0, False

    def installed(self):
        if self.dataset is None:
            path = self.root / "installation.json"
            if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
                raise ValueError("Native dataset metadata is unavailable")
            self.dataset = InstalledAppWorld(self.root / "native", json.loads(path.read_text()))
        return self.dataset

    def load_spec(self, spec):
        validate_spec(self.manifest, spec)
        try:
            installed = self.installed()
            task = installed.resolve(spec["case"])
            if self.bindings is None:
                path = self.root / "runtime.json"
                if path.is_symlink() or path.stat().st_size > 1024**2:
                    raise ValueError("Native runtime metadata is unavailable")
                self.bindings = json.loads(path.read_text())
            if (
                self.bindings.get("format_version") != "1.0"
                or self.bindings.get("source_digest") != installed.receipt["source_digest"]
                or set(self.bindings.get("assets", {})) != ASSETS
                or spec["models"]
                or spec["credential_refs"]
                or spec.get("purpose") == "evaluation"
            ):
                raise ValueError("Native runtime configuration is unavailable")
            effective = {**deepcopy(spec), "config": {**DEFAULTS, **spec["config"]}}
            if self.spec is not None and self.spec != effective:
                raise ValueError("Native run configuration is frozen")
            self.spec, self.config, self.task = effective, effective["config"], task
        except (ValueError, OSError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_CONFIGURATION_INVALID",
                "Native AppWorld dataset or runtime is invalid",
                "configuration",
            ) from None

    def agent(self):
        groups = native_groups()
        return {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": self.handles.get("task", "task"),
            "cwd": "/workspace",
            "prompt": self.task.instruction,
            "instructions": [
                "Use the provided native AppWorld MCP tools to complete the task. "
                "The supervisor identity is "
                + canonical(dict(self.task.supervisor)).decode()
                + ". Call supervisor__complete_task when finished."
            ],
            "resources": [
                {
                    "id": "native-tools",
                    "kind": "tools",
                    "config": {"names": ["mcp__" + group["id"] + "__*" for group in groups]},
                },
                *[
                    {
                        "id": group["id"],
                        "kind": "mcp",
                        "config": {
                            "transport": "stdio",
                            "command": "/opt/venv/bin/python",
                            "args": ["/opt/environment/appworld_mcp.py", group["id"]],
                            "cwd": "/workspace",
                        },
                    }
                    for group in groups
                ],
            ],
        }

    def plan(self):
        resources = [
            {
                "id": name,
                "kind": "network" if name == "network" else "volume",
                "provider": PROVIDER,
                "config": {"asset_id": self.bindings["assets"][name]},
            }
            for name in ("working", "connection", "state", "network")
        ]
        resources += [
            {
                "id": name,
                "kind": "container",
                "provider": PROVIDER,
                "config": {
                    "asset_id": self.bindings["assets"][name],
                    "network": "network",
                    "network_aliases": ["appworld" if name == "world" else "task"],
                    "storage": {"connection": "connection", "state": "state"}
                    if name == "world"
                    else {"connection": "connection", "working": "working"},
                },
                "depends_on": ["connection", "state", "network"]
                if name == "world"
                else ["connection", "working", "network", "world"],
            }
            for name in ("world", "task")
        ]
        return {
            "resources": resources,
            "agent": self.agent(),
            "grading_mode": "environment",
            "requirements": {"capabilities": ["host.resource_finalizers_v1"]},
            "outputs": [
                {
                    "id": name,
                    "resource_id": "world",
                    "path": path,
                    "visibility": "evaluator",
                    "required": False,
                    "stage": stage,
                }
                for name, path, stage in (
                    ("state", "/appworld/experiments/outputs", "capture"),
                    ("records", "/records", "capture"),
                    ("report", "/records/report", "evaluation"),
                )
            ]
            + [
                {
                    "id": "working",
                    "resource_id": "task",
                    "path": "/workspace",
                    "visibility": "user",
                    "required": False,
                    "stage": "capture",
                }
            ],
        }

    async def broker(self, context, operation, payload, *, resource=None):
        self.sequence += 1
        return await context.broker(
            self.spec["scope"],
            PROVIDER,
            operation,
            payload,
            **({"handle": self.handles[resource]} if resource else {}),
            request_id="appworld.broker." + str(self.sequence),
            timeout=185,
            durable_host=True,
        )

    async def prepare(self, context):
        for resource in self.plan()["resources"]:
            result = await self.broker(context, "allocate", {"resource_id": resource["id"]})
            self.handles[resource["id"]] = result["handle"]
        self.world = WorldClient(
            self.spec["scope"],
            self.handles["world"],
            deadline=time.monotonic()
            + min(self.config["prepare_timeout_seconds"], self.spec["limits"]["wall_time_seconds"]),
            prefix="appworld",
        )
        result = await self.world.call(context, "prepare", {"case": self.spec["case"]})
        if result != {
            "prompt": self.task.instruction,
            "supervisor": dict(self.task.supervisor),
            "groups": native_groups(),
        }:
            raise ProtocolError(
                "NATIVE_PROMPT_CHANGED", "Native public task context changed", "preparation"
            )
        self.ready = True
        return {
            "ready": True,
            "session_handle": self.handles["world"],
            "agent": self.agent(),
            "native_state": {"source_digest": self.spec["case"]["source_digest"]},
            "artifacts": [],
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
            self.load_spec(params["spec"])
        if method == "validate":
            return {"accepted": True, "effective_config": self.config}
        if method == "plan":
            return self.plan()
        if method == "prepare":
            return await self.prepare(context)
        if method == "drive":
            if not self.ready:
                raise ProtocolError("NOT_PREPARED", "Native world is unavailable", "execution")
            self.completion = await context.agent(
                self.spec["scope"],
                "run",
                {},
                request_id="appworld.agent",
                timeout=self.spec["limits"]["wall_time_seconds"],
            )
            return {"agent_outcome": self.completion["outcome"]}
        if method == "capture":
            if self.captured is not None:
                return deepcopy(self.captured)
            artifacts = []
            if self.world is not None:
                self.world.deadline = time.monotonic() + 180
                await self.world.call(
                    context,
                    "capture",
                    {
                        "agent_success": self.completion is not None
                        and self.completion["status"] == "success"
                    },
                )
                for name in ("state", "records"):
                    artifacts.append(
                        (
                            await self.broker(
                                context, "export", {"output_id": name}, resource="world"
                            )
                        )["artifact"]
                    )
            if "task" in self.handles:
                artifacts.append(
                    (
                        await self.broker(
                            context, "export", {"output_id": "working"}, resource="task"
                        )
                    )["artifact"]
                )
            self.captured = {
                "artifacts": artifacts,
                "regrade": "unavailable",
                "regrade_reason": "NATIVE_STATE_RESTORE_NOT_INSTALLED",
            }
            return deepcopy(self.captured)
        if method == "evaluate":
            if self.world is None or self.config["evaluation_mode"] == "deferred":
                return {
                    "status": "unavailable",
                    "success": None,
                    "metrics": [],
                    "artifacts": [],
                    "native": {"reason": "QUALITY_EVALUATION_DEFERRED"},
                    "completeness": {"native_verdict": False},
                }
            self.world.deadline = time.monotonic() + 180
            result = await self.world.call(context, "verify")
            artifact = (
                await self.broker(context, "export", {"output_id": "report"}, resource="world")
            )["artifact"]
            return {
                "status": "completed",
                "success": result["success"],
                "metrics": [],
                "artifacts": [artifact],
                "native": result,
                "completeness": {"native_verdict": result["native_verdict_complete"]},
            }
        if method == "cleanup":
            if self.world is not None:
                self.world.deadline = time.monotonic() + 180
                return await self.world.call(context, "cleanup")
            return {"cleaned": True}
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Native AppWorld operation is unsupported", "unsupported"
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

    await serve(AppWorldConnector(), lines(), write)


if __name__ == "__main__":
    asyncio.run(main())
