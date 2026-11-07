"""Standalone evidence transport inside a fresh, evaluator-only native image."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import sys
from pathlib import Path

if __package__:
    from .task_transfer import CHUNK, regular
else:
    from task_transfer import CHUNK, regular

MAX_LOG_BYTES = 64 * 1024**2
ROOT, STAGE = Path("/evalhive-results"), Path("/stage")


def execute(operation, argument=None, *, root=ROOT, stage=STAGE, patch=Path("/tmp/patch.diff")):
    if operation == "prepare":
        if not isinstance(argument, str) or not re.fullmatch(r"[a-f0-9]{64}", argument):
            raise ValueError("Native patch digest is invalid")
        data = regular(stage / "patch.diff")
        if hashlib.sha256(data).hexdigest() != argument:
            raise ValueError("Native evaluator received another patch")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.absolute() != root.resolve(strict=True) or any(root.iterdir()):
            raise ValueError("Native evaluator requires fresh private log storage")
        descriptor = os.open(patch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        return {"prepared": True, "patch_sha256": argument}
    if operation == "test" and argument is None:

        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_LOG_BYTES, MAX_LOG_BYTES))

        path = root / "test_output.txt"
        with path.open("xb") as output:
            process = subprocess.Popen(
                ["/bin/bash", str(stage / "eval.sh")],
                cwd="/testbed",
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                preexec_fn=limit,
                start_new_session=True,
            )
            try:
                status = process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise ValueError("Native evaluator exceeded its deadline") from None
        if status < 0:
            raise ValueError("Native evaluator was interrupted before complete evidence")
        data = regular(path, maximum=MAX_LOG_BYTES)
        proof = {
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "exit_code": status,
        }
        with (root / "receipt.json").open("x") as stream:
            json.dump(proof, stream, sort_keys=True)
        return proof
    if operation not in {"proof", "read"} or operation == "proof" and argument is not None:
        raise ValueError("Native evaluator transfer operation is unavailable")
    proof = json.loads(regular(root / "receipt.json", maximum=4096))
    data = regular(root / "test_output.txt", maximum=MAX_LOG_BYTES)
    if len(data) != proof["size_bytes"] or hashlib.sha256(data).hexdigest() != proof["sha256"]:
        raise ValueError("Native test evidence changed after evaluation")
    if operation == "proof":
        return proof
    if not isinstance(argument, str) or not argument.isdecimal() or len(argument) > 10:
        raise ValueError("Native evidence offset is invalid")
    offset = int(argument)
    if not 0 <= offset <= len(data):
        raise ValueError("Native evidence chunk leaves its sealed log")
    return {"offset": offset, "data": base64.b64encode(data[offset : offset + CHUNK]).decode()}


def main():
    try:
        if not 2 <= len(sys.argv) <= 3:
            raise ValueError("Native evaluator transfer requires an operation")
        result = execute(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else None)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except Exception as error:
        sys.stderr.write("Native evaluation transfer failed (" + type(error).__name__ + ")\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
