"""Expose only native public REST routes and drain their effects before saving."""

from __future__ import annotations

import http.client
import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

MAX_BODY = 8 * 1024**2


def monotonic():
    # AppWorld freezes Python wall and monotonic clocks inside its native world.
    return time.clock_gettime(time.CLOCK_MONOTONIC)


class Gate:
    def __init__(self):
        self.condition = threading.Condition()
        self.accepting, self.public, self.native = False, 0, 0

    def open(self):
        with self.condition:
            self.accepting = True

    @contextmanager
    def enter(self, kind):
        with self.condition:
            if kind == "public" and not self.accepting:
                raise PermissionError("The native task API is sealed")
            setattr(self, kind, getattr(self, kind) + 1)
        try:
            yield
        finally:
            with self.condition:
                setattr(self, kind, getattr(self, kind) - 1)
                self.condition.notify_all()

    def seal(self, *, timeout):
        deadline = monotonic() + timeout
        with self.condition:
            self.accepting = False
            while self.public or self.native:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("Native API effects did not quiesce before persistence")
                self.condition.wait(min(remaining, 0.1))

    def wrap_native(self, app):
        async def measured(scope, receive, send):
            if scope["type"] != "http":
                return await app(scope, receive, send)
            with self.enter("native"):
                return await app(scope, receive, send)

        return measured


class Surface:
    def __init__(self, documents):
        self.routes = []
        for app, entries in documents.items():
            if app in {"admin", "api_docs"} or not re.fullmatch(r"[a-z_]+", app):
                raise ValueError("Native public API documents contain a control application")
            for entry in entries.values():
                path, method = entry["path"], entry["method"].upper()
                if not path.startswith("/" + app + "/") or method not in {
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                }:
                    raise ValueError("Native public API route is invalid")
                parts = re.split(r"(\{[a-zA-Z_][a-zA-Z_0-9]*\})", path)
                pattern = "".join(
                    "[^/]+" if part.startswith("{") else re.escape(part) for part in parts
                )
                self.routes.append((method, re.compile(pattern)))

    def target(self, method, target):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise PermissionError("Native public API requires an origin-relative request")
        path = unquote(parsed.path, errors="strict")
        if (
            not path.startswith("/")
            or "\\" in path
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or path.count("/") != parsed.path.count("/")
            or not any(verb == method and pattern.fullmatch(path) for verb, pattern in self.routes)
        ):
            raise PermissionError("Native public API route is unavailable")
        return quote(path, safe="/:-._~@") + ("?" + parsed.query if parsed.query else "")


def proxy(surface, gate, private_port, *, address=("0.0.0.0", 8080)):
    if type(private_port) is not int or not 1 <= private_port <= 65535:
        raise ValueError("Native API requires a fixed private loopback port")

    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, *args):
            pass

        def respond(self, status, data=b"", content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def dispatch(self):
            try:
                target = surface.target(self.command, self.path)
                lengths = self.headers.get_all("Content-Length", [])
                if self.headers.get("Transfer-Encoding") or len(lengths) > 1:
                    raise ValueError("Ambiguous native request framing")
                length = int(lengths[0]) if lengths else 0
                if not 0 <= length <= MAX_BODY:
                    raise ValueError("Native API request exceeds its byte budget")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Native API request was interrupted")
                headers = {}
                for name in ("Content-Type", "Authorization"):
                    values = self.headers.get_all(name, [])
                    if len(values) > 1:
                        raise ValueError("Repeated native request header")
                    if values:
                        headers[name] = values[0]
                with gate.enter("public"):
                    connection = http.client.HTTPConnection("127.0.0.1", private_port, timeout=20)
                    try:
                        connection.request(self.command, target, body=body, headers=headers)
                        result = connection.getresponse()
                        data = result.read(MAX_BODY + 1)
                        if len(data) > MAX_BODY:
                            raise ValueError("Native API response exceeds its byte budget")
                        self.respond(
                            result.status,
                            data,
                            result.getheader("Content-Type", "application/json"),
                        )
                    finally:
                        connection.close()
            except PermissionError:
                self.respond(403)
            except (ValueError, UnicodeError):
                self.respond(400)
            except (OSError, http.client.HTTPException):
                self.respond(502)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = dispatch

    return ThreadingHTTPServer(address, Handler)
