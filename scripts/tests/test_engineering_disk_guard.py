"""Verify the cleanup boundary without accessing any Docker daemon."""

import importlib.util
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

GUARD = Path(__file__).resolve().parents[1] / "engineering-disk-guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("engineering_disk_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def disk(free_gib):
    return SimpleNamespace(f_blocks=100, f_bavail=free_gib, f_frsize=1024**3)


def invoke(reports, *, minimum=3, owner=11001, mode=stat.S_IFSOCK):
    module = load_guard()
    with (
        patch.object(module.os, "statvfs", side_effect=reports),
        patch.object(module.os, "getuid", return_value=11001),
        patch.object(module.Path, "stat", return_value=SimpleNamespace(st_mode=mode, st_uid=owner)),
        patch.object(module.shutil, "which", return_value="/usr/bin/docker"),
        patch.object(module.subprocess, "run") as run,
        patch("sys.argv", [str(GUARD), "--min-free-gib", str(minimum)]),
    ):
        module.main()
        return run


def test_below_threshold_does_not_clean():
    assert invoke([disk(6)]).call_count == 0


def test_insufficient_headroom_below_threshold_defers_without_cleaning():
    with pytest.raises(SystemExit, match="cleanup threshold not reached"):
        invoke([disk(6)], minimum=8)


def test_threshold_cleans_only_unused_rootless_build_cache():
    run = invoke([disk(5), disk(7)])
    assert run.call_count == 1
    args = run.call_args.args[0]
    assert args[args.index("--host") + 1] == "unix:///run/user/11001/docker.sock"
    assert args[-4:] == ["builder", "prune", "--all", "--force"]
    assert not {"image", "volume", "container", "system", "--keep-storage", "--filter"} & set(args)
    environment = run.call_args.kwargs["env"]
    assert environment["HOME"] == environment["DOCKER_CONFIG"]
    assert environment["HOME"].startswith("/tmp/evalhive-disk-guard-")


def test_cleanup_that_does_not_recover_space_stops_builds():
    with pytest.raises(SystemExit, match="Insufficient disk headroom"):
        invoke([disk(4), disk(4)])


@pytest.mark.parametrize("owner,mode", [(0, stat.S_IFSOCK), (11001, stat.S_IFREG)])
def test_wrong_socket_is_rejected_before_cleanup(owner, mode):
    with pytest.raises(SystemExit, match="rootless Docker socket"):
        invoke([disk(4)], owner=owner, mode=mode)
