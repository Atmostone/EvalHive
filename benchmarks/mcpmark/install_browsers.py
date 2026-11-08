"""Install only locked browser archives into an offline image build."""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile


def install(archives, target, lock):
    target.mkdir(parents=True, exist_ok=True)
    for artifact in lock["artifacts"]:
        path = archives / artifact["file"]
        data = path.read_bytes()
        if (
            len(data) != artifact["size_bytes"]
            or hashlib.sha256(data).hexdigest() != artifact["sha256"]
        ):
            raise ValueError("Browser archive differs from its installed lock")
        destination = target / artifact["name"]
        destination.mkdir()
        with ZipFile(path) as archive:
            total = 0
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                total += member.file_size
                if relative.is_absolute() or ".." in relative.parts or total > 2 * 1024**3:
                    raise ValueError("Browser archive exceeds its installation boundary")
                output = destination.joinpath(*relative.parts)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("Browser archives must not install symbolic links")
                archive.extract(member, destination)
                if output.is_file():
                    output.chmod(0o755 if mode & 0o111 else 0o644)
        (destination / "INSTALLATION_COMPLETE").touch()
    target.chmod(0o755)


if __name__ == "__main__":
    if os.uname().machine != "x86_64":
        raise SystemExit("The installed browser lock requires linux amd64")
    install(Path(sys.argv[1]), Path(sys.argv[2]), json.loads(Path(sys.argv[3]).read_text()))
