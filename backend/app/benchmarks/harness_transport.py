"""Start a registered Harness bundle in the benchmark's selected task rootfs."""

import asyncio
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import validate

from app.runtime.bundles import validate_bundle
from app.runtime.managed_spec import ContainerSpec, ManagedRuntimeError


class QuiescentHarnessProcess:
    """Main-process exit is insufficient proof that agent descendants stopped."""

    def __init__(self, process, stop):
        self.process, self.stop, self.settled = process, stop, None

    def __getattr__(self, name):
        return getattr(self.process, name)

    async def _wait(self):
        code = await self.process.wait()
        await self.stop()
        return code

    async def wait(self):
        if self.settled is None:
            self.settled = asyncio.create_task(self._wait())
            self.settled.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(self.settled)


class RootlessHarnessTransport:
    """One host-only attach; the worker supplies neither an image nor an argv."""

    def __init__(self, host, plan, prepared, bundle, manifest, *, credential_ref=None):
        validate("environment_plan", plan)
        validate("prepared", prepared)
        agent, planned = prepared.get("agent"), plan.get("agent")
        if (
            not agent
            or not planned
            or agent["mode"] != "in_environment"
            or planned["mode"] != "in_environment"
        ):
            raise ManagedRuntimeError("In-environment Harness transport requires a task target")
        if host.purpose != "execution" or host.environment is None:
            raise ManagedRuntimeError(
                "Harness transport requires an installed execution environment"
            )
        selected = host.environment.leases.get(planned.get("environment_handle"))
        if selected is None or selected[0] != host.assets.provider_id:
            raise ManagedRuntimeError("Harness target is outside the installed provider plan")
        lease = selected[1]
        if (
            lease.kind != "container"
            or not isinstance(lease.intent, ContainerSpec)
            or lease.intent.role != "environment"
            or agent.get("environment_handle") != lease.handle
        ):
            raise ManagedRuntimeError("Harness target differs from the declared task container")
        mounts = [mount for mount in lease.intent.mounts if mount.target == "/evalhive-runtime"]
        if len(mounts) != 1 or not mounts[0].read_only or mounts[0].source != bundle.host_path:
            raise ManagedRuntimeError(
                "Harness requires its exact registered read-only runtime bundle"
            )
        if (bundle.connector_id, bundle.connector_version) != (manifest["id"], manifest["version"]):
            raise ManagedRuntimeError("Harness bundle identity differs from the frozen connector")
        if credential_ref is not None and credential_ref not in host.host_private_inputs:
            raise ManagedRuntimeError("Harness credentials require a host-only private input grant")
        self.host, self.bundle, self.manifest = host, bundle, deepcopy(manifest)
        self.handle, self.cwd, self.credential_ref = lease.handle, agent["cwd"], credential_ref
        self.used = False

    async def __call__(self):
        if self.used:
            raise ManagedRuntimeError(
                "Harness transport cannot reattach or restart an uncertain process"
            )
        self.used = True
        receipt, _ = await asyncio.to_thread(
            validate_bundle,
            Path(self.bundle.local_path),
            self.manifest,
            expected_hash=self.bundle.fingerprint,
        )
        if tuple(receipt["entrypoint"]) != self.bundle.entrypoint:
            raise ManagedRuntimeError("Harness runtime entrypoint changed after registration")
        payload = {"argv": list(self.bundle.entrypoint), "cwd": self.cwd, "mode": "attach"}
        if self.credential_ref is not None:
            payload["private_input_refs"] = {"EVALHIVE_CREDENTIALS_FILE": self.credential_ref}
        attached = await self.host.host_broker.handle(
            self.host.request(
                self.host.scope,
                "exec",
                handle=self.handle,
                payload=payload,
                request_id="harness.exec",
            )
        )
        process = await self.host.host_broker.open_transport(
            self.host.request(
                self.host.scope,
                "inspect",
                handle=attached["handle"],
                request_id="harness.attach",
            )
        )

        async def stop():
            result = await self.host.host_broker.handle(
                self.host.request(
                    self.host.scope, "stop", handle=attached["handle"], request_id="harness.quiesce"
                )
            )
            if result.get("handle") != attached["handle"] or result.get("stopped") is not True:
                raise ManagedRuntimeError("Harness descendant shutdown could not be confirmed")

        return QuiescentHarnessProcess(process, stop)
