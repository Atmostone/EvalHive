"""Install administrator-registered bundle artifacts, never executable UI uploads."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from docker.errors import DockerException, NotFound
from evalhive_harness import ProtocolError, validate

from app.runtime.managed_spec import ManagedRuntimeError

MAX_BYTES = 2 * 1024**3
MAX_RECEIPT = 16 * 1024**2


def _path(value):
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\0" in value
        or str(path) != value
        or value == "."
    ):
        raise ManagedRuntimeError("Invalid runtime bundle member path")
    return path


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class RuntimeBundle:
    image_id: str
    fingerprint: str
    host_path: str
    local_path: str
    entrypoint: tuple[str, ...]
    connector_id: str
    connector_version: str
    probes: tuple[tuple[tuple[str, ...], str], ...]


def validate_bundle(root: Path, connector: dict, *, expected_hash=None):
    try:
        if root.resolve(strict=True) != root or (root / "bundle.json").is_symlink():
            raise ManagedRuntimeError("Runtime bundle cannot contain filesystem links")
        receipt_path = root / "bundle.json"
        if receipt_path.stat().st_size > MAX_RECEIPT:
            raise ManagedRuntimeError("Runtime bundle receipt exceeds size limit")
        receipt = validate("runtime_bundle", json.loads(receipt_path.read_text()))
        fingerprint = _hash(receipt_path)
        if expected_hash and fingerprint != expected_hash:
            raise ManagedRuntimeError("Runtime bundle receipt changed since installation")
        if (
            receipt["connector_id"] != connector["id"]
            or receipt["connector_version"] != connector["version"]
        ):
            raise ManagedRuntimeError("Runtime bundle connector identity does not match")
        files = receipt["files"]
        if "supervisor" not in files or "bundle.json" in files:
            raise ManagedRuntimeError("Runtime bundle supervisor is missing")
        actual, total = set(), 0
        for value in root.rglob("*"):
            if value.is_symlink() or (not value.is_file() and not value.is_dir()):
                raise ManagedRuntimeError("Runtime bundle contains an unsupported member")
            if value.is_file() and value != receipt_path:
                actual.add(str(value.relative_to(root)))
        if actual != set(files):
            raise ManagedRuntimeError("Runtime bundle inventory does not match installed files")
        for name, record in files.items():
            value = root / _path(name)
            total += record["size"]
            if (
                total > MAX_BYTES
                or value.stat().st_size != record["size"]
                or bool(value.stat().st_mode & 0o111) != record["executable"]
                or _hash(value) != record["sha256"]
            ):
                raise ManagedRuntimeError("Runtime bundle file integrity check failed")
        for argv in [receipt["entrypoint"], *[probe["argv"] for probe in receipt["probes"]]]:
            for argument in argv:
                if "\0" in argument:
                    raise ManagedRuntimeError("Runtime bundle argument is invalid")
                if argument.startswith("/"):
                    path = PurePosixPath(argument)
                    if not path.is_relative_to(receipt["mount_path"]):
                        raise ManagedRuntimeError("Runtime bundle entrypoint escapes its mount")
                    _path(str(path.relative_to(receipt["mount_path"])))
            executable = str(PurePosixPath(argv[0]).relative_to(receipt["mount_path"]))
            if executable not in files or not files[executable]["executable"]:
                raise ManagedRuntimeError("Runtime bundle entrypoint is not executable")
        if not files["supervisor"]["executable"]:
            raise ManagedRuntimeError("Runtime bundle supervisor is not executable")
        return receipt, fingerprint
    except ManagedRuntimeError:
        raise
    except (OSError, ValueError, ProtocolError, TypeError, KeyError):
        raise ManagedRuntimeError("Installed runtime bundle is invalid") from None


class _Chunks(io.RawIOBase):
    def __init__(self, chunks):
        self.chunks, self.pending = iter(chunks), memoryview(b"")

    def readable(self):
        return True

    def readinto(self, buffer):
        while not self.pending:
            try:
                self.pending = memoryview(next(self.chunks))
            except StopIteration:
                return 0
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


def unpack_bundle(chunks, destination: Path):
    """Stream a bounded archive; reject links, traversal, duplicate and special files."""
    total, names = 0, set()
    with tarfile.open(fileobj=io.BufferedReader(_Chunks(chunks)), mode="r|") as archive:
        for member in archive:
            path = _path(member.name.removesuffix("/") if member.isdir() else member.name)
            if path.parts[0] != "bundle":
                raise ManagedRuntimeError("Runtime bundle archive has an unexpected root")
            if member.name in names or len(names) >= 100001:
                raise ManagedRuntimeError(
                    "Runtime bundle archive has duplicate or excessive members"
                )
            names.add(member.name)
            if not member.isdir() and not member.isfile():
                raise ManagedRuntimeError(
                    "Runtime bundle archive links and special files are forbidden"
                )
            if member.size < 0:
                raise ManagedRuntimeError("Runtime bundle member has an invalid size")
            total += member.size
            if total > MAX_BYTES + MAX_RECEIPT:
                raise ManagedRuntimeError("Runtime bundle archive exceeds size limit")
            target = destination.joinpath(*path.parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                if not path.parts[1:]:
                    raise ManagedRuntimeError("Runtime bundle root is not a directory")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with target.open("xb") as stream:
                    shutil.copyfileobj(archive.extractfile(member), stream, 128 * 1024)
                target.chmod(0o555 if member.mode & 0o111 else 0o444)


def check_mount_target(runtime, image_id: str):
    """Inspect the immutable image without starting it or hiding native files."""
    if runtime.resolve_image(image_id)["image_id"] != image_id:
        raise ManagedRuntimeError("Runtime bundle target probe requires an immutable task image")
    source = None
    try:
        source = runtime.client.containers.create(
            image_id,
            entrypoint=["/not-executed"],
            network_disabled=True,
            labels={"evalhive.purpose": "runtime-bundle-target-probe"},
        )
        try:
            chunks, _ = source.get_archive("/evalhive-runtime")
        except NotFound:
            return
        else:
            if hasattr(chunks, "close"):
                chunks.close()
            raise ManagedRuntimeError("Task image already uses the reserved runtime bundle path")
    except DockerException:
        raise ManagedRuntimeError("Runtime bundle target could not be inspected") from None
    finally:
        if source is not None:
            source.remove(force=True, v=True)


def install_bundle(runtime, connector: dict, root_mapping) -> RuntimeBundle:
    """Server installation helper; image references come only from the catalog."""
    declaration = connector.get("requirements", {}).get("runtime_bundle")
    if not declaration or declaration.get("path") != "/bundle":
        raise ManagedRuntimeError("Connector has no registered in-environment runtime bundle")
    image_id = runtime.resolve_image(declaration["image"])["image_id"]
    root = Path(root_mapping.local)
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() != root:
        raise ManagedRuntimeError("Runtime bundle installation root cannot traverse links")
    destination = root / image_id.removeprefix("sha256:")
    if destination.exists() and destination.stat().st_mode & 0o055 != 0o055:
        # Historical private roots remain byte-for-byte pinned for old results.
        # New task users receive a separately installed, traversable copy.
        destination = root / (destination.name + ".readable-v1")
    if not destination.exists():
        temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
        source = None
        try:
            source = runtime.client.containers.create(
                image_id,
                entrypoint=["/not-executed"],
                network_disabled=True,
                labels={"evalhive.purpose": "runtime-bundle-installation"},
            )
            chunks, _ = source.get_archive("/bundle")
            unpack_bundle(chunks, temporary)
            validate_bundle(temporary, connector)
            temporary.chmod(0o755)
            try:
                os.rename(temporary, destination)
            except FileExistsError:
                pass  # Concurrent installer: validate the winning immutable copy.
        except ManagedRuntimeError:
            raise
        except (DockerException, OSError, tarfile.TarError, ValueError):
            raise ManagedRuntimeError("Runtime bundle installation failed") from None
        finally:
            if source is not None:
                source.remove(force=True, v=True)
            if temporary.exists():
                shutil.rmtree(temporary)
    receipt, fingerprint = validate_bundle(destination, connector)
    return RuntimeBundle(
        image_id,
        fingerprint,
        str(PurePosixPath(root_mapping.host) / destination.name),
        str(destination),
        tuple(receipt["entrypoint"]),
        receipt["connector_id"],
        receipt["connector_version"],
        tuple((tuple(probe["argv"]), probe["stdout"]) for probe in receipt["probes"]),
    )
