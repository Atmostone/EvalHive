"""Provision an isolated SDK worker, then install its server-resolved task plan."""

from __future__ import annotations

import asyncio
import uuid
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from evalhive_benchmark import (
    ConnectorClient,
    ProtocolError,
    validate,
    validate_environment,
    validate_spec,
)
from evalhive_benchmark.peer import identity

from app.benchmarks.broker import SqlBroker
from app.benchmarks.catalog import merged_requirements
from app.benchmarks.evidence import _io
from app.benchmarks.resource_authority import ActorPolicy, install_session
from app.benchmarks.retained_inputs import RetainedInputs
from app.benchmarks.worker_channel import WorkerChannel
from app.runtime.admission import GroupPolicy, ResourceDemand, host_capacity
from app.runtime.file_pins import freeze_files
from app.runtime.managed_spec import ManagedRuntimeError, OwnerScope


class RootlessWorkerHost:
    """Rootless provider integration; native benchmark code stays in its image.

    The phase controller supplies ownership, installed metadata and private input
    grants. This component never chooses a harness or interprets native formats.
    Start/install are supervised by the controller; they are not recovery APIs.
    """

    WORKER_ID = "sdk.worker"

    def __init__(
        self,
        store,
        owner,
        manifest,
        assets,
        providers,
        *,
        group_policy=None,
        retained_evidence=None,
        worker_image_pin=None,
        host_private_inputs=None,
        agent_factory=None,
        host_auxiliary_disk_mb=0,
    ):
        validate("manifest", manifest)
        if manifest["kind"] != "benchmark" or (manifest["id"], manifest["version"]) != (
            assets.document["connector_id"],
            assets.document["connector_version"],
        ):
            raise ManagedRuntimeError("Worker manifest differs from registered assets")
        self.store, self.owner, self.manifest, self.assets = store, owner, manifest, assets
        self.purpose = "execution" if owner.control_id is None else "evaluation"
        if host_private_inputs and self.purpose != "execution":
            raise ManagedRuntimeError("Host agent inputs require an execution controller")
        self.host_private_inputs = dict(host_private_inputs or {})
        if type(host_auxiliary_disk_mb) is not int or not 0 <= host_auxiliary_disk_mb <= 1024:
            raise ManagedRuntimeError("Host auxiliary disk must be a bounded MiB amount")
        self.host_auxiliary_disk_mb = host_auxiliary_disk_mb
        if worker_image_pin is not None and self.purpose != "evaluation":
            raise ManagedRuntimeError("A retained worker image requires an evaluation controller")
        self.worker_image_pin = worker_image_pin
        self.group_policy = group_policy or GroupPolicy()
        self.host_actor = uuid.uuid5(owner.controller_id, "host.actor")
        self.worker_actor = uuid.uuid5(owner.controller_id, "benchmark.actor")
        self.scope = OwnerScope(
            str(owner.workspace_id),
            str(owner.trial_id),
            str(uuid.uuid5(owner.controller_id, "benchmark.worker")),
            owner.generation,
        )
        self.host_broker = SqlBroker(store, owner, self.host_actor, providers)
        self.worker_broker = SqlBroker(store, owner, self.worker_actor, providers)
        self.channel, self.worker_demand, self.environment, self.client = None, None, None, None
        self.started = False
        self.retained_inputs = RetainedInputs(
            store, owner, assets.runtime.policy, retained_evidence
        )
        self.plan, self.prepared, self.agent_gateway = None, None, None
        if agent_factory is not None:
            if self.purpose != "execution":
                raise ManagedRuntimeError("Agent factories require an execution controller")
            self.agent_gateway = agent_factory(self)
            calls = self.agent_gateway.calls
            if (
                calls.trials is not store
                or calls.owner != owner
                or calls.actor_id != self.worker_actor
            ):
                raise ManagedRuntimeError("Agent gateway differs from its worker ownership")
            self.host_auxiliary_disk_mb += self.agent_gateway.executor.spool_disk_mb
            if self.host_auxiliary_disk_mb > 1024:
                raise ManagedRuntimeError("Host auxiliary disk exceeds its bounded allowance")

    async def _measure(self):
        return await asyncio.to_thread(
            host_capacity, self.assets.runtime, Path(self.assets.runtime.policy.data.local)
        )

    def request(self, scope, operation, *, handle=None, payload=None, request_id=None):
        result = {
            "protocol_version": "1.0",
            "type": "broker_request",
            "id": request_id or uuid.uuid4().hex,
            "scope": {
                "workspace_id": scope.workspace_id,
                "trial_id": scope.trial_id,
                "generation": scope.generation,
            },
            "provider": self.assets.provider_id,
            "operation": operation,
            "payload": payload or {},
        }
        if handle is not None:
            result["handle"] = handle
        return result

    async def start(self):
        if self.channel is not None:
            return self.channel
        if self.started:
            raise ProtocolError(
                "WORKER_START_UNCERTAIN", "Worker startup requires reconciliation", "uncertain"
            )
        self.started = True
        lease, demand, _ = await self.assets.resource(
            self.scope, self.WORKER_ID, self.assets.worker_asset, kind="container"
        )
        declared_image = await asyncio.to_thread(
            self.assets.runtime.resolve_image, self.worker_image_pin or self.manifest["image"]
        )
        if declared_image["image_id"] != lease.intent.image_id:
            raise ManagedRuntimeError("Worker asset image differs from its installed manifest")
        if lease.intent.entrypoint is not None and lease.intent.entrypoint != tuple(
            self.manifest["entrypoint"]
        ):
            raise ManagedRuntimeError("Worker entrypoint differs from its installed manifest")
        lease = replace(
            lease,
            intent=replace(lease.intent, entrypoint=tuple(self.manifest["entrypoint"]), command=()),
        )
        self.worker_demand = demand
        file_pins = {}
        if self.manifest["capabilities"].get("regrade") == "native":
            file_pins = await _io(
                lambda: freeze_files(self.assets.files, self.assets.runtime.policy)
            )
        await self.store.reserve(
            self.owner,
            demands=(demand,),
            native_resources={},
            measure=self._measure,
            runtime_policy=self.assets.runtime.policy,
            group_policy=self.group_policy,
            stage="planning",
        )
        await install_session(
            self.store,
            self.owner,
            self.scope,
            leases={self.WORKER_ID: (self.assets.provider_id, lease)},
            actors={
                self.host_actor: ActorPolicy(
                    (self.assets.provider_id,),
                    (self.WORKER_ID,),
                    tuple(
                        phase
                        for phase in (
                            "planning",
                            "preparing",
                            "ready",
                            "driving",
                            "capturing",
                            "evaluating",
                            "cleaning",
                        )
                        if phase != "driving" or self.purpose == "execution"
                    ),
                    ("allocate", "inspect", "stop", "release"),
                )
            },
            registration={
                "fingerprint": identity(self.assets.document),
                "catalog": self.assets.document,
                "worker_image_id": lease.intent.image_id,
                "manifest": self.manifest,
                "file_pins": file_pins,
            },
            purpose=self.purpose,
        )
        allocated = await self.host_broker.handle(
            self.request(
                self.scope,
                "allocate",
                payload={"resource_id": self.WORKER_ID},
                request_id="worker.allocate",
            )
        )
        process = await self.host_broker.open_transport(
            self.request(
                self.scope, "inspect", handle=allocated["handle"], request_id="worker.attach"
            )
        )
        client = self.client = ConnectorClient(
            process,
            broker=self.worker_broker.handle,
            broker_replay=self.worker_broker.replay,
            agent=None if self.agent_gateway is None else self.agent_gateway.handle,
            agent_replay=None if self.agent_gateway is None else self.agent_gateway.replay,
        )
        self.channel = await WorkerChannel.attach(self.store, self.owner, self.worker_actor, client)
        description = await self.channel.invoke("describe", request_id="worker.describe")
        # Images cannot contain their own final Docker digest. Those immutable
        # references belong to installation metadata; semantic SDK declarations
        # must still match what the installed worker reports.
        fields = (
            "protocol_version",
            "kind",
            "id",
            "version",
            "config_schema",
            "secret_schema",
            "capabilities",
            "requirements",
            "source_lock_digest",
        )
        if any(description.get(field) != self.manifest.get(field) for field in fields):
            raise ProtocolError(
                "WORKER_MISMATCH",
                "Worker description differs from installed metadata",
                "configuration",
            )
        return self.channel

    async def install_plan(self, spec, plan, *, private_inputs=None, extra_demands=()):
        if self.channel is None:
            raise ManagedRuntimeError("Task planning requires a started SDK worker")
        validate_spec(self.manifest, spec)
        validate_environment(spec, plan)
        if set(private_inputs or {}) & set(self.host_private_inputs):
            raise ManagedRuntimeError("Worker and host private input identities must be distinct")
        if spec.get("purpose", "execution") != self.purpose:
            raise ProtocolError(
                "PURPOSE_CONFLICT", "Plan purpose differs from its controller", "configuration"
            )
        if spec["scope"] != {
            "workspace_id": str(self.owner.workspace_id),
            "trial_id": str(self.owner.trial_id),
            "generation": self.owner.generation,
        }:
            raise ProtocolError("STALE_GENERATION", "Task plan belongs to another worker scope")
        scope = replace(
            self.scope,
            session_id=str(uuid.uuid5(self.owner.controller_id, "benchmark.environment")),
        )
        retained = self.retained_inputs.plan(spec, plan, scope)
        host_inputs = self.host_private_inputs
        if self.agent_gateway is not None:
            prepare_inputs = getattr(self.agent_gateway.executor, "plan_private_inputs", None)
            if prepare_inputs is not None:
                host_inputs = prepare_inputs(plan, host_inputs)
        compiled = await self.assets.compile(
            scope, plan, reserved_ids=(self.WORKER_ID,), retained_inputs=retained.grants
        )
        requirements = merged_requirements(
            self.manifest["requirements"], spec["case"]["requirements"], plan["requirements"]
        )
        demands = (self.worker_demand, *compiled.demands, *extra_demands)
        processes = tuple(item for item in demands if isinstance(item, ResourceDemand))
        totals = {
            "memory_mb": sum(item.limits.memory_mb for item in processes),
            "cpu_cores": sum(Decimal(str(item.limits.cpu_cores)) for item in processes),
            "disk_mb": sum(item.disk_mb for item in demands)
            + retained.disk_mb
            + self.host_auxiliary_disk_mb,
        }
        if any(value > Decimal(str(spec["limits"][name])) for name, value in totals.items()):
            raise ProtocolError(
                "LIMIT_EXCEEDED", "Installed resource group exceeds the trial budget", "preparation"
            )
        await self.store.reserve(
            self.owner,
            demands=demands,
            native_resources=requirements.get("resources", {}),
            measure=self._measure,
            runtime_policy=self.assets.runtime.policy,
            group_policy=self.group_policy,
            auxiliary_disk_mb=retained.disk_mb + self.host_auxiliary_disk_mb,
        )
        await self.retained_inputs.materialize(retained)
        resources = tuple(compiled.leases)
        policy = ActorPolicy(
            (self.assets.provider_id,),
            resources,
            tuple(
                phase
                for phase in (
                    "preparing",
                    "ready",
                    "driving",
                    "capturing",
                    "evaluating",
                    "cleaning",
                )
                if phase != "driving" or self.purpose == "execution"
            ),
            ("allocate", "exec", "inspect", "stop", "export", "release"),
            tuple((private_inputs or {}).keys()),
            min(86400, spec["limits"]["wall_time_seconds"]),
        )
        await install_session(
            self.store,
            self.owner,
            scope,
            leases=compiled.leases,
            actors={
                self.worker_actor: policy,
                self.host_actor: replace(
                    policy,
                    private_input_refs=tuple((private_inputs or {}).keys())
                    + tuple(self.host_private_inputs),
                ),
            },
            private_inputs={**(private_inputs or {}), **host_inputs},
            outputs=compiled.output_grants((self.worker_actor, self.host_actor)),
            links=compiled.links,
            registration={
                "fingerprint": compiled.fingerprint,
                "images": compiled.images,
                "files": compiled.files,
                "catalog": self.assets.document,
                "applied_spec": spec,
                "output_plan": plan["outputs"],
            },
            purpose=self.purpose,
        )
        self.environment = compiled
        self.plan = deepcopy(plan)
        return compiled

    async def bind_prepared(self, prepared):
        validate("prepared", prepared)
        if self.agent_gateway is not None:
            self.agent_gateway.configure(prepared)
        self.prepared = deepcopy(prepared)

    async def release_inputs(self):
        await self.retained_inputs.release()

    async def detach(self, *, grace=3):
        from contextlib import suppress

        from app.benchmarks.async_control import bounded, cancel_tasks

        if self.channel is not None:
            await self.channel.detach(grace=grace)
        elif self.client is not None:
            await cancel_tasks([self.client.reading], grace)
            with suppress(TimeoutError):
                await bounded(self.client.process.aclose(), grace)
        await self.host_broker.detach()
        await self.worker_broker.detach()
