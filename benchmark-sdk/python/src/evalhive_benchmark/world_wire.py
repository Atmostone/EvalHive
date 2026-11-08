"""Standalone standard-library transport for private native service control."""

from __future__ import annotations

import base64
import json
import socket
import sys
import time
from pathlib import Path


def monotonic():
    """Keep host deadlines independent of native simulated clocks."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


MAX_BYTES = 1024**2


class WorldError(ValueError):
    pass


def encoded(value):
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    if len(data) > MAX_BYTES:
        raise WorldError("Native operation exceeds its byte budget")
    return data


SOCKET = Path("/control/world.sock")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorldError("Native control contains duplicate fields")
        result[key] = value
    return result


def decode(data):
    if not data.endswith(b"\n") or len(data) > MAX_BYTES + 1:
        raise WorldError("Native control frame exceeds its budget")
    value = json.loads(data, object_pairs_hook=_object)
    encoded(value)
    return value


def exchange(request, path=SOCKET, *, timeout=10):
    frame = encoded(request) + b"\n"
    deadline = monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Native control did not become ready")
            connection.settimeout(remaining)
            try:
                connection.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                # A running container has not necessarily bound its application socket.
                # No request bytes have been sent, so waiting cannot repeat an effect.
                time.sleep(min(0.05, remaining))
        connection.settimeout(max(0.001, deadline - monotonic()))
        connection.sendall(frame)
        with connection.makefile("rb") as stream:
            response = decode(stream.readline(MAX_BYTES + 2))
    if set(response) != {"ok", "result"} or type(response["ok"]) is not bool:
        raise WorldError("Native control receipt is invalid")
    if not response["ok"]:
        raise WorldError("Native control rejected the request")
    return response["result"]


def main():
    try:
        if len(sys.argv) != 2:
            raise WorldError("Native control requires one encoded request")
        request = json.loads(
            base64.b64decode(sys.argv[1], validate=True), object_pairs_hook=_object
        )
        result = exchange(request)
        sys.stdout.buffer.write(encoded(result) + b"\n")
    except Exception as error:
        sys.stderr.write("Native control request failed (" + type(error).__name__ + ")\n")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
