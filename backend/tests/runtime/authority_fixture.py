"""TEST ONLY: explicit fixture authority/sink, never a production fallback.

It checks provider ordering and the SDK boundary. It makes no claim of durable
recovery; the coordinator's PostgreSQL implementation is tested separately.
"""

import asyncio
import copy
import hashlib
import uuid
from contextlib import asynccontextmanager

from app.runtime.broker import ExecutionGrant, ResourceLease
from app.runtime.managed_spec import ExecSpec
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity


def manifest():
    return {
        "protocol_version": "1.0",
        "kind": "environment_provider",
        "id": "fixture.rootless",
        "version": "1.0",
        "display_name": "Rootless test fixture",
        "entrypoint": ["fixture-only"],
        "image": "fixture-only",
        "config_schema": {"type": "object"},
        "capabilities": {
            name: "native" for name in ["allocate", "exec", "inspect", "stop", "export", "release"]
        },
        "requirements": {},
    }


class FixtureAuthority:
    def __init__(self, scope, plans, *, private_inputs=None, outputs=None):
        self.scope, self.plans = scope, plans
        self.resources = {value.handle: value for value in plans.values()}
        self.private_inputs, self.outputs = private_inputs or {}, outputs or {}
        self.receipts, self.journal = {}, []
        self.lock = asyncio.Lock()
        self.reserved = True
        self.max_timeout = 30

    @asynccontextmanager
    async def operation(self, request):
        async with self.lock:
            if not self.reserved:
                raise ProtocolError("NO_RESERVATION", "Host reservation is missing", "preparation")
            if request["scope"] != {
                "workspace_id": self.scope.workspace_id,
                "trial_id": self.scope.trial_id,
                "generation": self.scope.generation,
            }:
                raise ProtocolError(
                    "STALE_GENERATION", "Fixture generation is stale", "configuration"
                )
            signature = identity(request)
            previous = self.receipts.get(request["id"])
            if previous and previous[0] != signature:
                raise ProtocolError("COMMAND_CONFLICT", "Receipt content changed", "configuration")
            if previous and previous[2] is not None:
                raise previous[2]
            call = FixtureCall(self, request, previous[1] if previous else None)
            try:
                yield call
            except ProtocolError as error:
                self.receipts[request["id"]] = (signature, None, error)
                self.journal.append(("failure", request["id"], error.code))
                raise


class FixtureCall:
    def __init__(self, owner, request, replay):
        self.owner, self.request, self.replay = owner, request, replay
        self.scope = owner.scope

    async def allocation(self, resource_id):
        if resource_id not in self.owner.plans:
            raise ProtocolError(
                "UNREGISTERED_RESOURCE", "Plan resource is not installed", "configuration"
            )
        handle = self.owner.plans[resource_id].handle
        return self.owner.resources[handle]

    async def resource(self, handle):
        if handle not in self.owner.resources:
            raise ProtocolError("FOREIGN_HANDLE", "Fixture handle is unavailable", "configuration")
        return self.owner.resources[handle]

    async def execution(self, container, payload):
        seconds = payload.get("timeout_seconds", self.owner.max_timeout)
        if seconds > self.owner.max_timeout:
            raise ProtocolError(
                "LIMIT_EXCEEDED", "Requested deadline exceeds its grant", "configuration"
            )
        references = payload.get("private_input_refs", {})
        if any(value not in self.owner.private_inputs for value in references.values()):
            raise ProtocolError(
                "PRIVATE_INPUT_UNAVAILABLE", "Private input ref is unavailable", "configuration"
            )
        handle = str(uuid.uuid5(uuid.UUID(self.scope.trial_id), self.request["id"]))
        spec = ExecSpec(
            container.native,
            handle,
            tuple(payload["argv"]),
            workdir=payload.get("cwd"),
            user=payload.get("user"),
            environment=payload.get("environment", {}),
            private_inputs={
                name: self.owner.private_inputs[ref] for name, ref in references.items()
            },
        )
        lease = ResourceLease(handle, self.scope, "exec", spec)
        return ExecutionGrant(lease, payload.get("mode", "wait"), seconds)

    async def output(self, container, output_id):
        value = self.owner.outputs.get((container.handle, output_id))
        if value is None:
            raise ProtocolError(
                "OUTPUT_UNAVAILABLE", "Output is outside the saved plan", "configuration"
            )
        return value

    async def save(self, lease):
        self.owner.resources[lease.handle] = lease
        self.owner.journal.append(("save", lease.handle, lease.state))

    async def complete(self, result):
        self.owner.receipts[self.request["id"]] = (
            identity(self.request),
            copy.deepcopy(result),
            None,
        )
        self.owner.journal.append(("receipt", self.request["id"], None))


class FixtureArtifacts:
    def __init__(self):
        self.values = {}

    async def save(self, scope, grant, chunks):
        data = bytearray()
        async for chunk in chunks:
            data.extend(chunk)
        self.values[grant.artifact_id] = bytes(data)
        return {
            "id": grant.artifact_id,
            "uri": f"fixture://{scope.workspace_id}/{scope.trial_id}/{grant.artifact_id}",
            "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "visibility": grant.visibility,
            "media_type": "application/x-tar",
        }
