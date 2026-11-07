"""Fingerprint installed initial state before a native manager can copy it."""

from __future__ import annotations

import stat
from pathlib import Path

from .native import NativeConfigurationError, directory, file_record, fingerprint


def filesystem_asset(root, category):
    if (
        not isinstance(category, str)
        or not category
        or Path(category).name != category
        or category in {".", ".."}
    ):
        raise NativeConfigurationError("Initial filesystem category is invalid")
    base = directory(root)
    selected = directory(base / category)
    files, total = [], 0
    for path in sorted(selected.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        record = file_record(base, path)
        record.update(mode=stat.S_IMODE(info.st_mode), mtime_ns=str(info.st_mtime_ns))
        files.append(record)
        total += record["size_bytes"]
        if len(files) > 10000 or total > 128 * 1024**2:
            raise NativeConfigurationError("Initial filesystem state exceeds its budget")
    if not files:
        raise NativeConfigurationError("Initial filesystem state is empty")
    return {
        "id": "filesystem." + category,
        "service": "filesystem",
        "category": category,
        "source_digest": fingerprint(files),
        "files": files,
    }
