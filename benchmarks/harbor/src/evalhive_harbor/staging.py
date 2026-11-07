"""Chunked transfer storage inside the connector's private owned staging service."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from .archive import CHUNK_BYTES, MAX_ARCHIVE_BYTES, TransferError, digest_file, unpack_directory

OUTPUTS = {
    "artifacts": "/outputs/artifacts",
    "checkpoint": "/checkpoint/artifacts",
    "report": "/reports/native-verifier",
}


def operate(root, operation, payload):
    root = Path(root)
    if operation == "initialize":
        root.chmod(0o755)
        for name in ("/logs/verifier", "/logs/artifacts", "/logs/agent"):
            Path(name).mkdir(parents=True, exist_ok=True)
            Path(name).chmod(0o777)
        return {"initialized": True}
    if operation == "restore":
        identity = payload.get("artifact_id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identity):
            raise TransferError("Native checkpoint identity is invalid")
        with tempfile.TemporaryDirectory(prefix="harbor-restore-") as temporary:
            unpack_directory("/evalhive/retained/" + identity + "/payload", temporary)
            captured = Path(temporary) / "checkpoint/artifacts"
            if not captured.is_dir() or captured.is_symlink():
                raise TransferError("Native checkpoint artifact root is missing")
            destination = Path("/restored/artifacts")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(captured, destination, symlinks=True)
        return {"restored": True}
    identity = payload.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise TransferError("Native transfer identity is invalid")
    path = root / (identity + ".tar")
    if operation == "put":
        data = base64.b64decode(payload["data"], validate=True)
        if (
            not 0 < len(data) <= CHUNK_BYTES
            or type(payload["size_bytes"]) is not int
            or len(data) != payload["size_bytes"]
            or hashlib.sha256(data).hexdigest() != payload["sha256"]
        ):
            raise TransferError("Native small archive proof does not match")
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o444)
        return {"sealed": True, "path": str(path)}
    if operation == "create":
        with path.open("xb"):
            pass
        return {"created": True}
    if operation == "remove":
        path.unlink(missing_ok=True)
        return {"removed": True}
    readonly = operation in {"seal", "publish"}
    descriptor = os.open(path, (os.O_RDONLY if readonly else os.O_RDWR) | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb" if readonly else "r+b") as stream:
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
        if operation == "publish":
            destination = Path(OUTPUTS[payload["output"]])
            if destination.exists() or destination.is_symlink():
                raise TransferError("Native output was already published")
            unpack_directory(path, destination)
            return {"published": True}
    raise TransferError("Native transfer operation is unsupported")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["create", "write", "seal", "put", "remove", "publish", "initialize", "restore"])
    parser.add_argument("payload")
    parser.add_argument("--root", default="/evalhive-stage")
    args = parser.parse_args()
    print(json.dumps(operate(args.root, args.operation, json.loads(args.payload))))


if __name__ == "__main__":
    main()
