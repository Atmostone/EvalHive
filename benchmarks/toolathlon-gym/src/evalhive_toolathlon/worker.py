"""Native Toolathlon lifecycle over public resource and Harness SDK boundaries."""

from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, serve_stdio, validate_spec
from evalhive_cases.worker import CasesConnector, unavailable

from .commands import LaunchTime, NativeConfigurationError, native_command, portal_urls, task_path
from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, InstalledToolathlon

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
PROVIDER = "evalhive.rootless"
HELPER = "/opt/evalhive-toolathlon/environment.py"
PYTHON = "/opt/venv/bin/python"
STATE_SCHEMA = "toolathlon-gym.world.v2"


class ToolathlonConnector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, dataset=None):
        self.root, self.dataset = root, dataset
        self.spec, self.case, self.config = None, None, None
        self.handles, self.artifacts = {}, []
        self.ready, self.snapshot_available = False, False
        self.preparation_deadline = None

    def installed(self):
        if self.dataset is None:
            self.dataset = InstalledToolathlon(self.root)
        return self.dataset

    def _load(self, spec):
        validate_spec(self.manifest, spec)
        try:
            case = self.installed().resolve(spec["case"])
            launch = (
                spec["config"].get("launch_time")
                or (self.config or {}).get("launch_time")
                or LaunchTime.now().full
            )
            config = {
                "launch_time": launch,
                "prepare_timeout_seconds": 180,
                "command_timeout_seconds": 90,
                "evaluation_timeout_seconds": 120,
                "evaluation_mode": "native",
                "capture_state": True,
                "portal_checks": [],
                "mcp_overrides": {},
                **spec["config"],
            }
            launch = LaunchTime(config["launch_time"])
            effective = {**deepcopy(spec), "config": deepcopy(config)}
            if self.spec is not None and effective != self.spec:
                raise NativeConfigurationError("Native run specification is frozen")
            raw = case.native_data()
            if case.external_eval is None:
                raise NativeConfigurationError(
                    "Native Toolathlon evaluation configuration is missing"
                )
            if raw["input"].get("context"):
                raise NativeConfigurationError(
                    "Native document context requires an explicit adapter"
                )
            source_config = raw["meta"].get("native_config", {})
            if (
                source_config.get("max_steps") is not None
                or source_config.get("max_turns") is not None
                or source_config.get("max_steps_under_single_turn_mode") is not None
                or source_config.get("stop") is not None
                or (source_config.get("meta") or {}).get("max_steps") is not None
            ):
                raise NativeConfigurationError(
                    "Native step budget requires an explicit supported Harness binding"
                )
            self.spec, self.case, self.config, self.launch = effective, case, config, launch
            self.active_time = launch.full
            self.instructions = self.installed().archive.agent_instructions(case.id, launch.full)
            self.probes = list(
                dict.fromkeys(portal_urls(case.agent_input()["prompt"]) + config["portal_checks"])
            )
            # Validate extra probe origins without contacting a host endpoint.
            from urllib.parse import urlsplit

            for url in self.probes:
                parsed = urlsplit(url)
                if (
                    parsed.scheme != "http"
                    or parsed.hostname not in {"localhost", "127.0.0.1"}
                    or not parsed.port
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                ):
                    raise NativeConfigurationError("Native readiness requires local HTTP resources")
            self.probes = [
                url.replace("http://localhost:", "http://127.0.0.1:", 1) for url in self.probes
            ]
            needed = (raw.get("environment") or {}).get("mcp_servers", [])
            self.resources = self.installed().archive.mcp_resources(needed)
            for name, override in config["mcp_overrides"].items():
                selected = next(
                    (
                        resource
                        for resource in self.resources
                        if resource["id"] == "toolathlon-" + name
                    ),
                    None,
                )
                if selected is None:
                    raise NativeConfigurationError("MCP override targets an unused native server")
                for key, value in override.items():
                    selected["config"][key] = (
                        {**selected["config"].get(key, {}), **deepcopy(value)}
                        if key in {"env", "aliases"}
                        else deepcopy(value)
                    )
            if spec.get("purpose") == "evaluation":
                checkpoint = spec["checkpoint"]
                if (
                    checkpoint["state_schema"] != STATE_SCHEMA
                    or not checkpoint["complete"]
                    or len(checkpoint["artifacts"]) != 1
                    or checkpoint["state"].get("native_archive_digest")
                    != self.installed().archive.digest
                ):
                    raise NativeConfigurationError("Native world checkpoint is not restorable")
                self.active_time = launch.verify_active(
                    checkpoint["state"].get("active_launch_time")
                )
        except (NativeConfigurationError, ValueError, OSError):
            raise ProtocolError(
                "NATIVE_CONFIGURATION_INVALID",
                "Native Toolathlon installation or configuration is unavailable",
                "configuration",
            ) from None

    def _agent(self):
        return {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": "task",
            "cwd": "/workspace",
            "prompt": self.case.agent_input()["prompt"],
            "instructions": list(self.instructions),
            "resources": [
                {"id": "tools", "kind": "tools", "config": {"names": []}},
                *deepcopy(self.resources),
            ],
        }

    def _plan(self):
        def resource(identity, kind, asset, *, dependencies=(), **config):
            return {
                "id": identity,
                "kind": kind,
                "provider": PROVIDER,
                "config": {"asset_id": "toolathlon-gym." + asset, **config},
                **({"depends_on": list(dependencies)} if dependencies else {}),
            }

        evaluation = self.spec.get("purpose") == "evaluation"
        world_config = {"network": "network", "storage": {"workspace": "workspace"}}
        if evaluation:
            world_config["retained_inputs"] = [self.spec["checkpoint"]["artifacts"][0]["id"]]
        resources = [
            resource("network", "network", "network"),
            resource("workspace", "volume", "workspace"),
            resource("database-files", "volume", "database-files"),
            resource(
                "initialize-files",
                "container",
                "initialize-files",
                dependencies=("database-files",),
                storage={"database": "database-files"},
            ),
            resource(
                "database",
                "container",
                "database",
                dependencies=("network", "database-files", "initialize-files"),
                network="network",
                network_aliases=["toolathlon_pg"],
                storage={"database": "database-files"},
            ),
            resource(
                "world",
                "container",
                "world",
                dependencies=("network", "workspace", "database"),
                **world_config,
            ),
        ]
        if not evaluation:
            resources.append(
                resource(
                    "task",
                    "container",
                    "task",
                    dependencies=("workspace", "world"),
                    namespace="world",
                    storage={"workspace": "workspace"},
                )
            )
        result = {
            "resources": resources,
            "outputs": [
                {
                    "id": "workspace",
                    "resource_id": "world",
                    "path": "/workspace",
                    "visibility": "user",
                    "required": True,
                },
                {
                    "id": "checkpoint",
                    "resource_id": "world",
                    "path": "/checkpoint",
                    "visibility": "private",
                    "required": False,
                },
                {
                    "id": "native-report",
                    "resource_id": "world",
                    "path": "/reports",
                    "visibility": "evaluator",
                    "required": False,
                    "stage": "evaluation",
                },
            ],
            "requirements": {},
            "grading_mode": "environment",
        }
        if evaluation:
            result["purpose"] = "evaluation"
        else:
            result["agent"] = self._agent()
        return result

    async def _broker(self, context, operation, payload=None, *, handle=None, request_id):
        return await context.broker(
            self.spec["scope"],
            PROVIDER,
            operation,
            payload or {},
            handle=handle,
            request_id=request_id,
            timeout=(payload or {}).get("timeout_seconds", self.config["command_timeout_seconds"])
            + 5,
            durable_host=True,
        )

    async def _allocate(self, context, identity):
        response = await self._broker(
            context, "allocate", {"resource_id": identity}, request_id="prepare." + identity
        )
        self.handles[identity] = response["handle"]

    async def _exec(self, context, argv, *, request_id, timeout=None, cwd="/tmp"):
        timeout = timeout or self.config["command_timeout_seconds"]
        if self.preparation_deadline is not None:
            remaining = self.preparation_deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(
                    "NATIVE_PREPARATION_TIMEOUT",
                    "Native preparation deadline expired",
                    "preparation",
                )
            timeout = min(timeout, remaining)
        return await self._broker(
            context,
            "exec",
            {
                "argv": argv,
                "cwd": cwd,
                "timeout_seconds": timeout,
            },
            handle=self.handles["world"],
            request_id=request_id,
        )

    async def _helper(self, context, operation, *, request_id, **options):
        argv = [PYTHON, HELPER, operation]
        for name, value in options.items():
            argv.extend(["--" + name.replace("_", "-"), value])
        result = await self._exec(context, argv, request_id=request_id)
        if result["exit_code"] != 0 or result.get("stdout_truncated"):
            raise ProtocolError(
                "NATIVE_ENVIRONMENT_FAILED", "Native environment operation failed", "preparation"
            )
        try:
            response = json.loads(result["stdout"])
            if not isinstance(response, dict):
                raise ValueError("Native helper response must be an object")
            flag = {
                "initialize": "initialized",
                "reset": "reset",
                "restore": "restored",
                "snapshot": "checkpoint_complete",
                "database_ready": "ready",
                "portals": "ready",
            }[operation]
            if type(response.get(flag)) is not bool:
                raise ValueError("Native helper response has no explicit state")
            if operation in {"initialize", "reset", "restore"} and not response[flag]:
                raise ValueError("Native helper did not complete its operation")
            return response
        except ValueError:
            raise ProtocolError(
                "NATIVE_ENVIRONMENT_FAILED",
                "Native environment response is incomplete",
                "recording",
            ) from None

    async def _until(self, context, check, *, deadline):
        index = 0
        while time.monotonic() < deadline:
            if context.cancelled.is_set():
                raise ProtocolError("CANCELLED", "Native preparation was cancelled", "preparation")
            if await check(index):
                return
            index += 1
            await asyncio.sleep(0.1)
        raise ProtocolError(
            "NATIVE_NOT_READY", "Native environment readiness deadline expired", "preparation"
        )

    async def _prepare(self, context):
        deadline = time.monotonic() + self.config["prepare_timeout_seconds"]
        self.preparation_deadline = deadline
        for identity in ("network", "workspace", "database-files", "initialize-files"):
            await self._allocate(context, identity)

        async def initialized(index):
            state = (
                await self._broker(
                    context,
                    "inspect",
                    handle=self.handles["initialize-files"],
                    request_id=f"prepare.files.{index}",
                )
            )["native"]
            if state and state["status"] in {"exited", "dead"}:
                if state["exit_code"] != 0:
                    raise ProtocolError(
                        "NATIVE_FILES_FAILED",
                        "Native database storage initialization failed",
                        "preparation",
                    )
                return True
            return False

        await self._until(context, initialized, deadline=deadline)
        await self._allocate(context, "database")
        await self._allocate(context, "world")
        await self._helper(
            context,
            "initialize",
            request_id="prepare.source",
            source_digest=self.installed().archive.digest,
        )

        async def database(index):
            return (
                await self._helper(
                    context, "database_ready", request_id=f"prepare.database.{index}"
                )
            ).get("ready") is True

        await self._until(context, database, deadline=deadline)
        if self.spec.get("purpose") == "evaluation":
            artifact = self.spec["checkpoint"]["artifacts"][0]
            await self._helper(
                context,
                "restore",
                request_id="prepare.restore",
                task=self.case.id,
                checkpoint="/evalhive/retained/" + artifact["id"] + "/payload",
            )
        else:
            await self._helper(context, "reset", request_id="prepare.reset", task=self.case.id)
            for short in (False, True):
                argv = native_command(
                    self.case.external_eval["preprocess_command"],
                    case_id=self.case.id,
                    phase="preprocess",
                    launch_time=self.active_time,
                )
                response = await self._exec(
                    context,
                    argv,
                    request_id="prepare.native.short" if short else "prepare.native",
                    cwd=str(task_path(self.case.id)),
                )
                if response["exit_code"] == 0:
                    break
                fallback = self.launch.fallback(
                    response.get("stdout", "") + response.get("stderr", "")
                )
                if short or fallback is None:
                    raise ProtocolError(
                        "NATIVE_PREPROCESS_FAILED",
                        "Native task preprocessing failed",
                        "preparation",
                    )
                self.active_time = fallback

        async def portals(index):
            return (
                await self._helper(
                    context,
                    "portals",
                    request_id=f"prepare.portals.{index}",
                    urls=json.dumps(self.probes),
                )
            ).get("ready") is True

        await self._until(context, portals, deadline=deadline)
        if self.spec.get("purpose") != "evaluation":
            await self._allocate(context, "task")
            state = await self._broker(
                context, "inspect", handle=self.handles["task"], request_id="prepare.task-ready"
            )
            if (state.get("native") or {}).get("status") != "running":
                raise ProtocolError(
                    "NATIVE_TASK_NOT_READY", "Native Task container is not running", "preparation"
                )
        self.ready = True
        self.preparation_deadline = None
        result = {
            "ready": True,
            "session_handle": self.handles["world"],
            "native_state": {
                "active_launch_time": self.active_time,
                "native_archive_digest": self.installed().archive.digest,
            },
            "artifacts": [],
        }
        if self.spec.get("purpose") == "evaluation":
            result["purpose"] = "evaluation"
        else:
            result["agent"] = {**self._agent(), "environment_handle": self.handles["task"]}
        return result

    async def _capture(self, context):
        if "world" not in self.handles:
            return {"artifacts": [], "regrade": "unavailable"}
        saved = await self._broker(
            context,
            "export",
            {"output_id": "workspace"},
            handle=self.handles["world"],
            request_id="capture.workspace",
        )
        artifacts = [saved["artifact"]]
        if self.ready and self.config["capture_state"]:
            try:
                state = await self._helper(
                    context, "snapshot", request_id="capture.state", task=self.case.id
                )
                if state.get("checkpoint_complete") is True:
                    saved = await self._broker(
                        context,
                        "export",
                        {"output_id": "checkpoint"},
                        handle=self.handles["world"],
                        request_id="capture.checkpoint",
                    )
                    artifacts.append(saved["artifact"])
                    self.snapshot_available = True
            except ProtocolError as error:
                self.snapshot_available = False
                checkpoint_failure = error.code
            else:
                checkpoint_failure = (
                    "NATIVE_STATE_INCOMPLETE" if not self.snapshot_available else None
                )
        else:
            checkpoint_failure = (
                "NATIVE_STATE_CAPTURE_DISABLED" if self.ready else "NATIVE_PREPARATION_INCOMPLETE"
            )
        result = {
            "artifacts": artifacts,
            "regrade": "available" if self.snapshot_available else "unavailable",
            "completeness": {"workspace": True, "native_checkpoint": self.snapshot_available},
        }
        if checkpoint_failure is not None:
            result["regrade_reason"] = checkpoint_failure
        if self.snapshot_available:
            result["checkpoint"] = {
                "format_version": "1.0",
                "connector": {"id": CONNECTOR_ID, "version": CONNECTOR_VERSION},
                "case": self.spec["case"]["ref"],
                "source_digest": self.case.source_digest,
                "state_schema": STATE_SCHEMA,
                "complete": True,
                "artifacts": [artifacts[-1]],
                "evaluator_roles": [],
                "state": {
                    "active_launch_time": self.active_time,
                    "native_archive_digest": self.installed().archive.digest,
                },
            }
        return result

    async def _evaluate(self, context):
        if self.config["evaluation_mode"] == "deferred":
            return unavailable("QUALITY_EVALUATION_DEFERRED")
        if not self.ready:
            return unavailable("NATIVE_ENVIRONMENT_NOT_READY")
        argv = native_command(
            self.case.external_eval["eval_command"],
            case_id=self.case.id,
            phase="evaluation",
            launch_time=self.active_time,
            groundtruth_path=self.case.external_eval.get("groundtruth_path"),
        )
        response = await self._exec(
            context,
            argv,
            request_id="evaluate.native",
            timeout=self.config["evaluation_timeout_seconds"],
            cwd=str(task_path(self.case.id)),
        )
        if response["exit_code"] not in (0, 1):
            raise ProtocolError(
                "NATIVE_CHECKER_FAILED",
                "Native evaluator did not return a supported verdict",
                "grading",
            )
        report = await self._broker(
            context,
            "export",
            {"output_id": "native-report"},
            handle=self.handles["world"],
            request_id="evaluate.report",
        )
        success = response["exit_code"] == 0
        return {
            "status": "completed",
            "success": success,
            "primary_metric": "native_success",
            "metrics": [
                {
                    "name": "native_success",
                    "value": float(success),
                    "direction": "higher",
                    "aggregation": "mean",
                }
            ],
            "native": {
                "exit_code": response["exit_code"],
                "active_launch_time": self.active_time,
                "stdout": response.get("stdout", ""),
                "stderr": response.get("stderr", ""),
                "stdout_truncated": response.get("stdout_truncated", False),
                "stderr_truncated": response.get("stderr_truncated", False),
            },
            "artifacts": [report["artifact"]],
            "completeness": {"native_verdict": True},
        }

    async def handle(self, method, params, context):
        if method in {"list_cases", "resolve_case", "aggregate"}:
            catalog = CasesConnector(dataset=self.installed())
            return await catalog.handle(method, params, context)
        if "spec" in params:
            self._load(params["spec"])
        if method == "validate":
            return {"accepted": True, "effective_config": self.config}
        if method == "plan":
            return self._plan()
        if method == "prepare":
            return await self._prepare(context)
        if method == "drive":
            if self.spec.get("purpose") == "evaluation":
                raise ProtocolError(
                    "EVALUATION_ONLY", "Native evaluation cannot run an agent", "unsupported"
                )
            if not self.ready:
                raise ProtocolError("NOT_PREPARED", "Native task is not prepared", "execution")
            result = await context.agent(
                self.spec["scope"],
                "run",
                {},
                request_id="native.agent",
                timeout=self.spec["limits"]["wall_time_seconds"],
            )
            return {"agent_outcome": result["outcome"]}
        if method == "capture":
            return await self._capture(context)
        if method == "evaluate":
            return await self._evaluate(context)
        if method == "cleanup":
            return {"cleaned": True}
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Native Toolathlon method is unsupported", "unsupported"
        )


if __name__ == "__main__":
    asyncio.run(serve_stdio(ToolathlonConnector()))
