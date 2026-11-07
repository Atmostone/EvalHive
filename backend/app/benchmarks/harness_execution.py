"""Compose the existing Harness SDK client and runner over an owned transport."""

import asyncio
import math
import uuid
from copy import deepcopy

from evalhive_harness import ProtocolError as HarnessProtocolError
from evalhive_harness import validate_run
from evalhive_harness.client import ConnectorClient
from evalhive_runner.redaction import Redactor
from evalhive_runner.runner import AttemptRunner, RunnerPolicy
from evalhive_runner.spool import EventSpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.agent_resources import compile_resources, private_inputs
from app.benchmarks.async_control import bounded, consume
from app.benchmarks.harness_api import BenchmarkAttemptAPI
from app.benchmarks.trials import failure
from app.harnesses.attempts import TERMINAL, AttemptError, create_attempt, queue_command
from app.models.execution_attempt import ExecutionAttempt, ExecutionEvent


class HarnessExecution:
    """All arguments are server-resolved; workers cannot supply a process factory.

    The transport factory starts an admitted provider-owned process and must use
    that provider's ownership fence. Spool storage belongs to the admitted host
    context. No native harness or benchmark implementation is imported here.
    """

    def __init__(
        self,
        calls,
        manifest,
        spec,
        transport,
        spool_path,
        *,
        profile_id=None,
        credential_ref=None,
        private_values=(),
        policy=None,
        max_pending_bytes=8 * 1024**2,
    ):
        validate_run(manifest, spec)
        if (
            type(max_pending_bytes) is not int
            or not 2 * 1024**2 <= max_pending_bytes <= 64 * 1024**2
        ):
            raise ValueError("Harness spool capacity must be between 2 and 64 MiB")
        self.calls, self.manifest, self.spec = calls, deepcopy(manifest), deepcopy(spec)
        self.transport, self.spool_path, self.profile_id = transport, spool_path, profile_id
        interaction = spec.get("interaction", {}).get("mode", "single_turn")
        self.mode = "autonomous" if interaction == "single_turn" else interaction
        self.api, self.redactor = BenchmarkAttemptAPI(calls), Redactor(private_values)
        self.policy, self.max_pending_bytes = policy or RunnerPolicy(), max_pending_bytes
        self.prepared, self.starting = False, False
        self.access_configured = False
        self.job, self.connector, self.spool = None, None, None
        self.credential_ref, self.resource_values = credential_ref, None

    def plan_private_inputs(self, plan, inputs):
        _, values = compile_resources(plan.get("agent", {}).get("resources", []))
        resolved = private_inputs(inputs, self.credential_ref, values)
        self.resource_values = values
        return resolved

    @property
    def spool_disk_mb(self):
        return 2 * math.ceil(self.max_pending_bytes / 1024**2) + 8

    def configure_access(self, prepared):
        """Apply native task access while preserving frozen model/profile settings."""
        from evalhive_benchmark import validate

        validate("prepared", prepared)
        if self.prepared or self.starting or self.access_configured:
            raise failure("AGENT_ALREADY_STARTED", "Native agent access is already frozen")
        agent = prepared.get("agent")
        if agent is None or agent["interaction"] != self.mode:
            raise failure(
                "AGENT_MODE_CONFLICT", "Native access differs from the frozen interaction mode"
            )
        spec = deepcopy(self.spec)
        spec["task"] = {**spec["task"], "prompt": agent["prompt"]}
        spec["cwd"] = agent["cwd"]
        spec["instructions"] = [*spec.get("instructions", []), *agent["instructions"]]
        if "wall_time_seconds" in agent:
            budget = agent["wall_time_seconds"]
            spec["limits"] = {
                **spec.get("limits", {}),
                "wall_time_seconds": min(spec.get("limits", {}).get("wall_time_seconds", budget), budget),
            }
        environment = spec.get("environment", {})
        resources = {resource["id"]: resource for resource in environment.get("resources", [])}
        declarations, values = compile_resources(agent["resources"])
        if values != (self.resource_values or {}):
            raise failure(
                "AGENT_RESOURCE_CONFLICT", "Native runtime inputs differ from their installed plan"
            )
        for resource in declarations:
            if resource["id"] in resources and resources[resource["id"]] != resource:
                raise failure(
                    "AGENT_RESOURCE_CONFLICT",
                    "Native resource differs from the frozen role resource",
                )
            resources[resource["id"]] = deepcopy(resource)
        spec["environment"] = {
            **environment,
            "id": environment.get("id", "benchmark"),
            "resources": list(resources.values()),
        }
        spec["credential_refs"] = {
            **spec.get("credential_refs", {}),
            **{reference: "runtime-injected" for reference in values},
        }
        if "tool_mapping" in agent:
            spec["metadata"] = {
                **spec.get("metadata", {}),
                "benchmark_tool_mapping": deepcopy(agent["tool_mapping"]),
            }
        try:
            validate_run(self.manifest, spec)
        except HarnessProtocolError:
            raise failure(
                "INVALID_AGENT_ACCESS", "Native access is incompatible with the Harness contract"
            ) from None
        self.spec = spec
        self.access_configured = True

    async def prepare(self, initial_history=None):
        if self.prepared:
            raise failure(
                "AGENT_START_UNCERTAIN", "Agent preparation cannot be repeated", "uncertain"
            )
        spec = deepcopy(self.spec)
        if initial_history is not None:
            if self.mode == "autonomous":
                raise failure(
                    "AGENT_MODE_CONFLICT", "Autonomous execution cannot accept initial history"
                )
            spec["interaction"]["initial_history"] = deepcopy(initial_history)
        async with AsyncSession(self.calls.trials.engine, expire_on_commit=False) as db:
            trial, _, now = await self.calls._current(db, starting=True)
            if await db.get(ExecutionAttempt, self.calls.session_id) is not None:
                raise failure("AGENT_START_UNCERTAIN", "Agent attempt already exists", "uncertain")
            spec.update(
                attempt_id=str(self.calls.session_id),
                task_id=str(trial.task_id),
                workspace_id=str(trial.workspace_id),
            )
            spec["metadata"] = {
                **spec.get("metadata", {}),
                "benchmark_binding": self.calls._binding(trial).metadata(),
            }
            remaining = max(1, math.ceil((trial.deadline_at - now).total_seconds()))
            spec["limits"] = {
                **spec.get("limits", {}),
                "wall_time_seconds": min(
                    spec.get("limits", {}).get("wall_time_seconds", remaining), remaining
                ),
            }
            try:
                validate_run(self.manifest, spec)
            except HarnessProtocolError:
                raise failure(
                    "INVALID_AGENT_INPUT",
                    "Agent input is incompatible with the frozen Harness contract",
                ) from None
            await create_attempt(
                db,
                workspace_id=trial.workspace_id,
                task_id=trial.task_id,
                manifest=self.manifest,
                run_spec=spec,
                profile_id=self.profile_id,
            )
            await db.commit()
        self.spec, self.prepared = spec, True

    async def start(self):
        if not self.prepared or self.starting:
            raise failure("AGENT_START_UNCERTAIN", "Agent startup cannot be repeated", "uncertain")
        self.starting = True
        # Assert the durable binding before the provider may start a process.
        async with self.calls.runner_transaction():
            pass
        process = await self.transport()
        self.connector = ConnectorClient(process)
        try:
            self.spool = EventSpool(self.spool_path, max_pending_bytes=self.max_pending_bytes)
            runner = AttemptRunner(
                spec=self.spec,
                manifest=self.manifest,
                connector=self.connector,
                api=self.api,
                spool=self.spool,
                redactor=self.redactor,
                policy=self.policy,
            )
            self.job = asyncio.create_task(self._run(runner))
            self.job.add_done_callback(consume)
        except BaseException:
            try:
                await self.connector.close(grace=self.policy.shutdown_seconds)
            finally:
                if finalize := getattr(self.connector.process, "finalize_execution", None):
                    await bounded(finalize(), self.policy.shutdown_seconds)
            raise
        # Readiness means a committed start or terminal record, rather than
        # trusting an in-memory start response from the native process.
        while True:
            async with self.calls.runner_transaction() as (_, _, attempt, _):
                if attempt.status in TERMINAL:
                    return
                if self.mode == "autonomous" and attempt.started_at is not None:
                    return
            if self.mode != "autonomous":
                async with self.calls.runner_transaction() as (db, _, attempt, _):
                    ready = await db.scalar(
                        select(ExecutionEvent.id)
                        .where(
                            ExecutionEvent.attempt_id == attempt.id,
                            ExecutionEvent.event == "session.waiting_input",
                        )
                        .limit(1)
                    )
                    if ready is not None:
                        return
            if self.job.done():
                await self.job
                async with self.calls.runner_transaction() as (_, _, attempt, _):
                    if attempt.status in TERMINAL:
                        return
                raise failure(
                    "HARNESS_RECORDING_MISSING",
                    "Agent startup has no committed evidence",
                    "recording",
                )
            await asyncio.sleep(0.05)

    async def _run(self, runner):
        try:
            return await runner.run()
        finally:
            try:
                if finalize := getattr(self.connector.process, "finalize_execution", None):
                    await bounded(finalize(), self.policy.shutdown_seconds)
            finally:
                self.spool.close()

    async def wait(self):
        if self.job is None:
            raise failure("AGENT_NOT_STARTED", "Agent runner has not started", "execution")
        result = await asyncio.shield(self.job)
        if result["status"] == "delivery_pending":
            raise failure(
                "HARNESS_DELIVERY_PENDING", "Agent evidence requires recovery", "recording"
            )

    async def control(self, command_id, name, payload):
        try:
            await self.api.command(command_id, name, payload)
        except HarnessProtocolError as error:
            raise failure(
                "INVALID_AGENT_COMMAND",
                "Agent command is incompatible with the Harness contract",
                "unsupported" if error.code == "UNSUPPORTED_CAPABILITY" else "configuration",
            ) from None
        except AttemptError as error:
            if error.code != "ATTEMPT_FINISHED":
                raise
            raise failure(
                "AGENT_FINISHED", "Agent session has already finished", "execution"
            ) from None
        while True:
            result = await self.api.command_result(command_id)
            if result is not None:
                return result
            if self.job is None or self.job.done():
                result = await self.api.command_result(command_id)
                if result is not None:
                    return result
                raise failure(
                    "AGENT_COMMAND_UNCERTAIN",
                    "Agent command has no native acknowledgement",
                    "uncertain",
                )
            await asyncio.sleep(0.05)

    async def close(self, command_id, reason):
        async with self.calls.runner_transaction() as (db, _, attempt, _):
            if attempt.status in TERMINAL:
                return
            last = await db.scalar(
                select(ExecutionEvent)
                .where(
                    ExecutionEvent.attempt_id == attempt.id,
                    ExecutionEvent.event.in_(["session.waiting_input", "turn.started"]),
                )
                .order_by(ExecutionEvent.seq.desc())
                .limit(1)
            )
            if last is None or last.event != "session.waiting_input":
                raise failure(
                    "AGENT_NOT_IDLE",
                    "Agent session must reach an input boundary before closing",
                    "execution",
                )
            revision = last.payload["data"]["revision"]
        acknowledgement = await self.control(
            command_id,
            "session.close",
            {
                "input_id": str(command_id),
                "expected_revision": revision,
                "reason": reason,
            },
        )
        if not acknowledgement["ok"]:
            raise failure(
                "AGENT_CLOSE_REJECTED", "Harness did not accept session close", "execution"
            )

    async def cancel(self):
        async with AsyncSession(self.calls.trials.engine) as db:
            trial, _, _ = await self.calls._current(db)
            if await db.get(ExecutionAttempt, self.calls.session_id) is None:
                return
            attempt = await self.calls._attempt(db, trial)
            if attempt.status not in TERMINAL:
                await queue_command(
                    db,
                    attempt.id,
                    "abort",
                    {},
                    command_id=uuid.uuid5(attempt.id, "benchmark.abort"),
                )
                await db.commit()
