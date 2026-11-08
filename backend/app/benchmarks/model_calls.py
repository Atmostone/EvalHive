"""Durable auxiliary model requests over explicit host-owned role connections."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import httpx
from evalhive_benchmark import ProtocolError, encode_line, validate
from evalhive_benchmark.peer import identity
from evalhive_runner.redaction import Redactor
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.async_control import bounded, critical
from app.benchmarks.commands import CommandStore
from app.benchmarks.trials import failure
from app.models.benchmark_command import BenchmarkCommand
from app.models.benchmark_trial import BenchmarkEventStream, BenchmarkHostReservation
from app.runtime.managed_spec import ManagedRuntimeError
from app.runtime.model_gateway import GatewayRejected, ModelGateway, ModelRoute


@dataclass(frozen=True)
class ModelBinding:
    credential_ref: str
    route: ModelRoute
    phases: tuple[str, ...]

    def __post_init__(self):
        if (
            not isinstance(self.credential_ref, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", self.credential_ref)
            or not isinstance(self.route, ModelRoute)
            or not self.phases
            or set(self.phases) - {"driving", "evaluating"}
        ):
            raise ManagedRuntimeError("Auxiliary models require an explicit role and phase binding")


def observed_usage(role, model, phase, call_id, result):
    native = result["body"].get("usage", {})
    if not isinstance(native, dict):
        native = {}

    def count(value):
        return value if type(value) is int and 0 <= value <= 2**53 - 1 else None

    def detail(key, name):
        value = native.get(key, {})
        return count(value.get(name)) if isinstance(value, dict) else None

    value = {
        "usage_id": "model." + str(call_id),
        "role": role,
        "model": model,
        "purpose": phase,
        "mode": "cumulative",
        "input_tokens": count(native.get("input_tokens", native.get("prompt_tokens"))),
        "output_tokens": count(native.get("output_tokens", native.get("completion_tokens"))),
        "reasoning_tokens": detail("output_tokens_details", "reasoning_tokens")
        if "output_tokens_details" in native
        else detail("completion_tokens_details", "reasoning_tokens"),
        "cache_read_tokens": detail("input_tokens_details", "cached_tokens")
        if "input_tokens_details" in native
        else detail("prompt_tokens_details", "cached_tokens"),
        "cache_write_tokens": None,
        "cost": None,
        "metadata": {"source": "host.model_api_v1", "http_status": result["status_code"]},
    }
    validate("usage", value)
    return value


class AuxiliaryModels:
    def __init__(self, store, owner, actor_id, bindings, *, client=None):
        if (
            not isinstance(bindings, dict)
            or len(bindings) > 32
            or any(
                not isinstance(role, str)
                or role == "agent"
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}", role)
                or not isinstance(binding, ModelBinding)
                for role, binding in bindings.items()
            )
        ):
            raise ManagedRuntimeError(
                "Auxiliary models require declared non-agent role connections"
            )
        self.store, self.owner, self.actor_id = store, owner, uuid.UUID(str(actor_id))
        self.bindings, self.fingerprint, self.max_requests = dict(bindings), None, None
        self.commands = CommandStore(store, owner, self.actor_id)
        self.redactor = Redactor(
            [
                value
                for binding in bindings.values()
                for value in (binding.route.api_key, binding.route.token)
            ]
        )
        self.gateway = (
            ModelGateway(
                {role: binding.route for role, binding in bindings.items()},
                self._guard,
                client=client,
                max_response_bytes=768 * 1024,
            )
            if bindings
            else None
        )

    def configure(self, spec):
        validate("trial_spec", spec)
        selected = {role: value for role, value in spec["models"].items() if role != "agent"}
        if set(selected) != set(self.bindings) or any(
            (value["name"], value["endpoint"], value["credential_ref"])
            != (
                self.bindings[role].route.model,
                self.bindings[role].route.endpoint,
                self.bindings[role].credential_ref,
            )
            for role, value in selected.items()
        ):
            raise ManagedRuntimeError("Auxiliary connections differ from the frozen trial models")
        budget = spec["limits"].get("model_requests", 128)
        if type(budget) is not int or not 0 <= budget <= 10000:
            raise ManagedRuntimeError("Auxiliary model request budget exceeds host policy")
        fingerprint = identity({"models": selected, "budget": budget, "scope": spec["scope"]})
        if self.fingerprint is not None and self.fingerprint != fingerprint:
            raise ManagedRuntimeError("Auxiliary model configuration is already frozen")
        self.fingerprint, self.max_requests = fingerprint, budget

    async def _current(self, db, role):
        trial, now = await self.store.current(db, self.owner, row_lock=True)
        binding = self.bindings.get(role)
        if (
            self.fingerprint is None
            or binding is None
            or trial.phase not in binding.phases
            or trial.cancel_requested_at is not None
            or trial.deadline_at <= now
        ):
            raise failure("MODEL_ROLE_INACTIVE", "Auxiliary model role is inactive", "execution")
        stream = await db.scalar(
            select(BenchmarkEventStream).where(
                BenchmarkEventStream.trial_id == self.owner.trial_id,
                BenchmarkEventStream.control_id == self.owner.control_id,
                BenchmarkEventStream.generation == self.owner.generation,
                BenchmarkEventStream.actor_id == self.actor_id,
            )
        )
        if stream is None or stream.final_seq is not None:
            raise failure("MODEL_ACTOR_INACTIVE", "Auxiliary model actor is inactive", "execution")
        reservation = await db.get(BenchmarkHostReservation, self.store.host_id)
        if (
            reservation is None
            or (reservation.trial_id, reservation.generation, reservation.controller_id)
            != (self.owner.trial_id, self.owner.generation, self.owner.controller_id)
            or reservation.budget.get("stage") != "full"
        ):
            raise failure(
                "GROUP_ADMISSION_REQUIRED",
                "Auxiliary models require full group admission",
                "preparation",
            )
        return trial, now, stream

    async def _guard(self, role):
        try:
            async with AsyncSession(self.store.engine) as db:
                trial, now, _ = await self._current(db, role)
                return (trial.deadline_at - now).total_seconds()
        except ProtocolError:
            raise GatewayRejected("MODEL_GATEWAY_OWNER_UNAVAILABLE") from None

    def _frame(self, request):
        validate("command", request)
        encode_line(request)
        if request["method"] != "model.call" or request["params"]["scope"] != {
            "workspace_id": str(self.owner.workspace_id),
            "trial_id": str(self.owner.trial_id),
            "generation": self.owner.generation,
        }:
            raise failure("FOREIGN_MODEL_CALL", "Model call differs from its worker scope")
        return {**request, "id": "model." + uuid.uuid5(self.actor_id, request["id"]).hex}

    async def replay(self, request):
        ticket = await self.commands.begin(self._frame(request), replay_only=True)
        return self._unwrap(ticket.replay)

    @staticmethod
    def _unwrap(receipt):
        if receipt["ok"]:
            return receipt["result"]
        error = receipt["error"]
        raise ProtocolError(error["code"], error["message"], error["kind"])

    async def handle(self, request):
        frame = self._frame(request)
        params = frame["params"]
        role, binding = params["role"], self.bindings.get(params["role"])

        async def authorize(db, trial, stream, now):
            await self._current(db, role)
            if binding is None or params["body"]["model"] != binding.route.model:
                raise failure(
                    "MODEL_ROLE_UNAVAILABLE", "Auxiliary model differs from its bound role"
                )
            count = await db.scalar(
                select(func.count())
                .select_from(BenchmarkCommand)
                .where(
                    BenchmarkCommand.stream_id == stream.id,
                    BenchmarkCommand.method == "model.call",
                )
            )
            if count >= self.max_requests:
                raise failure(
                    "MODEL_REQUEST_LIMIT",
                    "Auxiliary model request budget is exhausted",
                    "execution",
                )

        async def record_started(db, trial, row):
            await self._usage(
                db, trial, row, role, binding.route.model, {"body": {}, "status_code": None}
            )

        ticket = await self.commands.begin(frame, authorize=authorize, record=record_started)
        if ticket.replay is not None:
            return self._unwrap(ticket.replay)
        base = {"protocol_version": "1.0", "type": "response", "id": frame["id"]}
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.gateway),
                base_url="http://auxiliary-model",
                trust_env=False,
            ) as client:
                response = await client.post(
                    "/" + role + "/v1/" + params["operation"],
                    json=params["body"],
                    headers={"Authorization": "Bearer " + binding.route.token},
                )
            try:
                body = self.redactor.value(response.json())
            except (ValueError, RecursionError):
                raise failure(
                    "MODEL_RESPONSE_INVALID",
                    "Auxiliary provider returned invalid JSON",
                    "recording",
                ) from None
            result = {"status_code": response.status_code, "body": body}
            validate("model_result", result)
            receipt = {**base, "ok": True, "result": result}
            encode_line(receipt)
        except ProtocolError as error:
            receipt = {
                **base,
                "ok": False,
                "error": {
                    "code": error.code,
                    "kind": error.kind,
                    "message": "Auxiliary model request failed",
                },
            }
        except BaseException:
            await critical(
                bounded(self.commands.uncertain(ticket.id, code="MODEL_CALL_UNCERTAIN"), 3)
            )
            raise

        async def record(db, trial, row, confirmed):
            if confirmed["ok"]:
                await self._usage(db, trial, row, role, binding.route.model, confirmed["result"])

        await self.commands.receive(ticket.id, receipt, record=record)
        completed = await self.commands.complete(ticket.id, event_floor=-1)
        return self._unwrap(completed.replay)

    async def _usage(self, db, trial, row, role, model, result):
        usage = observed_usage(role, model, row.phase, row.id, result)
        entry_id = uuid.uuid4()
        await self.store.append(
            db,
            trial,
            "usage",
            {"source": "host.model_api_v1"},
            id=entry_id,
            private_data=self.store.private.seal("journal:" + str(entry_id), {"data": usage}),
        )

    async def close(self):
        if self.gateway is not None:
            await self.gateway.close()
