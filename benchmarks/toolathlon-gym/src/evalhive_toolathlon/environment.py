"""Private native environment helpers, executed only inside owned containers."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
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
            archive.extract(member, root, filter="data")


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


def snapshot(identity):
    destination = Path("/checkpoint")
    destination.mkdir(exist_ok=False)
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
    extract_native(saved / "workspace.tar", "/workspace")
    extract_native(saved / "task.tar", task_root(identity))
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
