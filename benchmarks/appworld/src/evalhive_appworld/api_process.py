"""Supervise the private native API subprocess without discovering user settings."""

from __future__ import annotations

import http.client
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .api_gate import monotonic
from .api_service import PRIVATE_PORT


class ApiProcess:
    def __init__(self, records):
        self.records = Path(records)
        self.process, self.log = None, None

    def control(self, operation, *, timeout=25):
        if operation not in {"status", "open", "seal"}:
            raise ValueError("Native API control is not installed")
        connection = http.client.HTTPConnection("127.0.0.1", PRIVATE_PORT, timeout=timeout)
        try:
            connection.request(
                "GET" if operation == "status" else "POST", "/.__evalhive/" + operation, body=b""
            )
            response = connection.getresponse()
            raw = response.read(16385)
            if response.status != 200 or len(raw) > 16384:
                raise RuntimeError("Native API control was not confirmed")
            result = json.loads(raw)
            expected = {"open": {"accepting": True}, "seal": {"sealed": True}}
            if operation == "status":
                if (
                    not isinstance(result, dict)
                    or set(result) != {"ready", "accepting"}
                    or result["ready"] is not True
                    or type(result["accepting"]) is not bool
                ):
                    raise RuntimeError("Native API readiness receipt is invalid")
            elif result != expected[operation]:
                raise RuntimeError("Native API control receipt is invalid")
            return result
        finally:
            connection.close()

    def start(self, *, timeout=90):
        if self.process is not None:
            raise RuntimeError("One trial cannot replace its native API process")
        self.records.mkdir(parents=True, mode=0o700, exist_ok=True)
        descriptor = os.open(
            self.records / "api-service.log",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        self.log = os.fdopen(descriptor, "wb")
        self.process = subprocess.Popen(
            [sys.executable, "-m", "evalhive_appworld.api_service"],
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "APPWORLD_CACHE": "/opt/appworld-installed-cache"},
        )
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Native API process exited before readiness")
            try:
                return self.control("status", timeout=min(1, max(0.01, deadline - monotonic())))
            except (OSError, http.client.HTTPException):
                time.sleep(min(0.1, max(0, deadline - monotonic())))
        raise TimeoutError("Native API process did not become ready")

    def close(self):
        try:
            if self.process is not None and self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
        finally:
            if self.log is not None:
                self.log.close()
