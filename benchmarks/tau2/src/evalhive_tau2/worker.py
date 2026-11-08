"""Run the native half-duplex benchmark with a public Harness participant."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import Connector, ProtocolError, validate_spec
from evalhive_benchmark.connector import serve
from evalhive_benchmark.protocol import MAX_LINE_BYTES

from .agent import ExternalSession, create_agent, thread_exchange
from .dataset import CONNECTOR_ID, CONNECTOR_VERSION, InstalledTau2
from .models import PARAMETERS, BoundModel
from .native import REVISION, VERSION, NativeConfigurationError, canonical, load_domain
from .resources import agent_resources, public_surface
from .stage import CHUNK, MAX_BYTES

MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text())
PROVIDER = "evalhive.rootless"
STATE_SCHEMA = "tau2.simulation.v1"
DEFAULTS = {key: value["default"] for key, value in MANIFEST["config_schema"]["properties"].items()}


def needs_judge(task):
    return bool(
        task.evaluation_criteria and "NL_ASSERTION" in task.evaluation_criteria.reward_basis
    )


def unavailable(reason):
    return {
        "status": "unavailable",
        "success": None,
        "metrics": [],
        "artifacts": [],
        "native": {"reason": reason},
        "completeness": {"native_verdict": False},
    }


class Tau2Connector(Connector):
    manifest = MANIFEST

    def __init__(self, root="/dataset", *, dataset=None, bindings=None):
        self.root, self.dataset, self.bindings = Path(root), dataset, bindings
        self.spec, self.task, self.surface, self.config = None, None, None, None
        self.handles, self.sequence, self.record = {}, 0, None
        self.ready, self.captured = False, None

    def installed(self):
        if self.dataset is None:
            path = self.root / "installation.json"
            if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
                raise NativeConfigurationError("Native installation metadata is unavailable")
            self.dataset = InstalledTau2(self.root / "native", json.loads(path.read_text()))
        return self.dataset

    def _load(self, spec):
        validate_spec(self.manifest, spec)
        try:
            installed = self.installed()
            task = installed.resolve(spec["case"])
            domain = spec["case"]["native_ref"]["domain"]
            surface = public_surface(installed.root, domain)
            if self.bindings is None:
                path = self.root / "runtime.json"
                if path.is_symlink() or path.stat().st_size > 1024**2:
                    raise NativeConfigurationError("Native runtime metadata is unavailable")
                self.bindings = json.loads(path.read_text())
            if (
                self.bindings.get("format_version") != "1.0"
                or self.bindings.get("source_digest") != installed.receipt["source_digest"]
                or self.bindings.get("surface_fingerprint") != surface["fingerprint"]
                or set(self.bindings.get("assets", {})) != {"task", "recorder"}
            ):
                raise NativeConfigurationError("Native runtime belongs to another installation")
            config = {**DEFAULTS, **spec["config"]}
            effective = {**deepcopy(spec), "config": config}
            if self.spec is not None and self.spec != effective:
                raise NativeConfigurationError("Native run specification is frozen")
            evaluation = spec.get("purpose") == "evaluation"
            roles = {"judge"} if needs_judge(task) else set()
            if not evaluation:
                roles.add("user")
            if set(spec["models"]) != roles:
                raise NativeConfigurationError("Native auxiliary model roles must be explicit")
            for model in spec["models"].values():
                if set(model.get("config", {})) - PARAMETERS:
                    raise NativeConfigurationError("Native model parameters are unsupported")
            if evaluation:
                checkpoint = spec["checkpoint"]
                if (
                    not checkpoint["complete"]
                    or checkpoint["state_schema"] != STATE_SCHEMA
                    or len(checkpoint["artifacts"]) != 1
                    or set(checkpoint["evaluator_roles"]) != roles
                    or checkpoint["state"] != {"surface_fingerprint": surface["fingerprint"]}
                ):
                    raise NativeConfigurationError("Native checkpoint cannot be restored")
            self.spec, self.task, self.surface, self.config = effective, task, surface, config
        except (NativeConfigurationError, OSError, ValueError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_CONFIGURATION_INVALID",
                "Native tau2 source, public surface or model binding is unavailable",
                "configuration",
            ) from None

    def _deadline(self, seconds):
        return time.monotonic() + min(seconds, self.spec["limits"]["wall_time_seconds"])

    def _agent(self):
        return {
            "mode": "in_environment",
            "interaction": "external_steps",
            "environment_handle": self.handles.get("task", "task"),
            "cwd": "/workspace",
            "prompt": self.surface["instructions"],
            "instructions": [self.surface["instructions"]],
            "resources": agent_resources(),
        }

    def _plan(self):
        evaluation = self.spec.get("purpose") == "evaluation"
        resources = []
        for name in ["recorder"] if evaluation else ["recorder", "task"]:
            config = {"asset_id": self.bindings["assets"][name]}
            if evaluation:
                config["retained_inputs"] = [self.spec["checkpoint"]["artifacts"][0]["id"]]
            resources.append(
                {"id": name, "kind": "container", "provider": PROVIDER, "config": config}
            )
        result = {
            "resources": resources,
            "outputs": [
                {
                    "id": name,
                    "resource_id": "recorder",
                    "path": "/records/" + name,
                    "visibility": "private" if name == "checkpoint" else "evaluator",
                    "required": False,
                    "stage": "evaluation" if name == "report" else "capture",
                }
                for name in (["report"] if evaluation else ["trace", "checkpoint", "report"])
            ],
            "requirements": {},
            "grading_mode": "snapshot",
        }
        if evaluation:
            result["purpose"] = "evaluation"
        else:
            result["agent"] = self._agent()
        return result

    async def _broker(self, context, operation, payload=None, *, identity=None):
        remaining = self.operation_deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("NATIVE_PHASE_TIMEOUT", "Native operation expired", "execution")
        self.sequence += 1
        return await context.broker(
            self.spec["scope"],
            PROVIDER,
            operation,
            payload or {},
            **({"handle": self.handles[identity]} if identity else {}),
            request_id="tau2.broker." + str(self.sequence),
            timeout=min(remaining, 86400) + 5,
            durable_host=True,
        )

    async def _stage(self, context, operation, payload=None):
        encoded = base64.b64encode(canonical(payload or {})).decode()
        result = await self._broker(
            context,
            "exec",
            {
                "argv": ["/opt/venv/bin/python", "-m", "evalhive_tau2.stage", operation, encoded],
                "cwd": "/tmp",
                "timeout_seconds": min(self.operation_deadline - time.monotonic(), 86400),
                "deadline_policy": "inherit",
                "process_policy": "stop_after_command",
            },
            identity="recorder",
        )
        if result["exit_code"] != 0:
            raise ProtocolError(
                "NATIVE_EVIDENCE_FAILED", "Native private evidence transfer failed", "recording"
            )
        try:
            return json.loads(result["stdout"])
        except (ValueError, KeyError, TypeError):
            raise ProtocolError(
                "NATIVE_EVIDENCE_FAILED", "Native evidence receipt is invalid", "recording"
            ) from None

    async def _write(self, context, name, value):
        data = canonical(value)
        if len(data) > MAX_BYTES:
            raise ProtocolError(
                "NATIVE_EVIDENCE_LIMIT", "Native evidence exceeds its byte budget", "recording"
            )
        for offset in range(0, len(data), CHUNK):
            await self._stage(
                context,
                "write",
                {
                    "name": name,
                    "offset": offset,
                    "data": base64.b64encode(data[offset : offset + CHUNK]).decode(),
                },
            )
        await self._stage(
            context,
            "seal",
            {"name": name, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        )

    async def _read_checkpoint(self, context):
        proof = await self._stage(
            context, "restore", {"artifact_id": self.spec["checkpoint"]["artifacts"][0]["id"]}
        )
        data = bytearray()
        while len(data) < proof["size_bytes"]:
            result = await self._stage(context, "read", {"name": "checkpoint", "offset": len(data)})
            chunk = base64.b64decode(result["data"], validate=True)
            if (
                not chunk
                or len(chunk) > CHUNK
                or len(data) + len(chunk) > MAX_BYTES
                or result["size_bytes"] != proof["size_bytes"]
                or result["sha256"] != proof["sha256"]
            ):
                raise ProtocolError(
                    "NATIVE_RESTORE_FAILED", "Native checkpoint transfer changed", "grading"
                )
            data.extend(chunk)
        if len(data) != proof["size_bytes"] or hashlib.sha256(data).hexdigest() != proof["sha256"]:
            raise ProtocolError(
                "NATIVE_RESTORE_FAILED", "Native checkpoint transfer is incomplete", "grading"
            )
        record = json.loads(data)
        if (
            record.get("format_version") != STATE_SCHEMA
            or record.get("native_revision") != REVISION
            or record.get("source_digest") != self.spec["case"]["source_digest"]
            or record.get("case") != self.spec["case"]["ref"]
            or record.get("surface_fingerprint") != self.surface["fingerprint"]
        ):
            raise ProtocolError(
                "NATIVE_RESTORE_FAILED", "Native checkpoint provenance differs", "grading"
            )
        from tau2.data_model.simulation import SimulationRun

        simulation = SimulationRun.model_validate(record["simulation"])
        if simulation.task_id != self.task.id or simulation.mode != "half_duplex":
            raise ProtocolError(
                "NATIVE_RESTORE_FAILED", "Native checkpoint task or mode differs", "grading"
            )
        self.record = record

    async def _prepare(self, context):
        self.operation_deadline = self._deadline(self.config["prepare_timeout_seconds"])
        for resource in self._plan()["resources"]:
            result = await self._broker(context, "allocate", {"resource_id": resource["id"]})
            self.handles[resource["id"]] = result["handle"]
            state = await self._broker(context, "inspect", identity=resource["id"])
            if state.get("native", {}).get("status") != "running":
                raise ProtocolError(
                    "NATIVE_NOT_READY", "Native environment did not start", "execution"
                )
        await self._stage(context, "init")
        if self.spec.get("purpose") == "evaluation":
            await self._read_checkpoint(context)
        self.ready = True
        result = {
            "ready": True,
            "session_handle": self.handles["recorder"],
            "native_state": {
                "source_digest": self.spec["case"]["source_digest"],
                "interface": self.surface["interface"],
            },
            "artifacts": [],
        }
        if self.spec.get("purpose") == "evaluation":
            result["purpose"] = "evaluation"
        else:
            result["agent"] = self._agent()
        return result

    async def _drive(self, context):
        if not self.ready or self.spec.get("purpose") == "evaluation":
            raise ProtocolError(
                "NOT_PREPARED", "Native agent environment is unavailable", "execution"
            )
        self.operation_deadline = self._deadline(self.spec["limits"]["wall_time_seconds"])
        domain = self.spec["case"]["native_ref"]["domain"]
        module = load_domain(self.installed().root, domain)
        from tau2.orchestrator.orchestrator import Orchestrator
        from tau2.runner.build import build_user

        loop = asyncio.get_running_loop()
        session = ExternalSession(
            context,
            self.spec["scope"],
            deadline=self.operation_deadline,
            schemas=self.surface["schema_hashes"],
        )
        environment = module.get_environment()
        bound = BoundModel(context, self.spec, "user", loop, self.operation_deadline)
        try:
            user = build_user(
                "user_simulator",
                environment,
                self.task,
                llm=bound.name,
                llm_args=bound.arguments,
            )
            agent = create_agent(
                self.installed().root,
                domain,
                environment.get_tools(),
                environment.get_policy(),
                thread_exchange(session, loop),
            )
            orchestrator = Orchestrator(
                domain=domain,
                agent=agent,
                user=user,
                environment=environment,
                task=self.task,
                max_steps=self.config["max_steps"],
                max_errors=self.config["max_errors"],
                seed=self.config["seed"],
                validate_communication=self.config["enforce_communication_protocol"],
                timeout=session.remaining(),
                simulation_id=self.spec["scope"]["trial_id"],
            )
            with bound.native_transport(self.installed().root, domain):
                simulation = await asyncio.to_thread(orchestrator.run)
            if session.session_id is None:
                await session.open(agent.public_history(orchestrator.agent_state))
            completion = await session.close()
            self.record = {
                "format_version": STATE_SCHEMA,
                "native_version": VERSION,
                "native_revision": REVISION,
                "source_digest": self.spec["case"]["source_digest"],
                "case": self.spec["case"]["ref"],
                "surface_fingerprint": self.surface["fingerprint"],
                "interface": self.surface["interface"],
                "agent_seed_policy": "harness_profile",
                "native_config": deepcopy(self.config),
                "simulation": simulation.model_dump(mode="json"),
            }
            return {
                "agent_outcome": completion["outcome"],
                "native_termination": simulation.termination_reason.value,
            }
        finally:
            bound.close()

    async def _export(self, context, name):
        return (await self._broker(context, "export", {"output_id": name}, identity="recorder"))[
            "artifact"
        ]

    async def _capture(self, context):
        if self.captured is not None:
            return deepcopy(self.captured)
        if self.record is None or self.spec.get("purpose") == "evaluation":
            return {
                "artifacts": [],
                "regrade": "unavailable",
                "regrade_reason": "NATIVE_SIMULATION_INCOMPLETE",
            }
        self.operation_deadline = self._deadline(120)
        for name in ("trace", "checkpoint"):
            await self._write(context, name, self.record)
        trace, checkpoint = (
            await self._export(context, "trace"),
            await self._export(context, "checkpoint"),
        )
        self.captured = {
            "artifacts": [trace, checkpoint],
            "regrade": "available",
            "checkpoint": {
                "format_version": "1.0",
                "connector": {"id": CONNECTOR_ID, "version": CONNECTOR_VERSION},
                "case": self.spec["case"]["ref"],
                "source_digest": self.spec["case"]["source_digest"],
                "state_schema": STATE_SCHEMA,
                "complete": True,
                "artifacts": [checkpoint],
                "evaluator_roles": ["judge"] if needs_judge(self.task) else [],
                "state": {"surface_fingerprint": self.surface["fingerprint"]},
            },
        }
        return deepcopy(self.captured)

    async def _evaluate(self, context):
        if self.record is None:
            return unavailable("NATIVE_SIMULATION_INCOMPLETE")
        if self.config["evaluation_mode"] == "deferred":
            return unavailable("QUALITY_EVALUATION_DEFERRED")
        self.operation_deadline = self._deadline(self.spec["limits"]["wall_time_seconds"])
        domain = self.spec["case"]["native_ref"]["domain"]
        from tau2.data_model.simulation import SimulationRun
        from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation

        bound = (
            BoundModel(
                context, self.spec, "judge", asyncio.get_running_loop(), self.operation_deadline
            )
            if needs_judge(self.task)
            else None
        )
        try:
            with bound.judge(self.installed().root, domain) if bound else nullcontext():
                reward = await asyncio.to_thread(
                    evaluate_simulation,
                    SimulationRun.model_validate(self.record["simulation"]),
                    self.task,
                    EvaluationType.ALL,
                    False,
                    domain,
                    strict_replay=True,
                )
            report = {
                "reward_info": reward.model_dump(mode="json"),
                "source_digest": self.spec["case"]["source_digest"],
                "native_revision": REVISION,
                "evaluation_type": "all",
                "strict_replay": True,
            }
            await self._write(context, "report", report)
            artifact = await self._export(context, "report")
            return {
                "status": "completed",
                "success": None,
                "metrics": [
                    {
                        "name": "reward",
                        "value": reward.reward,
                        "direction": "higher",
                        "aggregation": "native_mean",
                    }
                ],
                "native": {
                    "reward": reward.reward,
                    "reward_basis": reward.model_dump(mode="json")["reward_basis"],
                    "termination": self.record["simulation"]["termination_reason"],
                    "interface": self.surface["interface"],
                },
                "artifacts": [artifact],
                "completeness": {"native_verdict": True, "native_trace": True},
            }
        finally:
            if bound:
                bound.close()

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
            if not cursor.isdecimal() or str(int(cursor)) != cursor or int(cursor) > len(cases):
                raise ProtocolError(
                    "INVALID_CURSOR", "Native case cursor is invalid", "configuration"
                )
            start, end, size = int(cursor), int(cursor), 4096
            for case in cases[start : start + params.get("limit", 100)]:
                size += len(canonical(case))
                if size > MAX_LINE_BYTES:
                    break
                end += 1
            if start == end and start < len(cases):
                raise ProtocolError(
                    "CASE_TOO_LARGE", "Native case exceeds the reply budget", "configuration"
                )
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
            return await self._prepare(context)
        if method == "drive":
            return await self._drive(context)
        if method == "capture":
            return await self._capture(context)
        if method == "evaluate":
            return await self._evaluate(context)
        if method == "cleanup":
            return {"cleaned": True}
        if method == "cancel":
            return {"cancelled": True}
        raise ProtocolError(
            "UNSUPPORTED_CAPABILITY", "Native tau2 method is unsupported", "unsupported"
        )


async def main():
    # Native dependencies may print diagnostics. Keep their stdout off JSONL.
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

    await serve(Tau2Connector(), lines(), write)


if __name__ == "__main__":
    asyncio.run(main())
