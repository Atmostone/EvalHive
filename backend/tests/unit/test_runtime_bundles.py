"""Registered artifact extraction never admits host paths or filesystem links."""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.runtime.bundles import check_mount_target, install_bundle, unpack_bundle, validate_bundle
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping
from docker.errors import NotFound


def receipt(root):
    (root / "node").write_bytes(b"native-fixture")
    (root / "node").chmod(0o555)
    (root / "supervisor").write_bytes(b"helper-fixture")
    (root / "supervisor").chmod(0o555)
    files = {
        path.name: {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
            "executable": True,
        }
        for path in root.iterdir()
    }
    value = {
        "bundle_version": "1.0",
        "connector_id": "example",
        "connector_version": "1.0",
        "platform": {"os": "linux", "architecture": "amd64", "libc": "glibc"},
        "mount_path": "/evalhive-runtime",
        "entrypoint": ["/evalhive-runtime/node"],
        "probes": [{"argv": ["/evalhive-runtime/node", "--version"], "stdout": "fixture\n"}],
        "files": files,
    }
    (root / "bundle.json").write_text(json.dumps(value))
    return value


def archive(*members):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "/outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
            info.size = 0
            output.addfile(info, io.BytesIO())
    value = stream.getvalue()
    return (value[index : index + 17] for index in range(0, len(value), 17))


def test_registered_files_and_exact_receipt_are_verified(tmp_path):
    value = receipt(tmp_path)
    checked, fingerprint = validate_bundle(tmp_path, {"id": "example", "version": "1.0"})
    assert checked == value and len(fingerprint) == 64
    validate_bundle(tmp_path, {"id": "example", "version": "1.0"}, expected_hash=fingerprint)
    with pytest.raises(ManagedRuntimeError, match="changed"):
        validate_bundle(tmp_path, {"id": "example", "version": "1.0"}, expected_hash="f" * 64)


def test_new_bundle_is_traversable_without_mutating_historical_private_roots(tmp_path):
    root, source = tmp_path / "installed", tmp_path / "source"
    root.mkdir()
    source.mkdir()
    receipt(source)
    image = "sha256:" + "a" * 64
    historical = root / image.removeprefix("sha256:")
    historical.mkdir(mode=0o700)
    receipt(historical)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as output:
        output.add(source, arcname="bundle")
    runtime = MagicMock()
    runtime.resolve_image.return_value = {"image_id": image}
    native = runtime.client.containers.create.return_value
    native.get_archive.return_value = ([buffer.getvalue()], {})
    connector = {"id": "example", "version": "1.0", "requirements": {"runtime_bundle": {"image": image, "path": "/bundle"}}}
    installed = install_bundle(runtime, connector, PathMapping("/registered", str(root)))
    assert historical.stat().st_mode & 0o777 == 0o700
    assert installed.local_path.endswith(".readable-v1")
    assert Path(installed.local_path).stat().st_mode & 0o777 == 0o755
    again = install_bundle(runtime, connector, PathMapping("/registered", str(root)))
    assert again == installed
    runtime.client.containers.create.assert_called_once()


@pytest.mark.parametrize(
    "change", ["modified", "extra", "missing", "symlink", "identity", "entrypoint", "mode"]
)
def test_file_drift_and_escaping_entrypoints_are_rejected(tmp_path, change):
    value = receipt(tmp_path)
    if change == "modified":
        (tmp_path / "node").chmod(0o755)
        (tmp_path / "node").write_bytes(b"changed")
    elif change == "extra":
        (tmp_path / "extra").touch()
    elif change == "missing":
        (tmp_path / "node").unlink()
    elif change == "symlink":
        (tmp_path / "node").unlink()
        (tmp_path / "node").symlink_to("/etc/hostname")
    elif change == "identity":
        value["connector_id"] = "foreign"
    elif change == "entrypoint":
        value["entrypoint"] = ["/evalhive-runtime/../outside"]
    elif change == "mode":
        (tmp_path / "node").chmod(0o444)
    (tmp_path / "bundle.json").write_text(json.dumps(value))
    with pytest.raises(ManagedRuntimeError):
        validate_bundle(tmp_path, {"id": "example", "version": "1.0"})


