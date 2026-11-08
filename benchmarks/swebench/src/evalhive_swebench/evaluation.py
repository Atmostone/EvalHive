"""Grade fresh native test evidence without consulting cached evaluation reports."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path

from .native import VERSION, NativeConfigurationError, canonical, verified_code

MAX_LOG_BYTES, MAX_PATCH_BYTES = 64 * 1024**2, 8 * 1024**2


def grade(test_spec, patch, log_path, records_root):
    verified_code()
    if patch is not None and (not isinstance(patch, str) or len(patch.encode()) > MAX_PATCH_BYTES):
        raise NativeConfigurationError("Native model patch exceeds its text budget")
    log_path = Path(log_path).absolute()
    if log_path.resolve(strict=True) != log_path:
        raise NativeConfigurationError("Native test evidence must not traverse links")
    descriptor = os.open(log_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_LOG_BYTES:
            raise NativeConfigurationError("Native test evidence exceeds its file budget")
        data = stream.read(MAX_LOG_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or any(
        getattr(before, key) != getattr(after, key)
        for key in ("st_ino", "st_dev", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise NativeConfigurationError("Native test evidence changed during evaluation")
    root = Path(records_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_id = "swebench-" + uuid.uuid4().hex
    run = root / run_id
    run.mkdir(mode=0o700)
    target = run / "test_output.txt"
    with target.open("xb") as stream:
        stream.write(data)
    from swebench.harness.grading import get_eval_report

    prediction = {
        "instance_id": test_spec.instance_id,
        "model_name_or_path": "evalhive",
        "model_patch": patch,
    }
    report = get_eval_report(test_spec, prediction, str(target), include_tests_status=True)
    metadata = {
        "native_version": VERSION,
        "run_id": run_id,
        "instance_id": test_spec.instance_id,
        "patch_sha256": None if patch is None else hashlib.sha256(patch.encode()).hexdigest(),
        "test_log_sha256": hashlib.sha256(data).hexdigest(),
        "cached_report_used": False,
    }
    for name, value in (("report.json", report), ("run.json", metadata)):
        with (run / name).open("xb") as stream:
            stream.write(canonical(value) + b"\n")
    return {"report": report, "metadata": metadata}
