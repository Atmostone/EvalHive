"""PostgreSQL authority for admitted rootless resource operations.

The host binds a controller epoch and an installed actor policy before exposing a
broker endpoint. None of those permissions are accepted from an RPC payload.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from evalhive_benchmark import ProtocolError, encode_line, validate
from evalhive_benchmark.peer import identity
from sqlalchemy import func, select
from sqlalchemy.orm import undefer

from app.benchmarks.async_control import critical
from app.benchmarks.private_store import binary, unbinary
from app.benchmarks.recovery_order import dependency_order
from app.benchmarks.trials import database_fence, failure
from app.models.benchmark_trial import (
    BenchmarkEnvironmentSession,
    BenchmarkHostReservation,
    BenchmarkOperation,
    BenchmarkResourceLease,
)
from app.runtime.broker import ExecutionGrant, ExportGrant, ResourceLease
from app.runtime.lease_codec import decode_lease, encode_lease
from app.runtime.managed_spec import ContainerSpec, ExecSpec, NetworkRequest, OwnerScope


@dataclass(frozen=True)
class ActorPolicy:
    provider_ids: tuple[str, ...]
    resources: tuple[str, ...]
    phases: tuple[str, ...]
    operations: tuple[str, ...]
    private_input_refs: tuple[str, ...] = ()
    max_exec_seconds: float = 60


async def install_session(
    store,
    owner,
    scope,
    *,
    leases,
    actors,
    private_inputs=None,
    outputs=None,
    links=None,
    registration=None,
    purpose="execution",
):
    """Freeze server-resolved plans and scoped permissions before any allocation.

    leases maps logical IDs to (provider ID, ResourceLease). links references the
    plan's network/namespace resources; they resolve only to allocated owned
    native handles. Private inputs and export paths never come from the worker.
    """
    if (scope.workspace_id, scope.trial_id, scope.generation) != (
        str(owner.workspace_id),
        str(owner.trial_id),
        owner.generation,
    ):
        raise failure("INVALID_SCOPE", "Session origin must match its controller")
    if purpose not in {"execution", "evaluation"}:
        raise failure("INVALID_PLAN", "Environment purpose is invalid")
    handles = {lease.handle for _, lease in leases.values()}
    if len(handles) != len(leases) or any(
        set(lease.dependencies) - handles for _, lease in leases.values()
    ):
        raise failure(
            "INVALID_PLAN", "Dependencies must name resources in the same installed session"
        )
    dependency_order({lease.handle: set(lease.dependencies) for _, lease in leases.values()})
    for actor_id, policy in actors.items():
        uuid.UUID(str(actor_id))
        if (
            isinstance(policy.max_exec_seconds, bool)
            or not math.isfinite(policy.max_exec_seconds)
            or not 0 < policy.max_exec_seconds <= 86400
        ):
            raise failure("INVALID_PLAN", "Actor deadline grant is invalid")
    plan = {
        "version": 1,
        "scope": asdict(scope),
        "purpose": purpose,
        "actors": {str(key): asdict(value) for key, value in actors.items()},
        "private_inputs": {key: binary(value) for key, value in (private_inputs or {}).items()},
        "outputs": outputs or {},
        "links": links or {},
        "registration": registration or {},
        "resources": {
            key: {"provider": provider, "lease": encode_lease(lease)}
            for key, (provider, lease) in leases.items()
        },
    }
    # Strict JSON validation occurs before any row is written.
    # dataclass tuples must be represented as arrays in the wire/storage format.
    plan = json.loads(json.dumps(plan))
    digest = identity(plan)
    async with database_fence(store.engine, owner.trial_id) as db:
        trial, now = await store.current(db, owner)
        allowed_phases = (
            {"planning", "preparing"}
            if purpose == "execution" or owner.control_id is not None
            else {"capturing", "evaluating"}
        )
        if (
            trial.phase not in allowed_phases
            or trial.cancel_requested_at
            or trial.deadline_at <= now
        ):
            raise failure("INVALID_PHASE", "Resource plans require an active preparation phase")
        reservation = await db.get(BenchmarkHostReservation, store.host_id)
        if reservation is None or (
            reservation.trial_id,
            reservation.generation,
            reservation.controller_id,
        ) != (owner.trial_id, owner.generation, owner.controller_id):
            raise failure(
                "NO_RESERVATION", "Session requires an owned host reservation", "preparation"
            )
        if reservation.budget.get("stage", "full") == "planning" and (
            purpose != "execution"
            or len(leases) != 1
            or any(
                lease.kind != "container"
                or not isinstance(lease.intent, ContainerSpec)
                or lease.intent.role != "worker"
                for _, lease in leases.values()
            )
        ):
            raise failure(
                "PLANNING_WORKER_ONLY", "Planning admission only permits its isolated worker"
            )
        previous = await db.get(BenchmarkEnvironmentSession, uuid.UUID(scope.session_id))
        if previous is not None:
            if (
                previous.plan_digest != digest
                or previous.trial_id != owner.trial_id
                or previous.control_id != owner.control_id
            ):
                raise failure("PLAN_CONFLICT", "Installed session plan is immutable")
            return previous.id
        for logical_id, (provider, lease) in leases.items():
            if (
                lease.scope != scope
                or lease.state != "planned"
                or lease.native is not None
                or lease.kind not in {"container", "network"}
                or not isinstance(
                    lease.intent, ContainerSpec if lease.kind == "container" else NetworkRequest
                )
                or lease.handle != lease.intent.resource_id
                or lease.intent.scope != scope
            ):
                raise failure("INVALID_PLAN", "Session contains an invalid resource declaration")
            uuid.UUID(lease.handle)
            if not logical_id or len(logical_id) > 200 or not provider:
                raise failure("INVALID_PLAN", "Session resource identity is invalid")
            if lease.kind == "container":
                admitted = {
                    item["resource_id"]: item for item in reservation.budget["resources"]
                }.get(logical_id)
                if admitted is None or admitted["limits"] != asdict(lease.intent.quota):
                    raise failure(
                        "UNADMITTED_RESOURCE", "Resource limits differ from the host reservation"
                    )
            existing = await db.scalar(
                select(BenchmarkResourceLease.id)
                .join(BenchmarkEnvironmentSession)
                .where(
                    BenchmarkEnvironmentSession.control_id == owner.control_id,
                    BenchmarkResourceLease.trial_id == owner.trial_id,
                    BenchmarkResourceLease.logical_id == logical_id,
                    BenchmarkResourceLease.state != "released",
                )
                .limit(1)
            )
            if existing is not None:
                raise failure("RESOURCE_IN_USE", "Plan resource already has a live lease")
        db.add(
            BenchmarkEnvironmentSession(
                id=uuid.UUID(scope.session_id),
                trial_id=owner.trial_id,
                workspace_id=owner.workspace_id,
                origin_generation=scope.generation,
                control_id=owner.control_id,
                purpose=purpose,
                state="planned",
                plan_digest=digest,
                private_plan=store.private.seal("session:" + scope.session_id, plan),
            )
        )
        await db.flush()
        for logical_id, (provider, lease) in leases.items():
            db.add(
                BenchmarkResourceLease(
                    id=uuid.UUID(lease.handle),
                    session_id=uuid.UUID(scope.session_id),
                    trial_id=owner.trial_id,
                    workspace_id=owner.workspace_id,
                    origin_generation=scope.generation,
                    logical_id=logical_id,
                    provider_id=provider,
                    kind=lease.kind,
                    state="planned",
                    private_value=store.private.seal("lease:" + lease.handle, encode_lease(lease)),
                )
            )
        await store.append(
            db,
            trial,
            "environment.planned",
            {"session_id": scope.session_id, "resource_count": len(leases)},
        )
        await db.commit()
        return uuid.UUID(scope.session_id)


class SqlResourceAuthority:
    def __init__(self, store, owner, session_id, actor_id, provider_id, *, recovery=False):
        self.store, self.owner = store, owner
        self.session_id, self.actor_id = uuid.UUID(str(session_id)), uuid.UUID(str(actor_id))
        self.provider_id, self.recovery = provider_id, recovery

    @asynccontextmanager
    async def operation(self, request):
        validate("broker_request", request)
        encode_line(request)
        async with database_fence(self.store.engine, self.owner.trial_id) as db:
            trial, now = await self.store.current(db, self.owner)
            session = await db.scalar(
                select(BenchmarkEnvironmentSession)
                .where(
                    BenchmarkEnvironmentSession.id == self.session_id,
                    BenchmarkEnvironmentSession.trial_id == self.owner.trial_id,
                    BenchmarkEnvironmentSession.workspace_id == self.owner.workspace_id,
                    BenchmarkEnvironmentSession.control_id == self.owner.control_id,
                )
                .options(undefer(BenchmarkEnvironmentSession.private_plan))
            )
            if session is None:
                raise failure("FOREIGN_SESSION", "Environment session is unavailable")
            scope = OwnerScope(
                str(trial.workspace_id),
                str(self.owner.trial_id),
                str(session.id),
                session.origin_generation,
            )
            if (
                request["scope"]
                != {
                    "workspace_id": scope.workspace_id,
                    "trial_id": scope.trial_id,
                    "generation": scope.generation,
                }
                or request["provider"] != self.provider_id
            ):
                raise failure("STALE_GENERATION", "Resource request is outside its bound scope")
            reservation = await db.get(BenchmarkHostReservation, self.store.host_id)
            if reservation is None or (
                reservation.trial_id,
                reservation.generation,
                reservation.controller_id,
            ) != (self.owner.trial_id, self.owner.generation, self.owner.controller_id):
                raise failure(
                    "NO_RESERVATION",
                    "Resource operation has no current host reservation",
                    "preparation",
                )
            if reservation.budget.get("stage", "full") == "planning" and request[
                "operation"
            ] not in {"allocate", "inspect", "stop", "release"}:
                raise failure(
                    "PLANNING_WORKER_ONLY",
                    "Native execution and exports require complete group admission",
                    "preparation",
                )
            plan = self.store.private.open("session:" + str(session.id), session.private_plan)
            policy = plan["actors"].get(str(self.actor_id))
            if self.recovery:
                if trial.phase != "cleaning" or request["operation"] not in {
                    "inspect",
                    "stop",
                    "export",
                    "release",
                }:
                    raise failure(
                        "RECOVERY_ONLY",
                        "Recovery may only inspect, capture and release old resources",
                    )
                policy = {
                    "resources": list(plan["resources"]),
                    "private_input_refs": [],
                    "max_exec_seconds": 0,
                }
            else:
                if scope.generation != self.owner.generation:
                    raise failure(
                        "STALE_GENERATION", "Old resource generation is restricted to host recovery"
                    )
                if (
                    policy is None
                    or trial.phase not in policy["phases"]
                    or self.provider_id not in policy["provider_ids"]
                    or request["operation"] not in policy["operations"]
                ):
                    raise failure(
                        "ACTOR_FORBIDDEN", "Actor lacks permission for this resource operation"
                    )
                if (trial.cancel_requested_at is not None or trial.deadline_at <= now) and request[
                    "operation"
                ] not in {"inspect", "stop", "export", "release"}:
                    raise failure("TRIAL_STOPPING", "Trial is stopping", "execution")
            digest = identity(request)
            operation = await db.scalar(
                select(BenchmarkOperation)
                .where(
                    BenchmarkOperation.trial_id == self.owner.trial_id,
                    BenchmarkOperation.control_id == self.owner.control_id,
                    BenchmarkOperation.generation == self.owner.generation,
                    BenchmarkOperation.actor_id == self.actor_id,
                    BenchmarkOperation.request_id == request["id"],
                )
                .options(undefer(BenchmarkOperation.private_result))
            )
            replay = None
            if operation is not None:
                if operation.content_hash != digest or operation.session_id != session.id:
                    raise failure(
                        "COMMAND_CONFLICT", "Resource request identity has different content"
                    )
                if operation.state == "complete":
                    replay = self.store.private.open(
                        "operation-result:" + str(operation.id), operation.private_result
                    )["result"]
                elif operation.state == "failed":
                    raise failure(
                        operation.error_code,
                        "Resource request was previously rejected",
                        operation.error_kind,
                    )
                else:
                    raise failure(
                        "RESOURCE_UNCERTAIN",
                        "Resource operation requires reconciliation",
                        "uncertain",
                    )
            else:
                operation = BenchmarkOperation(
                    id=uuid.uuid4(),
                    trial_id=self.owner.trial_id,
                    session_id=session.id,
                    control_id=self.owner.control_id,
                    generation=self.owner.generation,
                    actor_id=self.actor_id,
                    request_id=request["id"],
                    content_hash=digest,
                    state="started",
                )
                operation.private_request = self.store.private.seal(
                    "operation-request:" + str(operation.id), request
                )
                db.add(operation)
                await self.store.append(
                    db,
                    trial,
                    "resource.requested",
                    {"operation_id": str(operation.id), "operation": request["operation"]},
                )
                await db.commit()  # Still holding the session fence.
            call = SqlResourceCall(
                self, db, trial, session, plan, policy, request, operation, scope, now, replay
            )
            operation_id = operation.id
            try:
                yield call
                if not call.completed:
                    raise failure(
                        "RESOURCE_UNCERTAIN",
                        "Resource operation did not produce a durable receipt",
                        "uncertain",
                    )
            except BaseException as error:
                if replay is None:

                    async def record_failure(cause):
                        await db.rollback()
                        stored = await db.get(
                            BenchmarkOperation, operation_id, populate_existing=True
                        )
                        if stored.state != "started":
                            return
                        uncertain = (
                            not isinstance(cause, ProtocolError)
                            or cause.kind == "uncertain"
                            or cause.code in {"STALE_GENERATION", "OWNER_EXPIRED", "TRIAL_FINISHED"}
                        )
                        finished_at = await db.scalar(select(func.clock_timestamp()))
                        stored.state = "uncertain" if uncertain else "failed"
                        stored.error_code = (
                            cause.code if isinstance(cause, ProtocolError) else "RESOURCE_UNCERTAIN"
                        )
                        stored.error_kind = "uncertain" if uncertain else cause.kind
                        stored.finished_at = finished_at
                        current = await db.get(
                            type(trial), self.store.control_key(self.owner), populate_existing=True
                        )
                        if (
                            (current.generation, current.controller_id)
                            == (
                                self.owner.generation,
                                self.owner.controller_id,
                            )
                            and current.controller_expires_at > finished_at
                            and current.phase != "finished"
                        ):
                            await self.store.append(
                                db,
                                current,
                                "resource." + stored.state,
                                {"operation_id": str(stored.id), "error_code": stored.error_code},
                            )
                        await db.commit()

                    task = asyncio.create_task(record_failure(error))
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                    except Exception:
                        # A persisted 'started' intent is uncertain after a DB
                        # disconnect; absence of a receipt never permits retry.
                        pass
                raise


class SqlResourceCall:
    def __init__(
        self, authority, db, trial, session, plan, policy, request, operation, scope, now, replay
    ):
        self.authority, self.db, self.trial, self.session = authority, db, trial, session
        self.plan, self.policy, self.request, self.operation = plan, policy, request, operation
        self.scope, self.now, self.replay = scope, now, replay
        self.completed = replay is not None
        self.expected = {}
        self.expected_dependencies = {}
        self.artifact_grant = None

    @asynccontextmanager
    async def observation(self):
        """Non-mutating native reads only; re-fence and verify before any write."""
        if self.request["operation"] != "exec":
            raise failure(
                "INVALID_OBSERVATION", "Only native execution waits may suspend the fence"
            )
        suspend, resume = self.db.info["benchmark_observation_fence"]
        try:
            _, cancelled = await critical(suspend())
            if cancelled:
                raise asyncio.CancelledError
            yield
        finally:

            async def check():
                await resume()
                trial, now = await self.authority.store.current(self.db, self.authority.owner)
                if trial.phase == "finished":
                    raise failure(
                        "TRIAL_FINISHED", "Finished trials cannot accept late native observations"
                    )
                return trial, now

            (self.trial, self.now), cancelled = await critical(check())
            self.operation = await self.db.get(
                BenchmarkOperation, self.operation.id, populate_existing=True
            )
            if cancelled:
                raise asyncio.CancelledError

    async def _row(self, handle):
        try:
            handle = uuid.UUID(handle)
        except ValueError:
            raise failure("FOREIGN_HANDLE", "Resource handle is unavailable") from None
        row = await self.db.scalar(
            select(BenchmarkResourceLease)
            .where(
                BenchmarkResourceLease.id == handle,
                BenchmarkResourceLease.session_id == self.session.id,
                BenchmarkResourceLease.trial_id == self.authority.owner.trial_id,
                BenchmarkResourceLease.workspace_id == self.trial.workspace_id,
                BenchmarkResourceLease.origin_generation == self.scope.generation,
                BenchmarkResourceLease.provider_id == self.authority.provider_id,
            )
            .options(undefer(BenchmarkResourceLease.private_value))
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise failure("FOREIGN_HANDLE", "Resource handle is unavailable")
        return row

    def _decode(self, row):
        lease = decode_lease(
            self.authority.store.private.open("lease:" + str(row.id), row.private_value)
        )
        deadline = None if lease.deadline_at is None else datetime.fromisoformat(lease.deadline_at)
        if (
            lease.scope != self.scope
            or lease.state != row.state
            or lease.kind != row.kind
            or lease.handle != str(row.id)
            or lease.intent.resource_id != lease.handle
            or deadline != row.deadline_at
        ):
            raise failure("CORRUPT_LEASE", "Resource lease failed its stored scope checks")
        return lease

    async def _allowed(self, row, lease):
        logical_id = row.logical_id
        if lease.kind == "exec":
            parent = await self._row(lease.intent.container.resource_id)
            logical_id = parent.logical_id
        if logical_id not in self.policy["resources"]:
            raise failure("ACTOR_FORBIDDEN", "Resource is outside this actor's grant")

    async def allocation(self, resource_id):
        if resource_id not in self.policy["resources"] or resource_id not in self.plan["resources"]:
            raise failure(
                "UNREGISTERED_RESOURCE", "Resource is outside this actor's installed plan"
            )
        selected = self.plan["resources"][resource_id]
        row = await self._row(selected["lease"]["handle"])
        lease = self._decode(row)
        if lease.state == "planned":
            for dependency in lease.dependencies:
                linked = self._decode(await self._row(dependency))
                if linked.state != "allocated" or linked.native is None:
                    raise failure(
                        "RESOURCE_NOT_READY", "Resource dependency is not allocated", "preparation"
                    )
        if lease.state == "planned" and isinstance(lease.intent, ContainerSpec):
            spec = lease.intent
            for field, target in self.plan["links"].get(resource_id, {}).items():
                if field not in {"network", "namespace"} or target not in self.plan["resources"]:
                    raise failure("INVALID_PLAN", "Resource dependency is invalid")
                linked = self._decode(
                    await self._row(self.plan["resources"][target]["lease"]["handle"])
                )
                if linked.state != "allocated" or linked.native is None:
                    raise failure(
                        "RESOURCE_NOT_READY", "Resource dependency is not allocated", "preparation"
                    )
                spec = replace(spec, **{field: linked.native})
            lease = replace(lease, intent=spec)
        self.expected[lease.handle] = lease.intent
        self.expected_dependencies[lease.handle] = lease.dependencies
        return lease

    async def resource(self, handle):
        row = await self._row(handle)
        lease = self._decode(row)
        await self._allowed(row, lease)
        self.expected[lease.handle] = lease.intent
        self.expected_dependencies[lease.handle] = lease.dependencies
        return lease

    async def execution(self, container, payload):
        refs = payload.get("private_input_refs", {})
        if any(
            ref not in self.policy["private_input_refs"] or ref not in self.plan["private_inputs"]
            for ref in refs.values()
        ):
            raise failure("PRIVATE_INPUT_UNAVAILABLE", "Private input is outside the actor grant")
        now = await self.db.scalar(select(func.clock_timestamp()))
        maximum = min(
            self.policy["max_exec_seconds"], (self.trial.deadline_at - now).total_seconds()
        )
        seconds = payload.get("timeout_seconds", maximum)
        if not 0 < seconds <= maximum:
            raise failure("LIMIT_EXCEEDED", "Native deadline exceeds the saved grant")
        handle = str(uuid.uuid5(self.operation.id, "exec"))
        spec = ExecSpec(
            container.native,
            handle,
            tuple(payload["argv"]),
            workdir=payload.get("cwd"),
            user=payload.get("user"),
            environment=payload.get("environment", {}),
            private_inputs={
                name: unbinary(self.plan["private_inputs"][ref]) for name, ref in refs.items()
            },
        )
        lease = ResourceLease(handle, self.scope, "exec", spec)
        self.expected[handle] = spec
        self.expected_dependencies[handle] = lease.dependencies
        return ExecutionGrant(lease, payload.get("mode", "wait"), seconds)

    async def output(self, container, output_id):
        row = await self._row(container.handle)
        grant = self.plan["outputs"].get(row.logical_id, {}).get(output_id)
        if (
            grant is None
            or str(self.authority.actor_id) not in grant["actors"]
            and not self.authority.recovery
        ):
            raise failure("OUTPUT_UNAVAILABLE", "Output is outside this actor's saved grant")
        value = ExportGrant(**grant["grant"])
        if self.authority.recovery:
            value = replace(
                value,
                visibility="private",
                artifact_id="recovery." + str(uuid.uuid5(self.session.id, output_id)),
            )
        self.artifact_grant = value
        return value

    async def publish_artifact(self, artifact, locator):
        from app.benchmarks.evidence import publish_artifact, verify_origin

        if self.request["operation"] != "export" or self.artifact_grant is None:
            raise failure(
                "OUTPUT_UNAVAILABLE", "Only granted exports can publish evidence", "recording"
            )
        if artifact is None:
            await verify_origin(
                self.db, self.authority.owner, self.scope, store=self.authority.store
            )
            await self.db.commit()  # Keep heartbeat/cancel row locks free during object I/O.
            return None
        validate("artifact", artifact)
        grant = self.artifact_grant
        if (artifact["id"], artifact["visibility"]) != (
            grant.artifact_id,
            grant.visibility,
        ) or artifact["size_bytes"] > grant.max_archive_bytes:
            raise failure(
                "OUTPUT_UNAVAILABLE", "Evidence receipt differs from its output grant", "recording"
            )
        return await publish_artifact(
            self.db, self.authority.store, self.authority.owner, self.scope, artifact, locator
        )

    async def deadline_expired(self, lease):
        row = await self._row(lease.handle)
        current = self._decode(row)
        if current != lease:
            raise failure("LEASE_CHANGED", "Resource changed before its deadline check")
        now = await self.db.scalar(select(func.clock_timestamp()))
        return row.deadline_at is not None and row.deadline_at <= now

    async def save(self, lease):
        if (
            lease.scope != self.scope
            or lease.handle not in self.expected
            or lease.intent != self.expected[lease.handle]
            or lease.dependencies != self.expected_dependencies[lease.handle]
        ):
            raise failure("INVALID_INTENT", "Runtime intent differs from its host grant")
        row = await self.db.get(BenchmarkResourceLease, uuid.UUID(lease.handle))
        if row is None:
            if lease.kind != "exec":
                raise failure("UNREGISTERED_RESOURCE", "New resources require an installed plan")
            row = BenchmarkResourceLease(
                id=uuid.UUID(lease.handle),
                trial_id=self.authority.owner.trial_id,
                session_id=self.session.id,
                workspace_id=self.trial.workspace_id,
                origin_generation=self.scope.generation,
                logical_id="exec:" + lease.handle,
                provider_id=self.authority.provider_id,
                kind="exec",
            )
            self.db.add(row)
        elif row.session_id != self.session.id or row.provider_id != self.authority.provider_id:
            raise failure("FOREIGN_HANDLE", "Stored lease belongs to another session or provider")
        row.state = lease.state
        row.deadline_at = (
            None if lease.deadline_at is None else datetime.fromisoformat(lease.deadline_at)
        )
        row.private_value = self.authority.store.private.seal(
            "lease:" + lease.handle, encode_lease(lease)
        )
        await self.authority.store.append(
            self.db,
            self.trial,
            "resource.saved",
            {"handle": lease.handle, "state": lease.state, "kind": lease.kind},
        )
        await self.db.commit()

    async def complete(self, result):
        # Query before making the row dirty: SQLAlchemy otherwise autoflushes a
        # terminal state without its timestamp, violating immutable completion.
        finished_at = await self.db.scalar(select(func.clock_timestamp()))
        self.operation.private_result = self.authority.store.private.seal(
            "operation-result:" + str(self.operation.id), {"result": result}
        )
        self.operation.state = "complete"
        self.operation.finished_at = finished_at
        await self.authority.store.append(
            self.db, self.trial, "resource.completed", {"operation_id": str(self.operation.id)}
        )
        await self.db.commit()
        self.completed = True
