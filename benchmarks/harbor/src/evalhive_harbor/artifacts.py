"""Preserve Harbor's native collection manifest and best-effort hook semantics."""

from __future__ import annotations

import logging

from harbor.constants import MAIN_SERVICE_NAME
from harbor.models.task.artifacts import normalize_artifact_entries
from harbor.models.trial.paths import EnvironmentPaths
from harbor.trial.artifact_handler import ArtifactHandler

from .native import HarborConfigurationError, source_digest


def validate_main_artifacts(native):
    """Reject unavailable service/exclusion behavior before native effects."""
    if any(hook.service != MAIN_SERVICE_NAME for hook in native.task.config.verifier.collect):
        raise HarborConfigurationError(
            "Native sidecar collection needs an installed service binding"
        )
    for artifact in normalize_artifact_entries(native.task.config.artifacts):
        if artifact.service not in {None, MAIN_SERVICE_NAME}:
            raise HarborConfigurationError(
                "Native sidecar artifacts need an installed service binding"
            )
        if artifact.exclude:
            raise HarborConfigurationError(
                "Native tar exclusions need an installed compatible adapter"
            )


async def collect_artifacts(native, environment, paths):
    validate_main_artifacts(native)
    if source_digest(native.task.paths.task_dir) != native.source_digest:
        raise HarborConfigurationError("Native artifact source changed after installation")
    hooks = []
    for hook in native.task.config.verifier.collect:
        try:
            result = await environment.service_exec(
                hook.command,
                service=hook.service,
                timeout_sec=int(hook.timeout_sec),
                user=hook.user,
            )
            hooks.append(
                {
                    "status": "completed" if result.return_code == 0 else "error",
                    "exit_code": result.return_code,
                }
            )
        except Exception:
            # Harbor's native collect hooks are best-effort. Keep the failure
            # visible without exposing private command arguments or exceptions.
            hooks.append({"status": "error", "exit_code": None})
    handler = ArtifactHandler(
        artifacts=native.task.config.artifacts, logger=logging.getLogger(__name__)
    )
    handler.begin_collection()
    manifest = await handler.download_artifacts(
        environment,
        paths.artifacts_dir,
        source_artifacts_dir=EnvironmentPaths.for_os(environment.os).artifacts_dir,
        services={MAIN_SERVICE_NAME},
    )
    return {
        "manifest": manifest.to_json_data(),
        "collect_hooks": hooks,
        "complete": bool(manifest.entries)
        and all(entry.status in {"ok", "empty"} for entry in manifest.entries)
        and all(hook["status"] == "completed" for hook in hooks),
    }


async def restore_artifacts(native, environment, paths):
    validate_main_artifacts(native)
    handler = ArtifactHandler(
        artifacts=native.task.config.artifacts, logger=logging.getLogger(__name__)
    )
    env_paths = EnvironmentPaths.for_os(environment.os)
    await handler.upload_artifacts(
        environment,
        paths.artifacts_dir,
        source_artifacts_dir=env_paths.artifacts_dir,
        target_artifacts_dir=env_paths.artifacts_dir,
    )
