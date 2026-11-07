"""Verify the installed official source and dataset before importing native code."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import stat
from pathlib import Path

REVISION = "42b5bcf3cd334fee33f0c37c02070a9f5807add5"
VERSION, DATA_VERSION = "0.2.0.dev0", "0.2.0"
SPLITS = ("train", "dev", "test_normal", "test_challenge")
MAX_FILE_BYTES, MAX_SOURCE_BYTES = 128 * 1024**2, 512 * 1024**2
CODE_LOCK = json.loads(Path(__file__).with_name("native-code.lock.json").read_text())


class NativeConfigurationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def fingerprint(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def directory(path):
    root = Path(path).absolute()
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise NativeConfigurationError("Native data requires an installed directory without links")
    return root


def file_record(root, path):
    if path.resolve(strict=True) != path:
        raise NativeConfigurationError("Native source must not traverse links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise NativeConfigurationError("Native source exceeds its regular file budget")
        value = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(value) != before.st_size or any(
        getattr(before, key) != getattr(after, key)
        for key in ("st_ino", "st_dev", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise NativeConfigurationError("Native source changed during inventory")
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
    }


def load(root):
    root = directory(root)
    cache = os.environ.get("APPWORLD_CACHE")
    if (
        os.environ.get("PYTHON_DOTENV_DISABLED") != "1"
        or os.environ.get("APPWORLD_ROOT") != str(root)
        or not cache
        or not Path(cache).is_absolute()
    ):
        raise NativeConfigurationError("Native imports require explicit roots and disabled dotenv")
    if importlib.metadata.version("appworld") != VERSION:
        raise NativeConfigurationError("Native AppWorld package differs from its pinned version")
    spec = importlib.util.find_spec("appworld")
    if spec is None or spec.origin is None:
        raise NativeConfigurationError("Native AppWorld package is unavailable")
    code = directory(Path(spec.origin).parent)
    files = [file_record(code, path) for path in sorted(code.rglob("*.py"))]
    if CODE_LOCK["revision"] != REVISION or files != CODE_LOCK["files"]:
        raise NativeConfigurationError("Native AppWorld code differs from its pinned source")
    for path in (root / "data/version.txt", root / "data/base_dbs/version.txt"):
        file_record(root, path)
        if path.read_text().strip() != DATA_VERSION:
            raise NativeConfigurationError("Native AppWorld data version is incompatible")
    native = importlib.import_module("appworld")
    paths = importlib.import_module("appworld.common.path_store").path_store
    if paths.root != str(root) or paths.cache != cache:
        raise NativeConfigurationError("Native AppWorld is already bound to another root")
    return native


def task_ids(root, split):
    if split not in SPLITS:
        raise NativeConfigurationError("Native AppWorld split must be explicit")
    values = load(root).load_task_ids(split)
    if (
        not values
        or len(values) > 10000
        or len(set(values)) != len(values)
        or any(not re.fullmatch(r"[a-f0-9]{7}_[1-9][0-9]*", value) for value in values)
    ):
        raise NativeConfigurationError("Native AppWorld split has invalid task identities")
    return values


def inventory(root, split, identities):
    root = directory(root)
    paths = [root / "data/version.txt", root / "data/datasets" / (split + ".txt")]
    selected = [root / "data/base_dbs", root / "data/api_docs"]
    selected += [root / "data/tasks" / identity for identity in identities]
    for tree in selected:
        directory(tree)
        for path in sorted(tree.rglob("*")):
            if path.is_symlink():
                raise NativeConfigurationError("Native dataset must not contain links")
            if path.is_file():
                paths.append(path)
    if len(paths) > 20000:
        raise NativeConfigurationError("Native dataset exceeds its inventory budget")
    result = [file_record(root, path) for path in sorted(paths)]
    if sum(row["size_bytes"] for row in result) > MAX_SOURCE_BYTES:
        raise NativeConfigurationError("Native dataset exceeds its byte budget")
    return result
