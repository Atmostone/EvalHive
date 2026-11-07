"""Bind the admitted agent container to its host-owned model connection."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import PurePosixPath
from secrets import token_urlsafe

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity

from app.harnesses.attempts import TERMINAL
from app.runtime.managed_spec import ContainerSpec, ManagedRuntimeError
from app.runtime.model_gateway import GatewayRejected, ModelGateway, ModelRoute
from app.runtime.model_socket import ModelSocket


def model_connection(spec, credentials):
    """Keep the provider connection on the host and issue a private role token."""
    model = spec.get("model", {})
    reference = model.get("credential_ref")
    if (
        model.get("api", "openai-compatible") != "openai-compatible"
        or not isinstance(reference, str)
        or reference not in credentials
    ):
        raise ManagedRuntimeError("Owned model transport requires a resolved model connection")
    try:
        route = ModelRoute(
            model=model.get("name"), endpoint=model.get("endpoint"),
            api_key=credentials[reference], token=token_urlsafe(32),
        )
    except ValueError:
        raise ManagedRuntimeError("Owned model transport requires a resolved model connection") from None
    private = deepcopy(credentials)
    private[reference] = route.token
    return route, private


class AgentModelChannel:
    """A single agent role; no model listener exists before group admission."""

    def __init__(self, calls, runtime, route):
        self.calls, self.runtime, self.route = calls, runtime, route
        self.listener = None

    async def _guard(self, role):
        if role != "agent":
            raise GatewayRejected("MODEL_GATEWAY_ROLE_UNAVAILABLE")
        try:
            async with self.calls.runner_transaction() as (_, trial, attempt, now):
                if (
                    trial.phase != "driving" or trial.cancel_requested_at is not None
                    or attempt.status in TERMINAL
                ):
                    raise GatewayRejected("MODEL_GATEWAY_ATTEMPT_INACTIVE")
                deadline = trial.deadline_at
                seconds = attempt.run_spec.get("limits", {}).get("wall_time_seconds")
                if attempt.started_at is not None and seconds is not None:
                    deadline = min(deadline, attempt.started_at + timedelta(seconds=seconds))
                return (deadline - now).total_seconds()
        except ProtocolError:
            raise GatewayRejected("MODEL_GATEWAY_OWNER_UNAVAILABLE") from None

    def bind(self, compiled, plan, provider_id):
        if self.listener is not None:
            raise ManagedRuntimeError("Model channel cannot bind another environment")
        agent = plan.get("agent", {})
        selected = compiled.leases.get(agent.get("environment_handle"))
        if agent.get("mode") != "in_environment" or selected is None:
            raise ManagedRuntimeError("Model channel requires a declared task container")
        provider, lease = selected
        if (
            provider != provider_id or lease.kind != "container"
            or not isinstance(lease.intent, ContainerSpec) or lease.intent.role != "environment"
            or lease.scope != compiled.scope
        ):
            raise ManagedRuntimeError("Model channel target differs from its admitted environment")
        target = PurePosixPath(ModelSocket.target)
        if any(
            target.is_relative_to(PurePosixPath(mount.target))
            or PurePosixPath(mount.target).is_relative_to(target)
            for mount in (*lease.intent.mounts, *lease.intent.storage)
        ):
            raise ManagedRuntimeError("Model channel overlaps a registered mount")
        listener = ModelSocket(
            ModelGateway({"agent": self.route}, self._guard), compiled.scope, self.runtime.policy
        )
        leases = dict(compiled.leases)
        leases[agent["environment_handle"]] = (
            provider, replace(lease, intent=replace(
                lease.intent, mounts=(*lease.intent.mounts, listener.mount)
            )),
        )
        descriptor = {"transport": "unix_http_v1", "socket_path": listener.task_path, "role": "agent"}
        result = replace(compiled, leases=leases, fingerprint=identity({
            "environment": compiled.fingerprint, "model_gateway": descriptor,
        }))
        self.listener = listener
        return result, descriptor

    async def start(self):
        if self.listener is None:
            raise ManagedRuntimeError("Model channel requires an installed environment")
        await self.listener.start()

    async def close(self):
        if self.listener is not None:
            await self.listener.close()
