"""Bounded private evidence storage in a separately owned recorder container."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path

ROOT = Path("/records")
RETAINED = Path("/evalhive/retained")
MAX_BYTES = 8 * 1024**2
CHUNK = 24 * 1024
NAMES = {"trace", "checkpoint", "report"}


def path(name):
    if name not in NAMES:
        raise ValueError("Private evidence name is invalid")
    return ROOT / name / "record.json"


def read_file(target):
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Private evidence exceeds its regular-file budget")
        data = stream.read(MAX_BYTES + 1)
    if len(data) != info.st_size:
        raise ValueError("Private evidence changed during transfer")
    return data


def execute(operation, payload):
    if operation == "init":
        for name in NAMES:
            path(name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return {"initialized": True}
    if operation == "write":
        target = path(payload["name"])
        data = base64.b64decode(payload["data"], validate=True)
        offset = payload["offset"]
        if (
            type(offset) is not int
            or offset < 0
            or len(data) > CHUNK
            or offset + len(data) > MAX_BYTES
        ):
            raise ValueError("Private evidence exceeds its transfer budget")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            if os.fstat(stream.fileno()).st_size != offset:
                raise ValueError("Private evidence chunk is out of order")
            stream.seek(offset)
            stream.write(data)
        return {"size_bytes": offset + len(data)}
    if operation == "seal":
        data = read_file(path(payload["name"]))
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != payload["size_bytes"] or digest != payload["sha256"]:
            raise ValueError("Private evidence transfer was incomplete")
        json.loads(data)
        return {"sealed": True}
    if operation == "restore":
        identity = payload["artifact_id"]
        if not isinstance(identity, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identity
        ):
            raise ValueError("Retained evidence identity is invalid")
        archive = RETAINED / identity / "payload"
        descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES + 1024**2:
                raise ValueError("Retained evidence archive exceeds its budget")
            with tarfile.open(fileobj=source, mode="r:*") as stream:
                members = []
                for member in stream:
                    members.append(member)
                    if len(members) > 8:
                        raise ValueError("Retained evidence archive has unexpected members")
                files = [member for member in members if not member.isdir()]
                if len(members) > 8 or len(files) != 1:
                    raise ValueError("Retained evidence archive has unexpected members")
                member = files[0]
                if (
                    member.name not in {"record.json", "checkpoint/record.json"}
                    or not member.isfile()
                    or member.size > MAX_BYTES
                ):
                    raise ValueError("Retained evidence member is invalid")
                data = stream.extractfile(member).read(MAX_BYTES + 1)
                if len(data) != member.size:
                    raise ValueError("Retained evidence member is incomplete")
        json.loads(data)
        target = path("checkpoint")
        with target.open("xb") as output:
            output.write(data)
        return {
            "restored": True,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    if operation == "read":
        data = read_file(path(payload["name"]))
        offset = payload["offset"]
        if type(offset) is not int or not 0 <= offset <= len(data):
            raise ValueError("Private evidence read offset is invalid")
        return {
            "data": base64.b64encode(data[offset : offset + CHUNK]).decode(),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    raise ValueError("Private evidence operation is unsupported")


if __name__ == "__main__":
    operation, encoded = sys.argv[1:]
    payload = json.loads(base64.b64decode(encoded, validate=True))
    print(json.dumps(execute(operation, payload), separators=(",", ":")))
