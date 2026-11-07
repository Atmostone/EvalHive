"""Verify native evaluator code and explicit task-repository bytes before import."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import stat
from pathlib import Path

VERSION = "5.0.2"
REVISION = "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e"
TASK_REVISION = "3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d"
SUBSETS = {
    "verified": "SWE-bench/SWE-bench_Verified",
    "lite": "SWE-bench/SWE-bench_Lite",
    "full": "SWE-bench/SWE-bench",
}
CODE_LOCK = json.loads(Path(__file__).with_name("native-code.lock.json").read_text())
MAX_FILE_BYTES, MAX_SOURCE_BYTES = 16 * 1024**2, 256 * 1024**2


class NativeConfigurationError(ValueError):
    pass


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def fingerprint(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def directory(path):
    root = Path(path).absolute()
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise NativeConfigurationError("Native installation requires a directory without links")
    return root


def file_record(root, path):
    if path.resolve(strict=True) != path:
        raise NativeConfigurationError("Native source must not traverse links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise NativeConfigurationError("Native file exceeds its regular-file budget")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or any(
        getattr(before, key) != getattr(after, key)
        for key in ("st_ino", "st_dev", "st_size", "st_mode", "st_mtime_ns", "st_ctime_ns")
    ):
        raise NativeConfigurationError("Native file changed during inventory")
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def verified_code():
    if os.environ.get("PYTHON_DOTENV_DISABLED") != "1":
        raise NativeConfigurationError("Native imports require disabled dotenv")
    if importlib.metadata.version("swebench") != VERSION:
        raise NativeConfigurationError("Native evaluator version differs from its installation")
    spec = importlib.util.find_spec("swebench")
    if spec is None or spec.origin is None:
        raise NativeConfigurationError("Native evaluator package is unavailable")
    root = directory(Path(spec.origin).parent)
    records = [
        file_record(root, path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    ]
    if records != CODE_LOCK["files"] or CODE_LOCK["revision"] != REVISION:
        raise NativeConfigurationError("Native evaluator code differs from its pinned source")
    return root


def inventory(root, identities):
    root = directory(root)
    paths = [root / "sweb.yaml"]
    for identity in identities:
        tree = directory(root / "tasks" / identity)
        for path in sorted(tree.rglob("*")):
            if path.is_symlink():
                raise NativeConfigurationError("Native task source must not contain links")
            if path.is_file():
                paths.append(path)
    if len(paths) > 50000:
        raise NativeConfigurationError("Native task repository exceeds its inventory budget")
    records = [file_record(root, path) for path in sorted(paths)]
    if sum(row["size_bytes"] for row in records) > MAX_SOURCE_BYTES:
        raise NativeConfigurationError("Native task repository exceeds its byte budget")
    return records
