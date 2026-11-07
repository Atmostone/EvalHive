"""Chunked transfer storage inside the connector's private owned staging service."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
from pathlib import Path

from .archive import CHUNK_BYTES, MAX_ARCHIVE_BYTES, TransferError, digest_file


def operate(root, operation, payload):
    root = Path(root)
    identity = payload.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise TransferError("Native transfer identity is invalid")
    path = root / (identity + ".tar")
    if operation == "create":
        with path.open("xb"):
            pass
        return {"created": True}
    if operation == "remove":
        path.unlink(missing_ok=True)
        return {"removed": True}
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise TransferError("Native staging requires a regular archive file")
        size = info.st_size
        if operation == "write":
            data = base64.b64decode(payload["data"], validate=True)
            offset = payload["offset"]
            if type(offset) is not int or offset < 0 or offset > size or not 0 < len(data) <= CHUNK_BYTES:
                raise TransferError("Native transfer chunk is invalid")
            if offset + len(data) > MAX_ARCHIVE_BYTES:
                raise TransferError("Native transfer exceeds its archive budget")
            stream.seek(offset)
            if offset < size:
                if stream.read(len(data)) != data:
                    raise TransferError("Native transfer chunk conflicts with recorded bytes")
            else:
                stream.write(data)
            return {"written": len(data)}
        if operation == "seal":
            if size != payload["size_bytes"] or digest_file(path) != payload["sha256"]:
                raise TransferError("Native transfer archive proof does not match")
            path.chmod(0o444)
            return {"sealed": True, "path": str(path)}
    raise TransferError("Native transfer operation is unsupported")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["create", "write", "seal", "remove"])
    parser.add_argument("payload")
    parser.add_argument("--root", default="/evalhive-stage")
    args = parser.parse_args()
    print(json.dumps(operate(args.root, args.operation, json.loads(args.payload))))


if __name__ == "__main__":
    main()
