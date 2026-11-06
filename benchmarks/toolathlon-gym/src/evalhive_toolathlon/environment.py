"""Private native environment helpers, executed only inside owned containers."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def portals(urls):
    checks = []
    for url in urls:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("Portal probes require explicit local HTTP resources")
        try:
            with build_opener(NoRedirect()).open(Request(url, method="GET"), timeout=1) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
        except (URLError, TimeoutError, ConnectionError):
            status = None
        checks.append(
            {"url": url, "status": status, "ready": status is not None and 200 <= status < 300}
        )
    return {"ready": all(check["ready"] for check in checks), "checks": checks}


def database_ready():
    import psycopg2

    try:
        with psycopg2.connect(connect_timeout=1) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                ready = cursor.fetchone() == (1,)
        return {"ready": ready}
    except psycopg2.Error:
        return {"ready": False}


def extract_native(path, root):
    """No host extraction; reject entries that could leave this native directory."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "r:") as archive:
        members = []
        for index, member in enumerate(archive):
            name = PurePosixPath(member.name)
            if (
                index >= 65536
                or name.is_absolute()
                or ".." in name.parts
                or not member.isfile()
                and not member.isdir()
            ):
                raise ValueError("Native archive contains unsupported state entries")
            members.append(member)
        # Validate the complete index before writing. extractall applies directory
        # permissions after children, including captured read-only directories.
        archive.extractall(root, members=members, filter="data")


def initialize(source, expected_digest):
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream:
        while data := stream.read(1024**2):
            digest.update(data)
    if "sha256:" + digest.hexdigest() != expected_digest:
        raise ValueError("Native installed source digest changed")
    if Path("/gym").exists():
        raise ValueError("Native source initialization cannot be repeated")
    extract_native(source, "/gym")
    Path("/reports").mkdir()
    return {"initialized": True}


