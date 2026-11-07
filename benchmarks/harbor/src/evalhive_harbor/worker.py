"""Native Harbor lifecycle over public Benchmark SDK ownership boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
import time
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, serve_stdio, validate, validate_spec
from evalhive_benchmark.protocol import MAX_LINE_BYTES
from harbor.environments.base import BaseEnvironment
from harbor.metrics.mean import Mean
from harbor.models.trial.paths import TrialPaths

from .archive import TransferError
from .artifacts import collect_artifacts, restore_artifacts, validate_artifacts
from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, InstalledHarbor
from .environment import BrokerEnvironment
from .native import HarborConfigurationError
from .policies import network_plan
from .resources import agent_resources
from .transfers import BrokerTransfers, absolute_path
from .verification import NativeVerificationError, run_verifier

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
PROVIDER = "evalhive.rootless"
STATE_SCHEMA = "harbor.artifacts.v1"


def metric(name, value):
    identity = (
        name
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", name)
        else "reward." + hashlib.sha256(name.encode()).hexdigest()
    )
    return {"name": identity, "value": value, "direction": "none", "aggregation": "native_mean"}


def unavailable(reason):
    return {
        "status": "unavailable",
        "success": None,
        "metrics": [],
        "native": {"reason": reason},
        "artifacts": [],
        "completeness": {"native_verdict": False},
    }


def verifier_resource(resource):
    identity = resource["id"]
    return identity in {"verifier", "verifier-network"} or identity.startswith("verifier.")


class HarborConnector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, dataset=None, bindings=None):
        self.root, self.dataset, self.bindings = Path(root), dataset, bindings
        self.spec, self.native, self.binding, self.config = None, None, None, None
        self.handles, self.environments = {}, {}
        self.ready, self.collected, self.sequence = False, None, 0
        self.temporary = tempfile.TemporaryDirectory(prefix="harbor-trial-")
        self.paths = TrialPaths(Path(self.temporary.name) / "trial")
        self.paths.mkdir()
        self.logger = logging.getLogger(__name__)

    def installed(self):
        if self.dataset is None:
            path = self.root / "installation.json"
            if path.is_symlink() or path.stat().st_size > 64 * 1024**2:
                raise HarborConfigurationError("Native installation metadata is unavailable")
            self.dataset = InstalledHarbor(self.root / "native", json.loads(path.read_text()))
        return self.dataset

    def _load(self, spec):
        validate_spec(self.manifest, spec)
        try:
            installed = self.installed()
            native = installed.resolve(spec["case"])
            selected_requirements = (
                native.evaluation_requirements()
                if spec.get("purpose") == "evaluation"
                else native.requirements()
            )
            if {"hardware.gpu", "hardware.tpu"} & set(
                (selected_requirements or {}).get("capabilities", [])
            ):
                raise ProtocolError(
                    "NATIVE_HARDWARE_UNAVAILABLE",
                    "Native hardware requires an installed provider adapter",
                    "unsupported",
                )
            if self.bindings is None:
                path = self.root / "runtime.json"
                if path.is_symlink() or path.stat().st_size > 64 * 1024**2:
                    raise HarborConfigurationError("Native runtime metadata is unavailable")
                self.bindings = json.loads(path.read_text())
            if (
                self.bindings.get("format_version") != "1.0"
                or self.bindings.get("source_digest") != installed.receipt["source_digest"]
            ):
                raise HarborConfigurationError("Native runtime belongs to another dataset")
            binding = self.bindings["cases"][spec["case"]["ref"]["case_id"]]
            if binding["source_digest"] != native.source_digest:
                raise HarborConfigurationError("Native runtime source differs from the case")
            if native.task.config.steps or native.task.config.environment.os.value != "linux":
                raise HarborConfigurationError(
                    "Native execution requires its installed step/OS adapter"
                )
            budget = native.task.config.agent.timeout_sec
            if budget is not None and (not float(budget).is_integer() or not 1 <= budget <= 86400):
                raise HarborConfigurationError(
                    "Native agent budget requires an installed deadline adapter"
                )
            if {"network.allowlist", "network.dynamic_policy"} & set(
                (selected_requirements or {}).get("capabilities", [])
            ):
                raise HarborConfigurationError(
                    "Native phase networking requires an installed policy adapter"
                )
            if "environment.host_bindings_v1" in (selected_requirements or {}).get(
                "capabilities", []
            ):
                raise HarborConfigurationError(
                    "Native environment templates require private bindings"
                )
            for name in ("task", "verifier") if native.verifier_mode == "separate" else ("task",):
                selected = binding[name]
                services = selected.get("services", {})
                order = selected.get("order", ["main"])
                if (
                    not isinstance(services, dict)
                    or not isinstance(order, list)
                    or len(order) != len(set(order))
                    or set(order) != {"main", *services}
                ):
                    raise HarborConfigurationError("Native service group ordering is invalid")
                seen = set()
                for member in order:
                    data = selected if member == "main" else services[member]
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", data["asset_id"]):
                        raise HarborConfigurationError("Native image binding is invalid")
                    absolute_path(data["cwd"])
                    if not set(data.get("depends_on", [])) <= seen:
                        raise HarborConfigurationError(
                            "Native service dependencies are not ordered"
                        )
                    seen.add(member)
            validate_artifacts(native, services=binding["task"].get("services", {}))
            if spec.get("purpose") != "evaluation":
                agent_resources(native, binding["task"]["cwd"])
            config = {"evaluation_mode": "native", "prepare_timeout_seconds": 120, **spec["config"]}
            effective = {**deepcopy(spec), "config": config}
            if self.spec is not None and self.spec != effective:
                raise HarborConfigurationError("Native run specification is frozen")
            if spec.get("purpose") == "evaluation":
                saved = spec["checkpoint"]
                if (
                    native.verifier_mode != "separate"
                    or not saved["complete"]
                    or saved["state_schema"] != STATE_SCHEMA
                    or len(saved["artifacts"]) != 1
                ):
                    raise HarborConfigurationError("Native verifier cannot restore this checkpoint")
                if saved["state"].get("source_digest") != native.source_digest:
                    raise HarborConfigurationError("Native checkpoint belongs to another source")
            self.spec, self.native, self.binding, self.config = effective, native, binding, config
        except (HarborConfigurationError, TransferError, OSError, ValueError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_CONFIGURATION_INVALID",
                "Native Harbor source or runtime binding is unavailable",
                "configuration",
            ) from None

    def _agent(self):
        result = {
            "mode": "in_environment",
            "interaction": "autonomous",
            "environment_handle": "task",
            "cwd": self.binding["task"]["cwd"],
            "prompt": self.native.task.instruction,
            "instructions": [],
            "resources": agent_resources(self.native, self.binding["task"]["cwd"]),
            "process_policy": "retain_until_cleanup",
        }
        if self.native.task.config.agent.timeout_sec is not None:
            result["wall_time_seconds"] = int(self.native.task.config.agent.timeout_sec)
        if self.native.task.config.agent.user is not None:
            result["user"] = str(self.native.task.config.agent.user)
        return result

    def _plan(self):
        evaluation = self.spec.get("purpose") == "evaluation"

        def resource(identity, asset=None, *, kind="container", dependencies=(), **config):
            return {
                "id": identity,
                "kind": kind,
                "provider": PROVIDER,
                "config": {"asset_id": asset or "harbor." + identity, **config},
                **({"depends_on": list(dependencies)} if dependencies else {}),
            }

        resources = [
            resource("stage", kind="volume"),
            resource("verifier-logs", kind="volume"),
            resource("agent-artifacts", kind="volume"),
            resource("agent-logs", kind="volume"),
        ]
        policies = network_plan(self.native.task)
        networks = {}
        for identity, policy in (
            ("task", policies.agent_phase),
            ("verifier", policies.verifier_phase),
        ):
            if (
                identity == "task"
                and evaluation
                or identity == "verifier"
                and self.native.verifier_mode != "separate"
            ):
                continue
            if policy.network_mode.value == "public":
                networks[identity] = identity + "-network"
                resources.append(
                    resource(networks[identity], "harbor.public-network", kind="network")
                )
        world_config = {
            "storage": {
                "stage": "stage",
                "verifier-logs": "verifier-logs",
                "agent-artifacts": "agent-artifacts",
                "agent-logs": "agent-logs",
            }
        }
        if evaluation:
            world_config["retained_inputs"] = [self.spec["checkpoint"]["artifacts"][0]["id"]]
        resources.append(
            resource(
                "world",
                "harbor.world." + self.native.verifier_mode,
                dependencies=("stage", "verifier-logs", "agent-artifacts", "agent-logs"),
                **world_config,
            )
        )
        for role in ("task", "verifier"):
            if (
                role == "task"
                and evaluation
                or role == "verifier"
                and self.native.verifier_mode != "separate"
            ):
                continue
            group = self.binding[role]
            for name, asset in group.get("volumes", {}).items():
                resources.append(resource(role + ".volume." + name, asset, kind="volume"))

            def logical(name, role=role):
                return role if name == "main" else role + ".service." + name

            for name in group.get("order", ["main"]):
                member = group if name == "main" else group["services"][name]
                storage = (
                    deepcopy(world_config["storage"]) if name == "main" else {"stage": "stage"}
                )
                storage.update(
                    {
                        slot: role + ".volume." + volume
                        for slot, volume in member.get("storage", {}).items()
                    }
                )
                dependencies = [
                    "world",
                    *storage.values(),
                    *(logical(target) for target in member.get("depends_on", [])),
                ]
                options = {}
                if role in networks:
                    dependencies.append(networks[role])
                    options["network"] = networks[role]
                    if "order" in group:
                        options["network_aliases"] = [name]
                resources.append(
                    resource(
                        logical(name),
                        member["asset_id"],
                        dependencies=tuple(dict.fromkeys(dependencies)),
                        storage=storage,
                        **options,
                    )
                )
        outputs = [
            {
                "id": "native-report",
                "resource_id": "world",
                "path": "/reports/native-verifier",
                "visibility": "evaluator",
                "required": False,
                "stage": "evaluation",
            },
            {
                "id": "native-artifacts",
                "resource_id": "world",
                "path": "/outputs/artifacts",
                "visibility": "user",
                "required": False,
                "stage": "capture" if self.native.verifier_mode == "separate" else "evaluation",
            },
            {
                "id": "checkpoint",
                "resource_id": "world",
                "path": "/checkpoint",
                "visibility": "private",
                "required": False,
            },
        ]
        if not evaluation:
            outputs.append(
                {
                    "id": "task-artifacts",
                    "resource_id": "task",
                    "path": "/logs/artifacts",
                    "visibility": "user",
                    "required": False,
                }
            )
        result = {
            "resources": resources,
            "outputs": outputs,
            "requirements": {
                "capabilities": ["host.ephemeral_exec_v1", "host.container_health_v1"]
                + (
                    ["host.volume_copy_up_v1"]
                    if any(
                        group.get("volumes")
                        for group in self.binding.values()
                        if isinstance(group, dict)
                    )
                    else []
                )
                + ([] if evaluation else ["host.retained_agent_processes_v1"])
            },
            "grading_mode": "container_native",
        }
        if evaluation:
            result["purpose"] = "evaluation"
        else:
            result["agent"] = self._agent()
        return result

    async def _broker(self, context, operation, payload=None, *, identity=None):
        self.sequence += 1
        return await context.broker(
            self.spec["scope"],
            PROVIDER,
            operation,
            payload or {},
            **({"handle": self.handles[identity]} if identity else {}),
            request_id="harbor." + str(self.sequence),
            timeout=(payload or {}).get("timeout_seconds", 30) + 5,
            durable_host=True,
        )

    async def _exec(self, context, identity, argv, *, user=None, environment=None):
        remaining = self.operation_deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError(
                "NATIVE_PHASE_TIMEOUT", "Native operation deadline expired", "execution"
            )
        return await self._broker(
            context,
            "exec",
            {
                "argv": argv,
                "cwd": "/tmp",
                "timeout_seconds": min(remaining, 86400),
                "deadline_policy": "inherit",
                "process_policy": "stop_after_command",
                **({"user": user} if user is not None else {}),
                **({"environment": environment} if environment is not None else {}),
            },
            identity=identity,
        )

    def _environment(self, context, identity):
        if identity not in self.environments:

            async def task_exec(argv, **options):
                return await self._exec(context, identity, argv, **options)

            async def stage_exec(argv):
                return await self._exec(context, "world", argv)

            role, separator, service = identity.partition(".service.")
            binding = (
                self.binding[role]["services"][service]
                if separator
                else self.binding.get(identity, self.binding["task"])
            )
            native_user = (
                self.native.task.config.agent.user
                if identity == "task"
                else self.native.task.config.verifier.user
                if identity == "verifier"
                else binding.get("user", "0")
                if separator
                else "0"
            )
            transfers = BrokerTransfers(
                task_exec,
                stage_exec,
                read_user=str(native_user) if native_user is not None else binding.get("user", "0"),
            )
            self.environments[identity] = BrokerEnvironment(
                context,
                self.spec["scope"],
                PROVIDER,
                self.handles[identity],
                cwd=binding["cwd"] if identity != "world" else "/tmp",
                deadline=self.operation_deadline,
                transfers=transfers,
            )
        result = self.environments[identity]
        result.deadline = self.operation_deadline
        if identity in {"task", "verifier"}:
            result.services = {
                name: self._environment(context, identity + ".service." + name)
                for name in self.binding[identity].get("services", {})
            }
        return result

    async def _stage(self, context, operation, payload=None):
        response = await self._exec(
            context,
            "world",
            [
                "/opt/venv/bin/python",
                "-m",
                "evalhive_harbor.staging",
                operation,
                json.dumps(payload or {}),
            ],
        )
        if response.get("exit_code") != 0 or response.get("stdout_truncated"):
            raise ProtocolError(
                "NATIVE_STAGING_FAILED", "Private native staging failed", "recording"
            )
        return json.loads(response["stdout"])

    async def _allocate(self, context, resources):
        for resource in resources:
            if resource["id"] in self.handles:
                continue
            allocated = await self._broker(context, "allocate", {"resource_id": resource["id"]})
            self.handles[resource["id"]] = allocated["handle"]
            if resource["id"] == "world":
                if (await self._stage(context, "initialize")).get("initialized") is not True:
                    raise ProtocolError(
                        "NATIVE_NOT_READY",
                        "Native logging storage was not initialized",
                        "preparation",
                    )

    async def _wait_ready(self, context, resources):
        for identity in (
            resource["id"] for resource in resources if resource["kind"] == "container"
        ):
            if identity not in self.handles:
                continue
            while True:
                inspected = await self._broker(context, "inspect", identity=identity)
                state = inspected.get("native") or {}
                if state.get("status") != "running" or state.get("health") in {
                    "unhealthy",
                    "unknown",
                }:
                    raise ProtocolError(
                        "NATIVE_NOT_READY", "Native environment is not ready", "preparation"
                    )
                if state.get("health") in {None, "healthy"}:
                    break
                if state.get("health") != "starting" or time.monotonic() >= self.operation_deadline:
                    raise ProtocolError(
                        "NATIVE_NOT_READY", "Native environment readiness timed out", "preparation"
                    )
                await asyncio.sleep(min(0.2, max(0, self.operation_deadline - time.monotonic())))

    async def _prepare(self, context):
        self.operation_deadline = time.monotonic() + self.config["prepare_timeout_seconds"]
        initial = [
            resource for resource in self._plan()["resources"] if not verifier_resource(resource)
        ]
        await self._allocate(context, initial)
        await self._wait_ready(context, initial)
        if self.spec.get("purpose") == "evaluation":
            if (
                await self._stage(
                    context,
                    "restore",
                    {"artifact_id": self.spec["checkpoint"]["artifacts"][0]["id"]},
                )
            ).get("restored") is not True:
                raise ProtocolError(
                    "NATIVE_RESTORE_FAILED", "Native checkpoint was not restored", "grading"
                )
            await self._environment(context, "world").transfers.download(
                source_dir="/restored/artifacts", target_dir=self.paths.artifacts_dir
            )
        else:
            environment = self._environment(context, "task")
            environment.task_env_config, environment.logger = (
                self.native.task.config.environment,
                self.logger,
            )
            await BaseEnvironment.run_healthcheck(environment)
        self.ready = True
        result = {
            "ready": True,
            "session_handle": self.handles["world"],
            "native_state": {
                "source_digest": self.native.source_digest,
                "verifier_mode": self.native.verifier_mode,
            },
            "artifacts": [],
        }
        if self.spec.get("purpose") == "evaluation":
            result["purpose"] = "evaluation"
        else:
            result["agent"] = {**self._agent(), "environment_handle": self.handles["task"]}
        return result

    async def _export(self, context, output):
        return (await self._broker(context, "export", {"output_id": output}, identity="world"))[
            "artifact"
        ]

    async def _collect(self, context):
        if self.collected is None:
            self.collected = await collect_artifacts(
                self.native, self._environment(context, "task"), self.paths
            )
        return self.collected

    async def _capture(self, context):
        self.operation_deadline = time.monotonic() + min(
            120, self.spec["limits"]["wall_time_seconds"]
        )
        result = {
            "artifacts": [],
            "regrade": "unavailable",
            "regrade_reason": "NATIVE_SHARED_VERIFIER_REQUIRES_LIVE_ENVIRONMENT",
        }
        if not self.ready:
            result["regrade_reason"] = "NATIVE_PREPARATION_INCOMPLETE"
            return result
        if self.spec.get("purpose") == "evaluation":
            return {"artifacts": [], "regrade": "unavailable"}
        preview = await self._broker(
            context, "export", {"output_id": "task-artifacts"}, identity="task"
        )
        result["artifacts"].append(preview["artifact"])
        if self.native.verifier_mode == "separate":
            collection = await self._collect(context)
            world = self._environment(context, "world")
            await world.transfers.publish(self.paths.artifacts_dir, "artifacts")
            result["artifacts"].append(await self._export(context, "native-artifacts"))
            if collection["complete"]:
                await world.transfers.publish(self.paths.artifacts_dir, "checkpoint")
                checkpoint = await self._export(context, "checkpoint")
                result["artifacts"].append(checkpoint)
                result.update(
                    regrade="available",
                    checkpoint={
                        "format_version": "1.0",
                        "connector": {"id": CONNECTOR_ID, "version": CONNECTOR_VERSION},
                        "case": self.spec["case"]["ref"],
                        "source_digest": self.native.source_digest,
                        "state_schema": STATE_SCHEMA,
                        "complete": True,
                        "artifacts": [checkpoint],
                        "evaluator_roles": [],
                        "state": {"source_digest": self.native.source_digest},
                    },
                )
                result.pop("regrade_reason", None)
            else:
                result["regrade_reason"] = "NATIVE_ARTIFACT_RECORDING_INCOMPLETE"
            for name in reversed(self.binding["task"].get("order", ["main"])):
                identity = "task" if name == "main" else "task.service." + name
                await self._broker(context, "stop", identity=identity)
        return result

    async def _evaluate(self, context):
        if not self.ready:
            return unavailable("NATIVE_ENVIRONMENT_NOT_READY")
        self.operation_deadline = time.monotonic() + self.config["prepare_timeout_seconds"]
        identity = "verifier" if self.native.verifier_mode == "separate" else "task"
        paths = self.paths
        result = unavailable("QUALITY_EVALUATION_DEFERRED")
        try:
            if self.native.verifier_mode == "separate":
                resources = [
                    resource
                    for resource in self._plan()["resources"]
                    if verifier_resource(resource)
                ]
                await self._allocate(context, resources)
                await self._wait_ready(context, resources)
            environment = self._environment(context, identity)
            if self.native.verifier_mode == "separate":
                await environment.empty_dirs(["/logs/verifier"], chmod=True)
                await restore_artifacts(
                    self.native,
                    environment,
                    paths,
                    source_services=self.binding["task"].get("services", {}),
                )
            if self.config["evaluation_mode"] != "deferred":
                self.operation_deadline = (
                    time.monotonic() + self.native.task.config.verifier.timeout_sec
                )
                environment.deadline = self.operation_deadline
                verdict = await run_verifier(self.native, environment, paths)
                result = {
                    "status": "completed",
                    "success": None,
                    "metrics": [metric(name, value) for name, value in verdict.rewards.items()],
                    "native": {"rewards": verdict.rewards},
                    "artifacts": [],
                    "completeness": {
                        "native_verdict": True,
                        "verifier_logs": environment.recording_complete,
                    },
                }
        except (
            NativeVerificationError,
            TransferError,
            HarborConfigurationError,
            ProtocolError,
        ) as error:
            result = {
                "status": "error",
                "success": None,
                "metrics": [],
                "native": {},
                "artifacts": [],
                "completeness": {"native_verdict": False},
                "error": {
                    "code": getattr(error, "code", "NATIVE_VERIFIER_FAILED"),
                    "message": "Native Harbor verification failed",
                    "kind": "grading",
                },
            }
        self.operation_deadline = time.monotonic() + min(
            120, self.spec["limits"]["wall_time_seconds"]
        )
        if self.native.verifier_mode == "shared":
            result["native"]["artifact_collection"] = await self._collect(context)
            await self._environment(context, "world").transfers.publish(
                paths.artifacts_dir, "artifacts"
            )
            result["artifacts"].append(await self._export(context, "native-artifacts"))
        if paths.verifier_dir.exists():
            await self._environment(context, "world").transfers.publish(
                paths.verifier_dir, "report"
            )
            result["artifacts"].append(await self._export(context, "native-report"))
        return result

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
            if (
                params["dataset_id"] != installed.receipt["dataset_id"]
                or params["revision"] != installed.receipt["revision"]
            ):
                raise ProtocolError(
                    "DATASET_UNAVAILABLE", "Native dataset is not installed", "configuration"
                )
            cursor = params.get("cursor") or "0"
            if not cursor.isdecimal() or str(int(cursor)) != cursor or int(cursor) > len(cases):
                raise ProtocolError(
                    "INVALID_CURSOR", "Native case cursor is invalid", "configuration"
                )
            start, end, size = int(cursor), int(cursor), 4096
            for case in cases[start : start + params.get("limit", 100)]:
                size += len(json.dumps(case, ensure_ascii=False).encode())
                if size > MAX_LINE_BYTES:
                    break
                end += 1
            if end == start and start < len(cases):
                raise ProtocolError(
                    "CASE_TOO_LARGE", "Native case does not fit a reply", "configuration"
                )
            return {
                "cases": deepcopy(cases[start:end]),
                "next_cursor": str(end) if end < len(cases) else None,
            }
        if method == "aggregate":
            payload = params["payload"]
            results = payload.get("results", [])
            expected = payload.get("expected", len(results))
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
            rewards = [
                result["native"].get("rewards") if result["status"] == "completed" else None
                for result in results
            ]
            means = Mean().compute(rewards) if rewards else {}
            return {
                "metrics": [metric(name, value) for name, value in means.items()],
                "expected": expected,
                "observed": len(results),
                "native": {
                    "mean": means,
                    "unscored": sum(reward is None for reward in rewards),
                    "error_policy": "native_zero_in_mean",
                },
            }
        if "spec" in params:
            self._load(params["spec"])
        if method == "validate":
            return {"accepted": True, "effective_config": self.config}
        if method == "plan":
            return self._plan()
        if method == "prepare":
            return await self._prepare(context)
        if method == "drive":
            if not self.ready or self.spec.get("purpose") == "evaluation":
                raise ProtocolError(
                    "NOT_PREPARED", "Native agent environment is unavailable", "execution"
                )
            result = await context.agent(
                self.spec["scope"],
                "run",
                {},
                request_id="harbor.agent",
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
            "UNSUPPORTED_CAPABILITY", "Native Harbor method is unsupported", "unsupported"
        )

    async def close(self, context):
        self.temporary.cleanup()


if __name__ == "__main__":
    asyncio.run(serve_stdio(HarborConnector()))