@pytest.mark.parametrize(
    "name,kind",
    [
        ("/bundle/absolute", tarfile.REGTYPE),
        ("bundle/../outside", tarfile.REGTYPE),
        ("elsewhere/file", tarfile.REGTYPE),
        ("bundle/link", tarfile.SYMTYPE),
        ("bundle/link", tarfile.LNKTYPE),
        ("bundle/socket", tarfile.FIFOTYPE),
    ],
)
def test_archive_members_cannot_escape_or_link_to_the_host(tmp_path, name, kind):
    with pytest.raises(ManagedRuntimeError):
        unpack_bundle(archive((name, kind)), tmp_path)


def test_archive_directories_are_supported_and_duplicates_rejected(tmp_path):
    unpack_bundle(
        archive(
            ("bundle/", tarfile.DIRTYPE),
            ("bundle/sub/", tarfile.DIRTYPE),
            ("bundle/sub/file", tarfile.REGTYPE),
        ),
        tmp_path,
    )
    assert (tmp_path / "sub/file").is_file()
    assert not (tmp_path / "bundle").exists()
    with pytest.raises(ManagedRuntimeError, match="duplicate"):
        unpack_bundle(
            archive(("bundle/sub", tarfile.DIRTYPE), ("bundle/sub", tarfile.DIRTYPE)), tmp_path
        )


def test_truncated_tar_input_is_never_silently_installed(tmp_path):
    info = tarfile.TarInfo("bundle/missing")
    info.size = 1000
    with pytest.raises(tarfile.TarError):
        unpack_bundle([info.tobuf()], tmp_path)
    assert (tmp_path / "missing").stat().st_size == 0


def test_task_image_mount_collision_is_detected_without_starting_or_reading_files():
    runtime = MagicMock()
    runtime.resolve_image.return_value = {"image_id": "sha256:fixture"}
    source = runtime.client.containers.create.return_value
    chunks = MagicMock()
    source.get_archive.return_value = (chunks, {})
    with pytest.raises(ManagedRuntimeError, match="reserved"):
        check_mount_target(runtime, "sha256:fixture")
    chunks.close.assert_called_once()
    source.start.assert_not_called()
    source.remove.assert_called_once_with(force=True, v=True)
    source.get_archive.side_effect = NotFound("missing path")
    check_mount_target(runtime, "sha256:fixture")
    runtime.resolve_image.return_value = {"image_id": "different-image"}
    with pytest.raises(ManagedRuntimeError, match="immutable"):
        check_mount_target(runtime, "floating-tag")


@pytest.mark.asyncio
async def test_probe_cleanup_uncertainty_cannot_report_compatibility(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.runtime import bundle_probe
    from app.runtime.managed_spec import ManagedContainer, OwnerScope, UncertainRuntimeError

    scope = OwnerScope(
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
        1,
    )
    container = ManagedContainer(
        scope, "00000000-0000-0000-0000-000000000004", "container", "image"
    )
    runtime, owned = MagicMock(), MagicMock()
    runtime.container.return_value.attrs = {
        "Mounts": [{"Destination": "/evalhive-runtime", "Source": "/host/bundle", "RW": False}]
    }
    bundle = SimpleNamespace(host_path="/host/bundle", probes=())
    process = SimpleNamespace(
        stdout=SimpleNamespace(readline=AsyncMock(side_effect=[b"glibc 2.36\n", b""])),
        wait=AsyncMock(return_value=0),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(bundle_probe, "OwnedExecRuntime", lambda _: owned)
    monkeypatch.setattr(bundle_probe.OwnedDockerProcess, "start", AsyncMock(return_value=process))
    owned.control.side_effect = UncertainRuntimeError("Control response lost")
    with pytest.raises(UncertainRuntimeError, match="cleanup"):
        await bundle_probe.check_bundle(
            runtime,
            container,
            bundle,
            ["00000000-0000-0000-0000-000000000005", "00000000-0000-0000-0000-000000000006"],
        )
    process.aclose.assert_awaited_once()
