"""Standalone bounded patch capture for an agent environment with no evaluator data."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

MAX_BYTES, CHUNK = 8 * 1024**2, 24 * 1024
ROOT = Path("/evalhive-capture")


def regular(path, *, maximum=MAX_BYTES):
    if path.absolute() != path.resolve(strict=True):
        raise ValueError("Patch evidence must not traverse links")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ValueError("Patch evidence exceeds its regular-file budget")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or any(
        getattr(before, key) != getattr(after, key)
        for key in ("st_ino", "st_dev", "st_size", "st_mtime_ns", "st_ctime_ns")
    ):
        raise ValueError("Patch evidence changed during capture")
    return data


def git(arguments, *, cwd, output):
    with output.open("xb") as stdout, output.with_suffix(".stderr").open("xb") as stderr:
        process = subprocess.Popen(
            [
                "/usr/bin/git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C.UTF-8",
            },
        )
        deadline = time.monotonic() + 90
        try:
            # Bound exported evidence, not Git's internal files. RLIMIT_FSIZE
            # also limits overlay copy-up of existing repository pack files.
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, (stdout, MAX_BYTES))
                selector.register(process.stderr, selectors.EVENT_READ, (stderr, 64 * 1024))
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("Patch capture exceeded its deadline")
                    for key, _ in selector.select(min(remaining, 0.2)):
                        data = os.read(key.fileobj.fileno(), 64 * 1024)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        stream, maximum = key.data
                        if stream.tell() + len(data) > maximum:
                            raise ValueError("Patch capture exceeds its evidence byte budget")
                        stream.write(data)
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()
    if status != 0:
        raise ValueError("Patch capture did not complete; no empty patch is inferred")


def execute(operation, payload, *, root=ROOT, cwd=Path("/testbed")):
    if not isinstance(payload, dict) or not re.fullmatch(r"[a-f0-9]{32}", payload.get("id", "")):
        raise ValueError("Patch capture identity is invalid")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.absolute() != root.resolve(strict=True):
        raise ValueError("Patch capture requires a directory without links")
    target = root / payload["id"]
    if operation == "capture":
        if set(payload) != {"id", "base_commit"} or not re.fullmatch(
            r"[a-f0-9]{40}", payload["base_commit"]
        ):
            raise ValueError("Patch capture requires its frozen base commit")
        if not target.exists():
            target.mkdir(mode=0o700)
            git(["add", "--intent-to-add", "--", "."], cwd=cwd, output=target / "index.log")
            git(
                [
                    "diff",
                    "--binary",
                    "--full-index",
                    "--no-ext-diff",
                    "--no-textconv",
                    payload["base_commit"],
                    "--",
                ],
                cwd=cwd,
                output=target / "patch.diff",
            )
            data = regular(target / "patch.diff")
            data.decode("utf-8")
            proof = {
                "id": payload["id"],
                "base_commit": payload["base_commit"],
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            with (target / "receipt.json").open("x") as stream:
                json.dump(proof, stream, sort_keys=True)
        proof = json.loads(regular(target / "receipt.json", maximum=4096))
        data = regular(target / "patch.diff")
        if proof != {
            **payload,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }:
            raise ValueError("Patch capture receipt conflicts with frozen evidence")
        return proof
    if operation == "read":
        if set(payload) != {"id", "offset"} or type(payload["offset"]) is not int:
            raise ValueError("Patch chunk request is invalid")
        proof = json.loads(regular(target / "receipt.json", maximum=4096))
        data = regular(target / "patch.diff")
        if len(data) != proof["size_bytes"] or hashlib.sha256(data).hexdigest() != proof["sha256"]:
            raise ValueError("Patch evidence changed after capture")
        offset = payload["offset"]
        if not 0 <= offset <= len(data):
            raise ValueError("Patch chunk leaves its frozen evidence")
        return {"offset": offset, "data": base64.b64encode(data[offset : offset + CHUNK]).decode()}
    raise ValueError("Patch transfer operation is unavailable")


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("Patch transfer requires an operation and payload")
        payload = json.loads(base64.b64decode(sys.argv[2], validate=True))
        print(json.dumps(execute(sys.argv[1], payload), sort_keys=True, separators=(",", ":")))
    except Exception as error:
        sys.stderr.write("Patch transfer failed (" + type(error).__name__ + ")\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
