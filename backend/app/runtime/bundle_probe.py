"""No-model compatibility checks in the actual prepared task container."""

from __future__ import annotations

import asyncio
import re

from app.runtime.managed_exec import SUPERVISOR, OwnedDockerProcess, OwnedExecRuntime
from app.runtime.managed_spec import ExecSpec, ManagedRuntimeError, UncertainRuntimeError


async def check_bundle(runtime, container, bundle, resource_ids):
    """The host reserves these stable probe resource IDs before calling this.

    No advertised compatibility result authorizes execution or reserves capacity.
    A caller must still fence its owner generation and acquire the trial lease.
    """
    native = await asyncio.to_thread(runtime.container, container)
    if native is None or not any(
        mount.get("Destination") == "/evalhive-runtime"
        and mount.get("Source") == bundle.host_path
        and mount.get("RW") is False
        for mount in native.attrs.get("Mounts", [])
    ):
        raise ManagedRuntimeError(
            "Prepared environment does not contain the selected runtime bundle"
        )
    owned = OwnedExecRuntime(runtime)
    checks = [
        (("getconf", "GNU_LIBC_VERSION"), None),
        (
            (SUPERVISOR, "--capabilities"),
            '{"version":"1.0","subreaper":true,"pidfd":true,"private_inputs":true}\n',
        ),
        *bundle.probes,
    ]
    if len(resource_ids) != len(checks) or len(set(resource_ids)) != len(checks):
        raise ManagedRuntimeError("Bundle probes require distinct reserved resource identities")
    for index, ((argv, expected), resource_id) in enumerate(zip(checks, resource_ids, strict=True)):
        spec = ExecSpec(container, resource_id, tuple(argv), workdir="/tmp")
        handle = await asyncio.to_thread(owned.create, spec)
        process = None
        try:
            process = await OwnedDockerProcess.start(owned, handle, spec)
            async with asyncio.timeout(12):
                result = bytearray()
                while line := await process.stdout.readline():
                    result.extend(line)
                    if len(result) > 4096:
                        raise ManagedRuntimeError(
                            "Runtime compatibility probe exceeded output limit"
                        )
                code = await process.wait()
            matches = (
                bool(re.fullmatch(rb"glibc [0-9]+\.[0-9]+\n", result))
                if expected is None
                else result == expected.encode()
            )
            if code != 0 or not matches:
                return {
                    "compatible": False,
                    "reason": "task_libc_unavailable" if index == 0 else "runtime_probe_failed",
                }
        except (ManagedRuntimeError, OSError, TimeoutError):
            return {"compatible": False, "reason": "runtime_probe_failed"}
        finally:
            if process is not None:
                released = False
                try:
                    await asyncio.to_thread(owned.control, handle, "release")
                    released = True
                except Exception:
                    pass
                await process.aclose()
                if not released:
                    raise UncertainRuntimeError("Runtime probe cleanup needs reconciliation")
    return {
        "compatible": True,
        "bundle_image_id": bundle.image_id,
        "bundle_fingerprint": bundle.fingerprint,
        "runtime_admission": "pending",
    }
