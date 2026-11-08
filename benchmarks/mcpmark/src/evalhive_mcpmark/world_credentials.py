"""Receive a scoped private input without placing its value in argv or receipts."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path("/control")
PG_SECRET = Path("/postgres-secret")
MAX_BYTES = 64 * 1024


def private_file(path, data, mode):
    if path.exists():
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_BYTES
                or stream.read(MAX_BYTES + 1) != data
            ):
                raise ValueError("Native private configuration is frozen")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def configure(data, *, root=ROOT, pg_secret=PG_SECRET):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValueError("Native private configuration exceeds its budget")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Native private service configuration is invalid")
    postgres = value.get("service") == "postgres"
    if postgres and (
        set(value) != {"service", "password"}
        or not isinstance(value["password"], str)
        or not value["password"]
        or len(value["password"].encode()) > 1024
        or any(char in value["password"] for char in "\0\r\n")
    ):
        raise ValueError("Native private PostgreSQL configuration is invalid")
    if not postgres:
        if set(value) != {"service", "credentials"} or value["service"] not in {"github", "notion"}:
            raise ValueError("Native private service configuration is invalid")
        from .external_state import validate_credentials

        validate_credentials(value["service"], value["credentials"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve() != root or root.stat().st_mode & 0o077:
        raise ValueError("Native private configuration requires a private directory")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if postgres:
        pg_secret.mkdir(mode=0o755, parents=True, exist_ok=True)
        if pg_secret.resolve() != pg_secret:
            raise ValueError("Native PostgreSQL secret directory cannot traverse links")
        # Only the native world and its dedicated PostgreSQL service mount this
        # volume. The unprivileged PostgreSQL entrypoint must be able to read it.
        private_file(pg_secret / "password", value["password"].encode(), 0o444)
    private_file(root / "credentials.json", canonical, 0o600)
    return {"configured": True}


def read(root=ROOT):
    descriptor = os.open(root / "credentials.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Native private configuration exceeds its regular-file budget")
        return json.loads(stream.read(MAX_BYTES + 1))


def main():
    try:
        path = os.environ.pop("EVALHIVE_SERVICE_CREDENTIALS_FILE")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                raise ValueError("Native private input exceeds its regular-file budget")
            data = stream.read(MAX_BYTES + 1)
        print(json.dumps(configure(data), separators=(",", ":")))
    except Exception:
        sys.stderr.write("Native private configuration failed\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
