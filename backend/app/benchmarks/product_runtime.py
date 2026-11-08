"""Compose installed metadata with the existing owned runtime for product jobs."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from copy import deepcopy
from pathlib import Path, PurePosixPath

import docker
import urllib3
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession

from app.benchmarks.catalog import CatalogError
from app.benchmarks.composition import RootlessCoordinator
from app.benchmarks.driver import DriverLimits
from app.benchmarks.evidence import EvidenceStore
from app.benchmarks.host_contract import group_policy, provider_manifest
from app.benchmarks.installations import resolve_package
from app.benchmarks.launch_inputs import refresh_authorization
from app.benchmarks.model_calls import ModelBinding
from app.benchmarks.native_aggregation import NativeAggregation, behavior_digest
from app.benchmarks.trials import failure
from app.config import get_settings
from app.models.benchmark import BenchmarkDatasetRevision
from app.models.benchmark_trial import BenchmarkTrial
from app.runtime.bundles import install_bundle
from app.runtime.managed_docker import RootlessDockerRuntime
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping, RuntimePolicy
from app.runtime.model_gateway import ModelRoute
from app.runtime.registered_assets import RegisteredFile
from app.runtime.registered_files import load_registered_assets


def auxiliary_bindings(values, *, roles=None):
    selected = set(values) if roles is None else set(roles)
    if not selected <= set(values):
        raise failure("MODEL_UNAVAILABLE", "Frozen evaluator model connections are unavailable")
    return {
        role: ModelBinding(
            values[role]["credential_ref"],
            ModelRoute(
                values[role]["name"],
                values[role]["endpoint"],
                values[role]["api_key"],
                secrets.token_urlsafe(32),
            ),
            tuple(values[role]["phases"]),
        )
        for role in selected
    }


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise failure(
            "RUNTIME_UNAVAILABLE", "Execution storage requires a contained deployment path"
        )
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def evidence_store(store, *, settings=None):
    settings = settings or get_settings()
    spool = private_directory(Path(settings.data_dir) / "benchmark-evidence-spool")
    client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
        http_client=urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=30), retries=False),
    )
    return EvidenceStore(store, client, bucket=settings.benchmark_evidence_bucket, spool_dir=spool)


class ProductRuntime:
    def __init__(self, store, runtime, evidence, settings):
        self.store, self.runtime, self.evidence, self.settings = store, runtime, evidence, settings
        self.coordinator = RootlessCoordinator(
            runtime,
            provider_manifest(),
            evidence,
            group_policy=group_policy(settings),
            resource_finalizers=settings.benchmark_resource_finalizers,
            limits=DriverLimits(stop_seconds=settings.benchmark_stop_grace_seconds),
        )

    @classmethod
    def installed(cls, store, *, settings=None, recovery=False):
        settings = settings or get_settings()
        if not settings.benchmark_runtime_enabled and not recovery:
            raise failure(
                "RUNTIME_UNAVAILABLE", "Benchmark execution is not enabled on this server"
            )
        readonly = ()
        if settings.host_benchmark_manifest_dir:
            readonly = (
                PathMapping(settings.host_benchmark_manifest_dir, settings.benchmark_manifest_dir),
            )
        elif not recovery:
            raise failure(
                "RUNTIME_UNAVAILABLE", "Installed benchmark host paths are not configured"
            )
        data = private_directory(settings.data_dir)
        bundle_cache = private_directory(data / "benchmark-runtime-bundles")
        # Runtime bundles are installed code, separate from mutable attempt data.
        # Only this cache subtree may supply retained read-only runtime grants.
        readonly = (
            *readonly,
            PathMapping(
                str(PurePosixPath(settings.host_data_dir) / "benchmark-runtime-bundles"),
                str(bundle_cache),
            ),
        )
        policy = RuntimePolicy(
            PathMapping(settings.host_data_dir, settings.data_dir),
            readonly,
            max_memory_mb=settings.runtime_max_memory_mb,
            max_cpu_cores=settings.runtime_max_cpu_quota / 100000,
            allowed_capabilities=tuple(settings.benchmark_linux_capabilities),
        )
        private_directory(policy.data.local)
        runtime = RootlessDockerRuntime(docker.from_env(), policy)
        try:
            return cls(store, runtime, evidence_store(store, settings=settings), settings)
        except Exception:
            runtime.client.close()
            raise

    async def storage_ready(self):
        try:
            available = await asyncio.to_thread(
                self.evidence.client.bucket_exists, self.evidence.bucket
            )
        except Exception:
            raise failure(
                "STORAGE_UNAVAILABLE", "Benchmark evidence storage is unavailable"
            ) from None
        if not available:
            raise failure("STORAGE_UNAVAILABLE", "Benchmark evidence bucket is not installed")

    async def execution(self, owner, payload, snapshot):
        if owner.control_id is not None or payload.get("format_version") != "1.0":
            raise failure("INVALID_LAUNCH", "Execution requires its frozen product inputs")
        await self.storage_ready()
        async with AsyncSession(self.store.engine) as db:
            await self.store.current(db, owner)
            payload = await refresh_authorization(db, owner.workspace_id, snapshot, payload)
            dataset = await db.get(
                BenchmarkDatasetRevision, uuid.UUID(snapshot["selection"]["dataset_revision_id"])
            )
            if dataset is None or dataset.workspace_id != owner.workspace_id:
                raise failure(
                    "INSTALLATION_UNAVAILABLE", "Registered native installation is unavailable"
                )
            try:
                package = await asyncio.to_thread(
                    resolve_package, dataset, directory=self.settings.benchmark_manifest_dir
                )
            except CatalogError:
                raise failure(
                    "INSTALLATION_UNAVAILABLE", "Registered benchmark package is unavailable"
                ) from None
        if (
            package.metadata_digest != payload["installation_metadata_digest"]
            or package.manifest != payload["benchmark_manifest"]
        ):
            raise failure(
                "INSTALLATION_CONFLICT",
                "Installed runtime metadata changed after the launch request",
            )
        mapping = next(
            (
                item
                for item in self.runtime.policy.readonly
                if Path(item.local) == Path(self.settings.benchmark_manifest_dir)
            ),
            None,
        )
        if mapping is None:
            raise failure("INSTALLATION_UNAVAILABLE", "Installed benchmark mapping is unavailable")
        relative = package.directory.relative_to(Path(mapping.local))
        package_mapping = PathMapping(
            str(PurePosixPath(mapping.host) / relative.as_posix()), str(package.directory)
        )
        bundle_local = private_directory(
            Path(self.runtime.policy.data.local) / "benchmark-runtime-bundles"
        )
        bundle_mapping = PathMapping(
            str(PurePosixPath(self.runtime.policy.data.host) / "benchmark-runtime-bundles"),
            str(bundle_local),
        )
        try:
            bundle = await asyncio.to_thread(
                install_bundle, self.runtime, payload["harness_manifest"], bundle_mapping
            )
            assets = load_registered_assets(
                package_mapping,
                self.runtime,
                package.manifest,
                bindings={
                    "harness.runtime": RegisteredFile(
                        bundle.host_path, "sha256:" + bundle.fingerprint
                    )
                },
            )
        except ManagedRuntimeError:
            raise failure(
                "INSTALLATION_UNAVAILABLE",
                "Registered runtime assets are unavailable or incompatible",
            ) from None
        spool = private_directory(
            Path(self.runtime.policy.data.local)
            / "benchmark-jobs"
            / str(owner.trial_id)
            / str(owner.generation)
        )
        native_values = payload["benchmark_private_inputs"]
        auxiliary = payload["auxiliary_models"]
        controller = self.coordinator.execution(
            self.store,
            owner,
            package.manifest,
            assets,
            harness_manifest=payload["harness_manifest"],
            harness_spec=payload["harness_spec"],
            bundle=bundle,
            credentials=payload["harness_credentials"],
            spool_root=spool,
            profile_id=uuid.UUID(payload["harness_spec"]["metadata"]["profile_id"]),
            model_gateway=True,
            auxiliary_models=auxiliary_bindings(auxiliary),
            redaction_values=[
                *native_values.values(),
                *(value["api_key"] for value in auxiliary.values()),
            ],
        )
        spec = deepcopy(payload["benchmark_spec"])
        spec["scope"] = {
            "workspace_id": str(owner.workspace_id),
            "trial_id": str(owner.trial_id),
            "generation": owner.generation,
        }
        controller.aggregation = NativeAggregation(
            self.store, owner, package.manifest, behavior=behavior_digest(payload)
        )
        return controller, spec, {name: value.encode() for name, value in native_values.items()}

    async def evaluation(self, owner, payload):
        await self.storage_ready()
        inputs = await self.store.launch_inputs(owner)
        async with AsyncSession(self.store.engine) as db:
            await self.store.current(db, owner)
            trial = await db.get(BenchmarkTrial, owner.trial_id)
            payload = await refresh_authorization(
                db,
                owner.workspace_id,
                trial.snapshot,
                payload,
                evaluation_roles=inputs["spec"]["models"],
            )
        bindings = auxiliary_bindings(payload["auxiliary_models"], roles=inputs["spec"]["models"])
        controller = await self.coordinator.evaluation(self.store, owner, auxiliary_models=bindings)
        controller.aggregation = NativeAggregation(self.store, owner, controller.host.manifest)
        return controller

    def recovery(self, owner):
        return self.coordinator.recovery(self.store, owner)

    async def close(self):
        await asyncio.to_thread(self.runtime.client.close)
