#!/usr/bin/env python3
"""Maintain disk headroom for local engineering builds using rootless Docker only."""

import argparse
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

GIB = 1024**3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default="/opt/evalhive")
    parser.add_argument("--min-free-gib", type=float, default=8)
    parser.add_argument("--max-used-percent", type=float, default=90)
    args = parser.parse_args()
    if not 0 < args.min_free_gib or not 0 < args.max_used_percent < 100:
        parser.error("Disk thresholds must leave positive headroom")

    def report():
        fs = os.statvfs(args.path)
        used = (fs.f_blocks - fs.f_bfree) * fs.f_frsize
        available = fs.f_bavail * fs.f_frsize
        percent = 100 * used / (used + available)
        print(f"Disk: {percent:.1f}% used; {available / GIB:.1f} GiB available", flush=True)
        return available >= args.min_free_gib * GIB and percent < args.max_used_percent

    if report():
        return
    socket = Path(f"/run/user/{os.getuid()}/docker.sock")
    info = socket.stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise SystemExit("The current user's rootless Docker socket is required")
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker CLI is unavailable")
    # Only unused build cache is eligible. Images, containers and volumes are retained.
    with tempfile.TemporaryDirectory(prefix="evalhive-disk-guard-") as config:
        subprocess.run(
            [
                docker,
                "--config",
                config,
                "--host",
                "unix://" + str(socket),
                "builder",
                "prune",
                "--all",
                "--force",
                "--filter",
                "until=2h",
                "--keep-storage",
                "2GB",
            ],
            check=True,
            timeout=120,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": config,
                "DOCKER_CONFIG": config,
            },
        )
    if not report():
        raise SystemExit(
            "Insufficient disk headroom: defer large builds and review retained engineering images"
        )


if __name__ == "__main__":
    main()
