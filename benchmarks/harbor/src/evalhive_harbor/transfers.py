"""Native directory transfers through owned execs and an agent-read-only stage."""

from __future__ import annotations

import base64
import json
import re
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from harbor.utils.path_filter import filter_paths_by_patterns

from .archive import (
    CHUNK_BYTES,
    MAX_ARCHIVE_BYTES,
    TransferError,
    digest_file,
    pack_directory,
    unpack_directory,
)

TOOLS = "/evalhive-transfer"


def absolute_path(value):
    if not isinstance(value, str) or not value or "\0" in value:
        raise TransferError("Native transfer requires an absolute POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise TransferError("Native transfer path leaves its environment")
    return value


class BrokerTransfers:
    """Exec callbacks enforce scope, ownership, cancellation and parent deadlines.

    The installed toolchain is immutable and mounted read-only. Its explicit
    loader and absolute applet paths do not depend on the task's PATH or binaries.
    The stage is writable only by the private service, never by the task.
    """

    def __init__(self, task_exec, stage_exec, *, loader="ld-musl-x86_64.so.1", temporary=None):
        if not re.fullmatch(r"ld-musl-[a-z0-9_]+\.so\.1", loader):
            raise TransferError("Native transfer loader is invalid")
        self.task_exec, self.stage_exec = task_exec, stage_exec
        self.loader, self.temporary = loader, temporary

    async def _task(self, script):
        result = await self.task_exec(
            [TOOLS + "/" + self.loader, TOOLS + "/busybox", "ash", "-c", script],
            user="0", environment={"LD_PRELOAD": "", "LD_LIBRARY_PATH": ""},
        )
        if result.get("exit_code") != 0 or result.get("stdout_truncated") or result.get("stderr_truncated"):
            raise TransferError("Native filesystem operation failed or exceeded its recording budget")
        return result.get("stdout", "")

    def _tool(self, *arguments):
        return shlex.join([TOOLS + "/" + self.loader, TOOLS + "/busybox", *arguments])

    async def _stage(self, operation, payload):
        result = await self.stage_exec([
            "/opt/venv/bin/python", "-m", "evalhive_harbor.staging", operation,
            json.dumps(payload, separators=(",", ":")),
        ])
        if result.get("exit_code") != 0 or result.get("stdout_truncated") or result.get("stderr_truncated"):
            raise TransferError("Private native staging operation failed")
        return json.loads(result["stdout"])

    async def upload(self, *, source_dir, target_dir):
        target_dir = absolute_path(target_dir)
        identity = uuid.uuid4().hex
        with tempfile.TemporaryDirectory(dir=self.temporary, prefix="harbor-upload-") as temporary:
            archive = Path(temporary) / "payload.tar"
            proof = pack_directory(source_dir, archive)
            await self._stage("create", {"id": identity})
            try:
                with archive.open("rb") as stream:
                    offset = 0
                    while data := stream.read(CHUNK_BYTES):
                        receipt = await self._stage("write", {
                            "id": identity, "offset": offset,
                            "data": base64.b64encode(data).decode(),
                        })
                        if receipt.get("written") != len(data):
                            raise TransferError("Native staging did not acknowledge the complete chunk")
                        offset += len(data)
                sealed = await self._stage("seal", {"id": identity, **proof})
                remote = "/evalhive-stage/" + identity + ".tar"
                if not sealed.get("sealed") or sealed.get("path") != remote:
                    raise TransferError("Native staging returned an unexpected archive")
                await self._task(
                    self._tool("mkdir", "-p", "--", target_dir) + " && "
                    + self._tool("tar", "-xf", remote, "-C", target_dir)
                )
            finally:
                await self._stage("remove", {"id": identity})

    async def upload_file(self, *, source_path, target_path):
        target = PurePosixPath(absolute_path(target_path))
        with tempfile.TemporaryDirectory(dir=self.temporary, prefix="harbor-upload-file-") as temporary:
            shutil.copy2(source_path, Path(temporary) / target.name)
            await self.upload(source_dir=temporary, target_dir=str(target.parent))

    async def path_is_dir(self, path):
        path = absolute_path(path)
        value = (await self._task(
            "if " + self._tool("test", "-d", path) + "; then "
            + self._tool("printf", "directory") + "; else "
            + self._tool("printf", "other") + "; fi"
        )).strip()
        if value not in {"directory", "other"}:
            raise TransferError("Native path inspection returned an invalid receipt")
        return value == "directory"

    async def download_file(self, *, source_path, target_path):
        source = PurePosixPath(absolute_path(source_path))
        with tempfile.TemporaryDirectory(dir=self.temporary, prefix="harbor-download-file-") as temporary:
            await self.download(source_dir=str(source.parent), target_dir=temporary, members=[source.name])
            destination = Path(target_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staged = Path(temporary) / source.name
            if staged.is_symlink() or not staged.is_file():
                raise TransferError("Native file download did not produce a regular file")
            shutil.copy2(staged, destination)

    async def download(self, *, source_dir, target_dir, include=None, exclude=None, protect=None, members=None):
        source_dir = absolute_path(source_dir)
        if members is not None and any(not name or "/" in name or name in {".", ".."} for name in members):
            raise TransferError("Native file transfer requires source-relative basenames")
        remote = "/tmp/.evalhive-harbor-" + uuid.uuid4().hex
        archive = remote + "/payload.tar"
        with tempfile.TemporaryDirectory(dir=self.temporary, prefix="harbor-download-") as temporary:
            local = Path(temporary) / "payload.tar"
            try:
                # BusyBox ash uses 1024-byte units for RLIMIT_FSIZE. Leave the
                # native source untouched and reject oversized archive writes.
                await self._task(
                    "umask 077; " + self._tool("mkdir", "--", remote)
                    + " && ulimit -f " + str(MAX_ARCHIVE_BYTES // 1024)
                    + " && " + self._tool("tar", "-cf", archive, "-C", source_dir, "--", *(members or ["."]))
                )
                observed = (await self._task(self._tool("stat", "-c", "%s", archive))).strip()
                if not observed.isdecimal() or not 0 < int(observed) <= MAX_ARCHIVE_BYTES:
                    raise TransferError("Native archive exceeds its transfer budget")
                size = int(observed)
                proof = (await self._task(self._tool("sha256sum", archive))).split()[0]
                if not re.fullmatch(r"[a-f0-9]{64}", proof):
                    raise TransferError("Native archive has no valid byte proof")
                with local.open("xb") as stream:
                    for offset in range(0, size, CHUNK_BYTES):
                        encoded = await self._task(
                            self._tool("dd", "if=" + archive, "bs=" + str(CHUNK_BYTES),
                                       "skip=" + str(offset // CHUNK_BYTES), "count=1")
                            + " | " + self._tool("base64")
                        )
                        data = base64.b64decode("".join(encoded.split()), validate=True)
                        if len(data) != min(CHUNK_BYTES, size - offset):
                            raise TransferError("Native archive transfer is incomplete")
                        stream.write(data)
                if digest_file(local) != proof:
                    raise TransferError("Native archive changed during transfer")
                extracted = Path(temporary) / "extracted"
                unpack_directory(local, extracted)
                target = Path(target_dir)
                target.mkdir(parents=True, exist_ok=True)
                if include is None and exclude is None and not protect:
                    shutil.copytree(extracted, target, dirs_exist_ok=True, symlinks=True)
                else:
                    files = [path.relative_to(extracted).as_posix() for path in extracted.rglob("*")
                             if path.is_file() and not path.is_symlink()]
                    selected = set(filter_paths_by_patterns(files, include=include, exclude=exclude))
                    selected.update(set(protect or ()) & set(files))
                    for name in sorted(selected):
                        destination = target / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(extracted / name, destination)
            finally:
                await self._task(self._tool("rm", "-rf", "--", remote))
