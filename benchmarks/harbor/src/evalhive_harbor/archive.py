"""Bounded native file staging outside the task's executable filesystem."""

from __future__ import annotations

import hashlib
import os
import stat
import tarfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_ENTRIES = 65536
CHUNK_BYTES = 24 * 1024


class TransferError(ValueError):
    pass


class BoundedWriter:
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit

    def write(self, data):
        if len(data) > self.remaining:
            raise TransferError("Native transfer exceeds its archive budget")
        written = self.stream.write(data)
        self.remaining -= written
        return written


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pack_directory(source, destination, *, limit=MAX_ARCHIVE_BYTES):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise TransferError("Native upload requires a directory without a root link")
    count, logical = 0, 0

    def members(directory, depth=0):
        nonlocal count, logical
        if depth > 128:
            raise TransferError("Native transfer exceeds its directory depth")
        with os.scandir(directory) as children:
            for child in children:
                count += 1
                if count > MAX_ENTRIES:
                    raise TransferError("Native transfer exceeds its entry budget")
                path = Path(child.path)
                info = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    link = os.readlink(path)
                    if os.path.isabs(link) or not (path.parent / link).resolve().is_relative_to(source.resolve()):
                        raise TransferError("Native upload contains an unsafe link")
                elif not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                    raise TransferError("Native upload contains unsupported filesystem entries")
                logical += info.st_size if stat.S_ISREG(info.st_mode) else 0
                if logical > limit:
                    raise TransferError("Native transfer exceeds its logical byte budget")
                yield path
                if stat.S_ISDIR(info.st_mode):
                    yield from members(path, depth + 1)

    with Path(destination).open("xb") as stream:
        with tarfile.open(fileobj=BoundedWriter(stream, limit), mode="w|") as archive:
            for path in members(source):
                info = archive.gettarinfo(str(path), arcname=path.relative_to(source).as_posix())
                if not info.isdir() and not info.isfile() and not info.issym() and not info.islnk():
                    raise TransferError("Native upload changed its filesystem type")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.isfile():
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as content:
                        observed = os.fstat(content.fileno())
                        if not stat.S_ISREG(observed.st_mode) or observed.st_size != info.size:
                            raise TransferError("Native upload changed during staging")
                        archive.addfile(info, content)
                else:
                    archive.addfile(info)
    return {"size_bytes": Path(destination).stat().st_size, "sha256": digest_file(destination)}


def unpack_directory(source, destination, *, limit=MAX_ARCHIVE_BYTES):
    if Path(source).stat().st_size > limit:
        raise TransferError("Native transfer exceeds its archive budget")
    destination = Path(destination)
    if destination.is_symlink():
        raise TransferError("Native transfer destination cannot be a link")
    destination.mkdir(parents=True, exist_ok=True)
    logical, entries, names = 0, [], set()
    with tarfile.open(source, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or len(name.parts) > 128:
                raise TransferError("Native archive path leaves the transfer root")
            normalized = name.as_posix()
            if len(entries) >= MAX_ENTRIES or normalized in names:
                raise TransferError("Native archive exceeds its entry budget or repeats a path")
            if not member.isfile() and not member.isdir() and not member.issym() and not member.islnk():
                raise TransferError("Native archive contains unsupported filesystem entries")
            logical += member.size
            if logical > limit:
                raise TransferError("Native transfer exceeds its logical byte budget")
            # Validate every entry before writing any contents.
            tarfile.data_filter(member, str(destination))
            entries.append(member)
            names.add(normalized)
        # A later entry must never traverse a link declared by the archive.
        # This permits native relative aliases while making validation independent
        # of extraction order and of future link targets.
        links = {PurePosixPath(member.name) for member in entries if member.issym() or member.islnk()}
        by_name = {PurePosixPath(member.name): member for member in entries}
        for member in entries:
            name = PurePosixPath(member.name)
            if any(parent in links for parent in name.parents):
                raise TransferError("Native archive entry descends through a link")
            if member.issym() or member.islnk():
                target = name.parent / member.linkname if member.issym() else PurePosixPath(member.linkname)
                if any(parent in links for parent in target.parents):
                    raise TransferError("Native archive contains chained links")
                seen = set()
                while target in links:
                    linked = by_name[target]
                    if linked.issym() or target in seen:
                        raise TransferError("Native archive contains chained symbolic links")
                    seen.add(target)
                    target = PurePosixPath(linked.linkname)
                    if any(parent in links for parent in target.parents):
                        raise TransferError("Native archive contains chained link parents")
                if member.islnk() and (target not in by_name or not by_name[target].isfile()):
                    raise TransferError("Native hard link does not resolve to a recorded regular file")
        archive.extractall(destination, members=entries, filter="data")
