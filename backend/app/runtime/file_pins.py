"""Bounded content proofs for administrator-installed read-only file grants."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from app.runtime.managed_spec import ManagedRuntimeError

MAX_BYTES = 2 * 1024**3
MAX_ENTRIES = 65536


def installed_path(host_path, policy):
    host = PurePosixPath(host_path)
    for mapping in policy.readonly:
        root = PurePosixPath(mapping.host)
        if host.is_relative_to(root):
            local = Path(mapping.local) / host.relative_to(root)
            if local.resolve(strict=True) != local:
                break
            return local
    raise ManagedRuntimeError("Retained static files require an installed read-only root")


def content_proof(root, *, max_bytes=None, max_entries=None):
    """Hash names, types, modes and bytes without retaining file contents."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    max_entries = MAX_ENTRIES if max_entries is None else max_entries
    digest, size, entries = hashlib.sha256(b"evalhive-file-proof-v1\n"), 0, 0

    def observe(path, relative, depth):
        nonlocal size, entries
        entries += 1
        if entries > max_entries or depth > 128:
            raise ManagedRuntimeError("Static file inventory exceeds policy")
        before = path.lstat()
        directory = stat.S_ISDIR(before.st_mode)
        if not directory and not stat.S_ISREG(before.st_mode):
            raise ManagedRuntimeError("Static file inventory contains an unsupported member")
        record = {
            "path": relative,
            "mode": before.st_mode & 0o777,
            "kind": "directory" if directory else "file",
        }
        if directory:
            with os.scandir(path) as children:
                names = []
                for child in children:
                    names.append(child.name)
                    if entries + len(names) > max_entries:
                        raise ManagedRuntimeError("Static file inventory exceeds policy")
            digest.update((json.dumps(record, sort_keys=True) + "\n").encode())
            for name in sorted(names):
                observe(path / name, name if relative == "." else relative + "/" + name, depth + 1)
        else:
            if size + before.st_size > max_bytes:
                raise ManagedRuntimeError("Static file inventory exceeds storage policy")
            hashed, count = hashlib.sha256(), 0
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ManagedRuntimeError("Static file inventory is not a regular file")
                while chunk := source.read(1024**2):
                    count += len(chunk)
                    if size + count > max_bytes:
                        raise ManagedRuntimeError("Static file inventory exceeds storage policy")
                    hashed.update(chunk)
            if count != before.st_size:
                raise ManagedRuntimeError("Static file changed during inventory")
            size += count
            record.update(size_bytes=count, digest="sha256:" + hashed.hexdigest())
            digest.update((json.dumps(record, sort_keys=True) + "\n").encode())
        after = path.lstat()
        fields = ("st_ino", "st_dev", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise ManagedRuntimeError("Static file changed during inventory")

    if root.resolve(strict=True) != root:
        raise ManagedRuntimeError("Static file inventory cannot traverse links")
    observe(root, ".", 0)
    return {
        "algorithm": "file_tree_sha256_v1",
        "digest": "sha256:" + digest.hexdigest(),
        "size_bytes": size,
        "entries": entries,
    }


def freeze_files(files, policy):
    result, observed, size, entries = {}, {}, 0, 0
    for file_id, grant in sorted(files.items()):
        proof = None
        if not grant.writable:
            try:
                if grant.host_path in observed:
                    proof = observed[grant.host_path]
                else:
                    proof = content_proof(
                        installed_path(grant.host_path, policy),
                        max_bytes=MAX_BYTES - size,
                        max_entries=MAX_ENTRIES - entries,
                    )
                    size += proof["size_bytes"]
                    entries += proof["entries"]
                    observed[grant.host_path] = proof
            except (OSError, ManagedRuntimeError):
                pass
        result[file_id] = {**asdict(grant), "content_proof": proof}
    return result
