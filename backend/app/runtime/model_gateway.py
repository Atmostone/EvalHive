"""Scoped model HTTP transport for tasks without network egress.

The server binds only an owned Unix socket. Callers resolve model connections and
provide a current-owner guard; benchmark code supplies neither an upstream URL
nor an owner credential. Native clients keep their HTTP/SSE request semantics.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, StreamingResponse


@dataclass(frozen=True)
class ModelRoute:
    model: str
    endpoint: str = field(repr=False)
    api_key: str = field(repr=False)
    token: str = field(repr=False)

    def __post_init__(self):
        if any(
            not isinstance(value, str)
            for value in (self.model, self.endpoint, self.api_key, self.token)
        ):
            raise ValueError("Model gateway requires a resolved private model connection")
        address = urlsplit(self.endpoint)
        if (
            not self.model
            or not self.token
            or not self.api_key
            or address.scheme not in {"http", "https"}
            or not address.netloc
            or address.username
            or address.password
            or address.query
            or address.fragment
            or any(character in self.api_key + self.token for character in "\r\n")
        ):
            raise ValueError("Model gateway requires a resolved private model connection")


class GatewayRejected(Exception):
    """A fixed public error code, never a provider exception string."""

    def __init__(self, code, status=403):
        self.code, self.status = code, status
        super().__init__(code)


class ModelGateway:
    """ASGI application with one explicit upstream route per authorized role."""

    def __init__(
        self,
        routes,
        guard,
        *,
        client=None,
        max_request_bytes=2 * 1024**2,
        max_response_bytes=64 * 1024**2,
    ):
        if (
            not routes
            or not callable(guard)
            or any(
                not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}", role)
                or not isinstance(route, ModelRoute)
                for role, route in routes.items()
            )
        ):
            raise ValueError("Model gateway requires scoped role routes and an ownership guard")
        if any(
            type(value) is not int or not 1 <= value <= 2**30
            for value in (max_request_bytes, max_response_bytes)
        ):
            raise ValueError("Model gateway byte limits must be bounded positive integers")
        self.routes, self.guard = dict(routes), guard
        self.max_request_bytes, self.max_response_bytes = max_request_bytes, max_response_bytes
        self.client = client or httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self.owns_client, self.closing, self.active = client is None, False, set()

    async def _budget(self, role):
        if self.closing:
            raise GatewayRejected("MODEL_GATEWAY_CLOSED")
        remaining = await self.guard(role)
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
            or not 0 < remaining <= 7 * 86400
        ):
            raise GatewayRejected("MODEL_GATEWAY_EXPIRED")
        return remaining

    async def _receive_owned(self, awaitable, role):
        """Recheck ownership even when the provider stops producing bytes."""
        pending = asyncio.ensure_future(awaitable)
        try:
            while not pending.done():
                completed, _ = await asyncio.wait({pending}, timeout=0.5)
                if not completed:
                    await self._budget(role)
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @staticmethod
    async def _error(scope, receive, send, error):
        await JSONResponse(
            {"error": {"code": error.code, "message": "Model gateway request was rejected"}},
            status_code=error.status,
        )(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        response_started = False

        async def tracked_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        task = asyncio.current_task()
        self.active.add(task)
        try:
            await self._request(scope, receive, tracked_send)
        except GatewayRejected as error:
            if not response_started:
                await self._error(scope, receive, send, error)
        except (httpx.HTTPError, TimeoutError):
            if not response_started:
                await self._error(
                    scope, receive, send, GatewayRejected("MODEL_GATEWAY_UPSTREAM_UNAVAILABLE", 502)
                )
        except ClientDisconnect:
            pass
        finally:
            self.active.discard(task)

    async def _request(self, scope, receive, send):
        request = Request(scope, receive)
        match = re.fullmatch(
            r"/([A-Za-z][A-Za-z0-9_.:-]{0,63})/v1/(chat/completions|responses)", scope["path"]
        )
        if request.method != "POST" or not match or scope.get("query_string"):
            raise GatewayRejected("MODEL_GATEWAY_ROUTE_UNAVAILABLE", 404)
        role, operation = match.groups()
        route = self.routes.get(role)
        authorization = request.headers.get("authorization", "")
        if route is None or not hmac.compare_digest(
            authorization.encode(), ("Bearer " + route.token).encode()
        ):
            raise GatewayRejected("MODEL_GATEWAY_UNAUTHORIZED", 401)
        budget = await self._budget(role)
        async with asyncio.timeout(budget):
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > self.max_request_bytes:
                    raise GatewayRejected("MODEL_GATEWAY_REQUEST_TOO_LARGE", 413)
                data.extend(chunk)
            try:
                body = json.loads(data)
            except (ValueError, UnicodeError):
                raise GatewayRejected("MODEL_GATEWAY_INVALID_REQUEST", 400) from None
            if not isinstance(body, dict) or body.get("model") != route.model:
                raise GatewayRejected("MODEL_GATEWAY_MODEL_UNAVAILABLE", 400)
            await self._budget(role)  # Uploading a body cannot retain expired ownership.
            excluded = {
                "host",
                "authorization",
                "proxy-authorization",
                "proxy-connection",
                "connection",
                "content-length",
                "transfer-encoding",
                "upgrade",
                "cookie",
                "x-api-key",
                "api-key",
            }
            headers = {
                name: value
                for name, value in request.headers.items()
                if name.lower() not in excluded and not name.lower().startswith("x-forwarded-")
            }
            headers.update(
                Authorization="Bearer " + route.api_key, **{"Content-Type": "application/json"}
            )
            outbound = self.client.build_request(
                "POST",
                route.endpoint.rstrip("/") + "/" + operation,
                content=bytes(data),
                headers=headers,
            )
            upstream = await self._receive_owned(
                self.client.send(outbound, stream=True, follow_redirects=False), role
            )
            try:
                if not 200 <= upstream.status_code < 300:
                    # Provider diagnostics may echo the owner key. Preserve the
                    # status/retry semantics without forwarding private details.
                    headers = {}
                    retry = upstream.headers.get("retry-after", "")
                    if re.fullmatch(r"[0-9]{1,6}", retry):
                        headers["Retry-After"] = retry
                    await JSONResponse(
                        {
                            "error": {
                                "code": "MODEL_GATEWAY_PROVIDER_ERROR",
                                "message": "Model provider request failed",
                            }
                        },
                        status_code=upstream.status_code if upstream.status_code >= 400 else 502,
                        headers=headers,
                    )(scope, receive, send)
                    return

                async def chunks():
                    count, checked = 0, 0.0
                    iterator = upstream.aiter_bytes()
                    while True:
                        try:
                            chunk = await self._receive_owned(anext(iterator), role)
                        except StopAsyncIteration:
                            break
                        count += len(chunk)
                        if count > self.max_response_bytes:
                            raise GatewayRejected("MODEL_GATEWAY_RESPONSE_TOO_LARGE", 502)
                        now = asyncio.get_running_loop().time()
                        if now - checked >= 0.5:
                            await self._budget(role)
                            checked = now
                        yield chunk

                headers = {
                    "Content-Type": upstream.headers.get("content-type", "application/json"),
                    "Cache-Control": "no-store",
                }
                await StreamingResponse(
                    chunks(), status_code=upstream.status_code, headers=headers
                )(scope, receive, send)
            finally:
                await upstream.aclose()

    async def close(self):
        self.closing = True
        pending = self.active - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=3)
        if self.owns_client:
            await self.client.aclose()