def restore_tree(archive, root):
    """Replace captured contents, including deletions, without removing a mount root."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Native restore root cannot be a link")
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    extract_native(archive, root)


def task_root(identity):
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", identity):
        raise ValueError("Native task identity is invalid")
    return Path("/gym/tasks/finalpool") / identity


def reset(identity):
    with gzip.open("/gym/db/init.sql.gz", "rb") as source:
        with Path("/reports/reset.stderr").open("wb") as errors:
            process = subprocess.Popen(
                ["psql", "-v", "ON_ERROR_STOP=1", "--no-password"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=errors,
            )
            try:
                shutil.copyfileobj(source, process.stdin, length=1024**2)
                process.stdin.close()
                if process.wait(timeout=60) != 0:
                    raise ValueError("Native database reset failed")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    # Match the upstream runner's deletion semantics for email task resets.
    import psycopg2

    with psycopg2.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE email.sent_log DROP CONSTRAINT IF EXISTS sent_log_message_id_fkey"
            )
            cursor.execute(
                "ALTER TABLE email.sent_log ADD CONSTRAINT sent_log_message_id_fkey FOREIGN KEY (message_id) REFERENCES email.messages(id) ON DELETE CASCADE"
            )
    initial = task_root(identity) / "initial_workspace"
    if initial.is_dir():
        shutil.copytree(initial, "/workspace", dirs_exist_ok=True)
    return {"reset": True}


def portal_service(argv, identity):
    """Only the native read-only Python file server has a supported restart recipe."""
    if len(argv) != 6 or argv[1:3] != ["-m", "http.server"] or argv[4] != "--directory":
        return None
    if Path(argv[0]).name not in {"python", "python3", "python3.12"}:
        return None
    if not argv[3].isdigit() or not 1 <= int(argv[3]) <= 65535:
        return None
    directory = Path(argv[5])
    for name, root in (("task", task_root(identity)), ("workspace", Path("/workspace"))):
        if directory.is_absolute() and directory.is_relative_to(root):
            relative = directory.relative_to(root).as_posix()
            if ".." not in directory.parts and directory.resolve(strict=True) == directory:
                return {"kind": "python_http", "port": int(argv[3]), "root": name, "path": relative}
    return None


def native_services(identity, *, proc=Path("/proc")):
    services = []
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            argv = (directory / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
            if not argv or not argv[0] or int(directory.name) == os.getpid():
                continue
            if argv[0] in {
                "/sbin/docker-init",
                "/usr/bin/docker-init",
                "/evalhive-runtime/supervisor",
            }:
                continue
            if argv[1:] == ["-c", "import time;time.sleep(86400)"] and Path(
                argv[0]
            ).name.startswith("python"):
                continue
            service = portal_service(argv, identity)
        except (FileNotFoundError, ProcessLookupError):
            continue
        if service is None or len(services) >= 32:
            return None
        services.append(service)
    return services if len({value["port"] for value in services}) == len(services) else None


def restore_services(identity, saved, *, reports=Path("/reports")):
    if not isinstance(saved, list) or len(saved) > 32:
        raise ValueError("Native service checkpoint is invalid")
    ports = set()
    for service in saved:
        if (
            not isinstance(service, dict)
            or set(service) != {"kind", "port", "root", "path"}
            or service["kind"] != "python_http"
            or type(service["port"]) is not int
            or not 1 <= service["port"] <= 65535
            or service["port"] in ports
            or service["root"] not in {"task", "workspace"}
            or not isinstance(service["path"], str)
        ):
            raise ValueError("Native service checkpoint is unsupported")
        root = task_root(identity) if service["root"] == "task" else Path("/workspace")
        path = PurePosixPath(service["path"])
        if path.is_absolute() or ".." in path.parts or path.as_posix() != service["path"]:
            raise ValueError("Native service path escapes its captured state")
        directory = root / service["path"]
        if not directory.is_dir() or directory.resolve(strict=True) != directory:
            raise ValueError("Native service state is unavailable")
        ports.add(service["port"])
        with (reports / f"portal-{service['port']}.restore.log").open("ab") as output:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "http.server",
                    str(service["port"]),
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    str(directory),
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )


def snapshot(identity):
    services = native_services(identity)
    if services is None:
        return {"checkpoint_complete": False, "reason": "UNSUPPORTED_NATIVE_SERVICES"}
    destination = Path("/checkpoint")
    destination.mkdir(exist_ok=False)
    (destination / "services.json").write_text(json.dumps(services) + "\n")
    with (destination / "dump.stderr").open("wb") as errors:
        subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--no-password",
                "--file",
                str(destination / "db.dump"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            timeout=60,
        )
    for output, source in (
        ("workspace.tar", Path("/workspace")),
        ("task.tar", task_root(identity)),
    ):
        with tarfile.open(destination / output, "w") as archive:
            archive.add(source, arcname=".", recursive=True)
        with tarfile.open(destination / output, "r:") as archive:
            if any(not member.isfile() and not member.isdir() for member in archive):
                return {"checkpoint_complete": False, "reason": "UNSUPPORTED_NATIVE_STATE"}
    return {"checkpoint_complete": True}


def restore(identity, checkpoint):
    root = Path("/restored")
    if root.exists():
        raise ValueError("Native state restore cannot be repeated")
    extract_native(checkpoint, root)
    saved = root / "checkpoint"
    if not (saved / "db.dump").is_file():
        raise ValueError("Native database snapshot is missing")
    with Path("/reports/restore.stderr").open("wb") as errors:
        subprocess.run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--no-password",
                "--dbname",
                os.environ["PGDATABASE"],
                str(saved / "db.dump"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=errors,
            timeout=60,
        )
    restore_tree(saved / "workspace.tar", "/workspace")
    restore_tree(saved / "task.tar", task_root(identity))
    service_file = saved / "services.json"
    if not service_file.is_file() or service_file.stat().st_size > 65536:
        raise ValueError("Native service checkpoint is unavailable")
    restore_services(identity, json.loads(service_file.read_text()))
    return {"restored": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=["initialize", "database_ready", "portals", "reset", "snapshot", "restore"],
    )
    parser.add_argument("--source", default="/native/native-source.tar")
    parser.add_argument("--source-digest")
    parser.add_argument("--task")
    parser.add_argument("--urls", default="[]")
    parser.add_argument("--checkpoint")
    args = parser.parse_args()
    if args.operation == "initialize":
        result = initialize(args.source, args.source_digest)
    elif args.operation == "database_ready":
        result = database_ready()
    elif args.operation == "portals":
        result = portals(json.loads(args.urls))
    elif args.operation == "reset":
        result = reset(args.task)
    elif args.operation == "snapshot":
        result = snapshot(args.task)
    else:
        result = restore(args.task, args.checkpoint)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
