"""Bounded, retryable transfer of confirmed Harness messages into the private world."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path("/records/transcript")
MAX_BYTES, CHUNK = 8 * 1024**2, 24 * 1024


def read(root=ROOT):
    descriptor = os.open(root / "messages.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Native messages exceed their regular-file budget")
        data = stream.read(MAX_BYTES + 1)
    if len(data) != info.st_size:
        raise ValueError("Native message transfer changed")
    return data


def execute(operation, payload, *, root=ROOT):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if operation == "write":
        if set(payload) != {"offset", "data"} or (root / "sealed.json").exists():
            raise ValueError("Native message transfer is frozen")
        data = base64.b64decode(payload["data"], validate=True)
        offset = payload["offset"]
        if (
            type(offset) is not int
            or offset < 0
            or not data
            or len(data) > CHUNK
            or offset + len(data) > MAX_BYTES
        ):
            raise ValueError("Native message chunk exceeds its budget")
        descriptor = os.open(root / "messages.json", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "r+b") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size == offset:
                stream.seek(offset)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            elif size >= offset + len(data):
                stream.seek(offset)
                if stream.read(len(data)) != data:
                    raise ValueError("Native message chunk conflicts with confirmed data")
            else:
                raise ValueError("Native message chunk is out of order")
        return {"through_bytes": offset + len(data)}
    if operation == "seal":
        if set(payload) != {"size_bytes", "sha256"}:
            raise ValueError("Native message receipt is invalid")
        data = read(root)
        proof = {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if proof != payload or not isinstance(json.loads(data), list):
            raise ValueError("Native message transfer is incomplete")
        target = root / "sealed.json"
        if target.exists():
            if json.loads(target.read_text()) != proof:
                raise ValueError("Native message receipt conflicts")
        else:
            with target.open("x") as stream:
                json.dump(proof, stream, sort_keys=True)
        return proof
    raise ValueError("Native message transfer operation is unavailable")


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("Native message transfer requires an operation and payload")
        result = execute(sys.argv[1], json.loads(base64.b64decode(sys.argv[2], validate=True)))
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except Exception:
        sys.stderr.write("Native private message transfer failed\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
