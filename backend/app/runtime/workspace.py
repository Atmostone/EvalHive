"""Map server-owned attempt directories to host mounts without harness conventions."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import get_settings
from app.runtime.spec import BindMount


@dataclass(frozen=True)
class AttemptLayout:
    root: Path
    workspace: Path
    private: Path
    host_workspace: str
    host_private: str

    def workspace_mount(self, target: str = "/workspace") -> BindMount:
        return BindMount(self.host_workspace, target, read_only=False)


def prepare_layout(task_id: str, attempt_id: str) -> AttemptLayout:
    task_id, attempt_id = str(uuid.UUID(task_id)), str(uuid.UUID(attempt_id))
    settings = get_settings()
    data = Path(settings.data_dir).resolve()
    base = data / "workspaces"
    root = base / task_id / attempt_id
    # The task may contain files from an earlier legacy attempt. Refuse a
    # symlink at any managed ancestor instead of following it out of the mount.
    for path in (base, base / task_id, root):
        if path.is_symlink():
            raise ValueError("Attempt directory cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.resolve().is_relative_to(data):
        raise ValueError("Attempt directory escapes the configured data root")
    workspace, private = root / "workspace", root / "private"
    for path in (workspace, private):
        if path.is_symlink():
            raise ValueError("Attempt directory cannot be a symlink")
        path.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    host_data = PurePosixPath(settings.host_data_dir)
    if not host_data.is_absolute() or ".." in host_data.parts:
        raise ValueError("Host data directory must be absolute")
    host_root = host_data / "workspaces" / task_id / attempt_id
    return AttemptLayout(
        root, workspace, private, str(host_root / "workspace"), str(host_root / "private")
    )


def write_private_json(layout: AttemptLayout, name: str, value: dict) -> str:
    """Durable private input; returned host path is mounted read-only into the runner."""
    if not name or Path(name).name != name or name in (".", ".."):
        raise ValueError("Private input name must be a filename")
    temporary = layout.private / ("." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(layout.private / name)
        directory_fd = os.open(layout.private, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return str(PurePosixPath(layout.host_private) / name)


def seed_workspace(layout: AttemptLayout, *, max_bytes=256 * 1024 * 1024):
    """Copy prepared task inputs, excluding all managed attempt directories.

    Environment preprocessing owns the task root. Each connector receives a
    separate copy so retries cannot inherit files written by another attempt.
    """
    source = layout.root.parent
    total = 0
    managed = {layout.root.name}
    for candidate in source.iterdir():
        if candidate.is_symlink():
            raise ValueError("Prepared task inputs cannot contain symlinks")
        try:
            uuid.UUID(candidate.name)
        except ValueError:
            continue
        if (candidate / "workspace").is_dir() and (candidate / "private").is_dir():
            managed.add(candidate.name)
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if relative.parts[0] in managed:
            continue
        if path.is_symlink():
            raise ValueError("Prepared task inputs cannot contain symlinks")
        destination = layout.workspace / relative
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            total += path.stat().st_size
            if total > max_bytes:
                raise ValueError("Prepared task inputs exceed the size limit")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        else:
            raise ValueError("Prepared task inputs must be regular files")
