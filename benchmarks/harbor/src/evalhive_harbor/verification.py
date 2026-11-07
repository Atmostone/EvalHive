"""Run the pinned Harbor verifier over a connector-owned environment adapter."""

from __future__ import annotations

import asyncio
import math

from harbor.models.task.verifier_mode import resolve_verifier_environment_definition
from harbor.verifier.verifier import (
    AddTestsDirError,
    DownloadVerifierDirError,
    RewardFileEmptyError,
    RewardFileNotFoundError,
    Verifier,
    VerifierOutputParseError,
)

from .native import HarborConfigurationError, source_digest


class NativeVerificationError(RuntimeError):
    """A public category; native exception details remain on the private cause."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


async def run_verifier(native, environment, paths, *, remaining_seconds=None):
    """Keep test staging, stale reward removal and parsing in the official SDK.

    The caller owns the prepared shared/separate environment and isolated worker
    bindings. This function neither chooses an image nor creates a Docker client.
    A numeric reward is returned verbatim; no binary success threshold is inferred.
    """
    if source_digest(native.task.paths.task_dir) != native.source_digest:
        raise HarborConfigurationError("Native verifier source changed after installation")
    if native.task.config.steps:
        raise HarborConfigurationError("This verifier adapter requires a single-step task")
    timeout = native.task.config.verifier.timeout_sec
    if remaining_seconds is not None:
        if (
            isinstance(remaining_seconds, bool) or not isinstance(remaining_seconds, (int, float))
            or not math.isfinite(remaining_seconds) or remaining_seconds <= 0
        ):
            raise NativeVerificationError("NATIVE_VERIFIER_DEADLINE")
        timeout = min(timeout, remaining_seconds)
    if not math.isfinite(timeout) or timeout < 0:
        raise HarborConfigurationError("Native verifier timeout must be finite and non-negative")
    definition = resolve_verifier_environment_definition(native.task.config, native.task.paths)
    verifier = Verifier(
        task=native.task, trial_paths=paths, environment=environment,
        skip_tests_upload=bool(definition and definition.bundled_tests),
    )
    try:
        with environment.with_default_user(native.task.config.verifier.user):
            async with asyncio.timeout(timeout):
                return await verifier.verify()
    except TimeoutError:
        raise NativeVerificationError("NATIVE_VERIFIER_TIMEOUT") from None
    except (RewardFileNotFoundError, RewardFileEmptyError, VerifierOutputParseError) as error:
        code = {
            RewardFileNotFoundError: "NATIVE_REWARD_MISSING",
            RewardFileEmptyError: "NATIVE_REWARD_EMPTY",
            VerifierOutputParseError: "NATIVE_REWARD_INVALID",
        }[type(error)]
        raise NativeVerificationError(code) from error
    except (AddTestsDirError, DownloadVerifierDirError) as error:
        raise NativeVerificationError("NATIVE_VERIFIER_TRANSFER_FAILED") from error
