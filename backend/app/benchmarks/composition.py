"""Compose installed benchmarks, owned environments and Harness SDK execution.

Inputs are resolved by the server before construction. Native benchmark packages
remain in their worker images; this module never imports a benchmark adapter.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark import validate
from evalhive_harness import validate_run

from app.benchmarks.agent_calls import AgentCallStore
from app.benchmarks.agent_gateway import AgentGateway
from app.benchmarks.deadlines import DeadlineService
from app.benchmarks.driver import TrialDriver
from app.benchmarks.harness_execution import HarnessExecution
from app.benchmarks.harness_transport import RootlessHarnessTransport
from app.benchmarks.model_channel import AgentModelChannel, model_connection
from app.benchmarks.recovery import RecoveryService
from app.benchmarks.regrade_driver import RegradeDriver
from app.benchmarks.retained_installation import load_retained_installation
from app.benchmarks.worker_host import RootlessWorkerHost
from app.runtime.docker_provider import RootlessDockerProvider
from app.runtime.managed_spec import ContainerSpec, ManagedRuntimeError
from app.runtime.reconcile import RootlessReconciler


class _RegisteredHarness(HarnessExecution):
    def __init__(self, *args, host, model_channel=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.host = host
        self.model_channel = model_channel

    def bind_environment(self, compiled, plan):
        if self.model_channel is None:
            return compiled
        compiled, descriptor = self.model_channel.bind(compiled, plan, self.host.assets.provider_id)
        self.spec["metadata"] = {**self.spec.get("metadata", {}), "model_gateway": descriptor}
        validate_run(self.manifest, self.spec)
        return compiled

    async def start_environment(self):
        if self.model_channel is not None:
            await self.model_channel.start()

    async def detach_environment(self):
        if self.model_channel is not None:
            await self.model_channel.close()

    def configure_access(self, prepared):
        agent = prepared.get("agent", {})
        planned = self.host.plan.get("agent", {})
        target = self.host.environment.leases.get(planned.get("environment_handle"))
        if (
            agent.get("mode") != "in_environment"
            or planned.get("mode") != "in_environment"
            or target is None
            or target[0] != self.host.assets.provider_id
        ):
            raise ManagedRuntimeError("Agent requires its registered task environment")
        lease = target[1]
        if (
            lease.kind != "container"
            or not isinstance(lease.intent, ContainerSpec)
            or lease.intent.role != "environment"
            or agent.get("environment_handle") != lease.handle
        ):
            raise ManagedRuntimeError("Prepared agent target differs from its installed plan")
        super().configure_access(prepared)
        self.spec["metadata"] = {
            **self.spec.get("metadata", {}),
            "image_id": lease.intent.image_id,
        }
        self.spec["limits"] = {
            **self.spec.get("limits", {}),
            "memory_mb": lease.intent.quota.memory_mb,
            "cpu_cores": lease.intent.quota.cpu_cores,
        }
        validate_run(self.manifest, self.spec)


class RootlessCoordinator:
    """Build one controller from administrator-installed declarations and inputs.

    Construction does not allocate resources or start an agent. The resulting
    driver's admission, ownership fences and journal remain authoritative.
    """

    def __init__(self, runtime, provider_manifest, evidence, *, limits=None, runner_policy=None):
        validate("manifest", provider_manifest)
        if provider_manifest["kind"] != "environment_provider":
            raise ManagedRuntimeError("Coordinator requires an environment provider declaration")
        self.runtime, self.evidence = runtime, evidence
        self.provider = deepcopy(provider_manifest)
        self.limits, self.runner_policy = limits, runner_policy

    def _providers(self, store, owner):
        if self.evidence.store is not store:
            raise ManagedRuntimeError("Evidence belongs to another controller store")
        return {
            self.provider["id"]: lambda authority: RootlessDockerProvider(
                self.runtime, authority, self.provider, artifacts=self.evidence.sink(owner)
            )
        }

    def _driver(self, host, factories, driver_class):
        if (
            host.assets.runtime is not self.runtime
            or host.assets.provider_id != self.provider["id"]
        ):
            raise ManagedRuntimeError("Installed assets require another runtime provider")
        reconciler = lambda authority: RootlessReconciler(  # noqa: E731
            self.runtime, authority, self.provider["id"]
        )
        return driver_class(
            host,
            RecoveryService(host.store, host.host_actor, {self.provider["id"]: reconciler}),
            capture_providers=factories,
            capture_validator=self.evidence.validate_capture,
            deadlines=(
                DeadlineService(host.store, self.provider["id"], host.host_actor, reconciler),
            ),
            limits=self.limits,
        )

    def execution(
        self,
        store,
        owner,
        manifest,
        assets,
        *,
        harness_manifest,
        harness_spec,
        bundle,
        credentials,
        spool_root,
        profile_id=None,
        model_gateway=False,
    ):
        if owner.control_id is not None:
            raise ManagedRuntimeError("Agent execution requires an execution controller")
        if assets.runtime is not self.runtime or assets.provider_id != self.provider["id"]:
            raise ManagedRuntimeError("Installed assets require another runtime provider")
        validate_run(harness_manifest, harness_spec)
        if type(model_gateway) is not bool or "model_gateway" in harness_spec.get("metadata", {}):
            raise ManagedRuntimeError("Model transport must be selected by the coordinator")
        if model_gateway and harness_manifest["capabilities"].get("model_gateway") not in {"native", "emulated"}:
            raise ManagedRuntimeError("Harness connector cannot use an owned model channel")
        if (bundle.connector_id, bundle.connector_version) != (
            harness_manifest["id"],
            harness_manifest["version"],
        ):
            raise ManagedRuntimeError("Harness runtime differs from the selected connector")
        if not isinstance(credentials, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in credentials.items()
        ):
            raise ManagedRuntimeError("Harness credentials require a private string map")
        private_values = tuple(value for value in credentials.values() if value)
        route, private_credentials = model_connection(harness_spec, credentials) if model_gateway else (None, credentials)
        encoded = json.dumps(private_credentials, ensure_ascii=False).encode()
        if len(encoded) > 1024**2:
            raise ManagedRuntimeError("Harness credential input exceeds its byte limit")
        spool_root = Path(spool_root)
        local_data = Path(self.runtime.policy.data.local)
        if (
            not spool_root.is_absolute()
            or not spool_root.is_relative_to(local_data)
            or spool_root.resolve() != spool_root
        ):
            raise ManagedRuntimeError("Harness spool requires a contained controller data path")
        spec, harness = deepcopy(harness_spec), deepcopy(harness_manifest)
        spec["credential_refs"] = {
            **spec.get("credential_refs", {}),
            **{name: "runtime-injected" for name in credentials},
        }
        spec["metadata"] = {
            **spec.get("metadata", {}),
            "runtime_bundle_image_id": bundle.image_id,
            "runtime_bundle_fingerprint": bundle.fingerprint,
        }
        if route is not None:
            private_values += (route.token,)
        factories = self._providers(store, owner)

        def agent_factory(host):
            calls = AgentCallStore(store, owner, host.worker_actor)

            async def transport():
                return await RootlessHarnessTransport(
                    host,
                    host.plan,
                    host.prepared,
                    bundle,
                    harness,
                    credential_ref="agent.credentials",
                )()

            return AgentGateway(
                calls,
                _RegisteredHarness(
                    calls,
                    harness,
                    spec,
                    transport,
                    spool_root / str(calls.session_id) / "events.sqlite",
                    host=host,
                    model_channel=AgentModelChannel(calls, self.runtime, route) if route else None,
                    profile_id=profile_id,
                    credential_ref="agent.credentials",
                    private_values=private_values,
                    policy=self.runner_policy,
                ),
            )

        host = RootlessWorkerHost(
            store,
            owner,
            manifest,
            assets,
            factories,
            retained_evidence=self.evidence,
            agent_factory=agent_factory,
            host_private_inputs={"agent.credentials": encoded},
            host_auxiliary_disk_mb=1 if route else 0,
        )
        return self._driver(host, factories, TrialDriver)

    async def evaluation(self, store, owner):
        if owner.control_id is None:
            raise ManagedRuntimeError("Regrading requires an independent evaluation controller")
        if self.evidence.store is not store or not callable(getattr(self.evidence, "open_verified", None)):
            raise ManagedRuntimeError("Regrading requires its scoped verified evidence reader")
        frozen = await load_retained_installation(store, owner, self.runtime)
        factories = self._providers(store, owner)
        host = RootlessWorkerHost(
            store,
            owner,
            frozen.manifest,
            frozen.assets,
            factories,
            retained_evidence=self.evidence,
            worker_image_pin=frozen.worker_image_id,
        )
        return self._driver(host, factories, RegradeDriver)
