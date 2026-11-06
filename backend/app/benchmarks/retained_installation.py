"""Resolve a fresh rootless worker from immutable source installation records."""

from copy import deepcopy
from dataclasses import dataclass
from functools import partial

from evalhive_benchmark import validate
from evalhive_benchmark.peer import identity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.evidence import _io
from app.benchmarks.trials import failure
from app.models.benchmark_trial import BenchmarkEnvironmentSession
from app.runtime.file_pins import content_proof, installed_path
from app.runtime.lease_codec import decode_lease
from app.runtime.managed_spec import ManagedRuntimeError
from app.runtime.registered_assets import RegisteredFile, RootlessAssets


@dataclass(frozen=True)
class RetainedInstallation:
    manifest: dict
    assets: RootlessAssets
    worker_image_id: str


async def load_retained_installation(store, owner, runtime):
    inputs = await store.launch_inputs(owner)
    snapshots = inputs["environment_sessions"]
    plans = []
    async with AsyncSession(store.engine) as db:
        await store.current(db, owner)
        for saved in snapshots:
            row = await db.scalar(
                select(BenchmarkEnvironmentSession)
                .where(
                    BenchmarkEnvironmentSession.id == saved["id"],
                    BenchmarkEnvironmentSession.workspace_id == owner.workspace_id,
                    BenchmarkEnvironmentSession.trial_id == owner.trial_id,
                )
                .options(undefer(BenchmarkEnvironmentSession.private_plan))
            )
            if (
                row is None
                or row.plan_digest != saved["plan_digest"]
                or row.origin_generation != saved["origin_generation"]
            ):
                raise failure(
                    "INSTALLATION_UNAVAILABLE",
                    "Source installation record is unavailable",
                    "recording",
                )
            plan = store.private.open("session:" + str(row.id), row.private_plan)
            if identity(plan) != row.plan_digest or plan["scope"] != {
                "workspace_id": str(owner.workspace_id),
                "trial_id": str(owner.trial_id),
                "session_id": str(row.id),
                "generation": row.origin_generation,
            }:
                raise failure(
                    "INSTALLATION_CONFLICT", "Source installation proof differs", "recording"
                )
            plans.append(plan)
    workers = [plan for plan in plans if "sdk.worker" in plan["resources"]]
    if len(workers) != 1:
        raise failure(
            "INSTALLATION_UNAVAILABLE",
            "A single source worker installation is required",
            "unsupported",
        )
    worker = workers[0]
    registration = worker["registration"]
    manifest, catalog = registration.get("manifest"), registration.get("catalog")
    if manifest is None or catalog is None or "file_pins" not in registration:
        raise failure(
            "INSTALLATION_UNAVAILABLE",
            "Full source installation metadata was not retained",
            "unsupported",
        )
    validate("manifest", manifest)
    if any(
        manifest.get(key) != inputs["evaluator"].get(key)
        for key in ("id", "version", "image", "protocol_version", "capabilities")
    ):
        raise failure(
            "INSTALLATION_CONFLICT",
            "Source worker differs from the retained evaluator",
            "recording",
        )
    if registration["fingerprint"] != identity(catalog):
        raise failure(
            "INSTALLATION_CONFLICT", "Source asset catalog differs from its digest", "recording"
        )
    if any(plan["registration"].get("catalog") != catalog for plan in plans):
        raise failure(
            "INSTALLATION_CONFLICT", "Source sessions use different installed catalogs", "recording"
        )
    lease = decode_lease(worker["resources"]["sdk.worker"]["lease"])
    if (
        lease.kind != "container"
        or lease.intent.role != "worker"
        or (
            lease.intent.image_id != registration["worker_image_id"]
            or lease.intent.entrypoint != tuple(manifest["entrypoint"])
            or lease.intent.command != ()
        )
    ):
        raise failure(
            "INSTALLATION_CONFLICT",
            "Source worker process differs from its installation",
            "recording",
        )
    used_files = {
        file_id
        for plan in plans
        for file_id in plan["registration"].get("files", {})
        if not file_id.startswith("retained/")
    }
    used_files.update(
        mount["file_id"] for mount in catalog["assets"][catalog["worker_asset"]].get("mounts", [])
    )
    files, observed_files = {}, {}
    for file_id in used_files:
        record = registration["file_pins"].get(file_id)
        if record is None or record["writable"] or record.get("content_proof") is None:
            raise failure(
                "STATIC_INPUT_UNAVAILABLE",
                "A source static input cannot be replayed",
                "unsupported",
            )
        if any(
            plan["registration"].get("files", {}).get(file_id, record["source_digest"])
            != record["source_digest"]
            for plan in plans
        ):
            raise failure(
                "STATIC_INPUT_CONFLICT",
                "Source file inventory differs from its installation",
                "recording",
            )
        try:
            local = installed_path(record["host_path"], runtime.policy)
            if record["host_path"] not in observed_files:
                observed_files[record["host_path"]] = await _io(
                    partial(
                        content_proof,
                        local,
                        max_bytes=record["content_proof"]["size_bytes"],
                        max_entries=record["content_proof"]["entries"],
                    )
                )
            observed = observed_files[record["host_path"]]
            if observed != record["content_proof"]:
                raise ManagedRuntimeError("Static input content changed")
            files[file_id] = RegisteredFile(record["host_path"], record["source_digest"])
        except (OSError, ManagedRuntimeError):
            raise failure(
                "STATIC_INPUT_CONFLICT",
                "Pinned static input is unavailable or changed",
                "recording",
            ) from None
    assets = RootlessAssets(
        catalog,
        runtime,
        files=files,
        connector_id=manifest["id"],
        connector_version=manifest["version"],
    )
    image_ids = {
        selected.intent.image_id
        for plan in plans
        for resource in plan["resources"].values()
        if (selected := decode_lease(resource["lease"])).kind == "container"
    }
    for image_id in sorted(image_ids):
        try:
            image = await _io(partial(runtime.resolve_image, image_id))
            if image["image_id"] != image_id:
                raise ManagedRuntimeError("Source image identity changed")
        except ManagedRuntimeError:
            raise failure(
                "PINNED_IMAGE_UNAVAILABLE",
                "A retained native image is not installed",
                "unsupported",
            ) from None
    async with AsyncSession(store.engine) as db:
        await store.current(db, owner)
    return RetainedInstallation(deepcopy(manifest), assets, lease.intent.image_id)
