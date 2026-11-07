"""Fingerprint installed initial state before a native manager can copy it."""

from __future__ import annotations

import stat
from pathlib import Path

from .native import NativeConfigurationError, directory, file_record, fingerprint

POSTGRES_DATABASES = ("employees", "chinook", "dvdrental", "sports", "lego")


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
    files, directories, total = [], [], 0
    for path in [selected, *sorted(selected.rglob("*"))]:
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            directories.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "mode": stat.S_IMODE(info.st_mode),
                    "mtime_ns": str(info.st_mtime_ns),
                }
            )
            if len(directories) > 10000:
                raise NativeConfigurationError("Initial filesystem directories exceed their budget")
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
        "source_digest": fingerprint({"files": files, "directories": directories}),
        "files": files,
        "directories": directories,
    }


def postgres_asset(root):
    root = directory(root)
    files = [
        file_record(root, root / (name + ".backup"), max_bytes=64 * 1024**2)
        for name in POSTGRES_DATABASES
    ]
    if sum(item["size_bytes"] for item in files) > 128 * 1024**2:
        raise NativeConfigurationError("Native PostgreSQL backups exceed their source budget")
    return {
        "id": "postgres.backups",
        "service": "postgres",
        "source_digest": fingerprint(files),
        "files": files,
    }
