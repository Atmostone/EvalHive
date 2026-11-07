"""The model channel uses an owned filesystem socket without process-wide changes."""

import logging
import signal
import socket
import stat
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from app.runtime.managed_spec import ManagedRuntimeError, OwnerScope, PathMapping, RuntimePolicy
from app.runtime.model_gateway import ModelGateway
from app.runtime.model_socket import ModelSocket

from tests.runtime.test_model_gateway import ROUTE, TOKEN


@pytest.mark.asyncio
async def test_unix_listener_supports_long_owned_roots_and_preserves_api_signals_and_logs(tmp_path):
    root = tmp_path / ("long-owned-data-" * 8)
    root.mkdir()
    policy = RuntimePolicy(PathMapping("/host/data", str(root)))
    scope = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
    logger = logging.getLogger("uvicorn.access")
    logging_state = logger.level, list(logger.handlers), logger.propagate
    original_signal = signal.getsignal(signal.SIGTERM)
    observed = []

    def provider(request):
        observed.append(request)
        return httpx.Response(200, json={"output_text": "Native response"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        gateway = ModelGateway({"agent": ROUTE}, AsyncMock(return_value=60), client=upstream)
        listener = ModelSocket(gateway, scope, policy)
        await listener.start()
        try:
            assert listener.listener.family == socket.AF_UNIX
            assert len(str(listener.socket_path).encode()) > 108
            assert stat.S_IMODE(listener.directory.stat().st_mode) == 0o711
            assert stat.S_IMODE(listener.socket_path.stat().st_mode) == 0o666
            address = f"/proc/self/fd/{listener.directory_fd}/model.sock"
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=address), base_url="http://owned-socket"
            ) as client:
                response = await client.post(
                    "/agent/v1/responses",
                    json={"model": ROUTE.model},
                    headers={"authorization": "Bearer " + TOKEN},
                )
            assert response.status_code == 200 and len(observed) == 1
            assert listener.mount.read_only and listener.mount.target == "/evalhive-control"
            assert scope.session_id in listener.mount.source
            assert signal.getsignal(signal.SIGTERM) == original_signal
            assert (logger.level, list(logger.handlers), logger.propagate) == logging_state
        finally:
            await listener.close()
        assert not listener.socket_path.exists() and not listener.directory.exists()
        assert listener.directory_fd is None
        await listener.close()
        with pytest.raises(ManagedRuntimeError, match="reuse"):
            await listener.start()


@pytest.mark.asyncio
async def test_listener_release_preserves_a_replaced_path_and_closes_its_descriptor(tmp_path):
    policy = RuntimePolicy(PathMapping("/host/data", str(tmp_path)))
    scope = OwnerScope(*(str(uuid.uuid4()) for _ in range(3)), 1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as upstream:
        listener = ModelSocket(
            ModelGateway({"agent": ROUTE}, AsyncMock(return_value=60), client=upstream),
            scope,
            policy,
        )
        await listener.start()
        listener.socket_path.unlink()
        listener.socket_path.write_text("Another owner replaced this path")
        with pytest.raises(ManagedRuntimeError, match="identity changed"):
            await listener.close()
        assert listener.directory_fd is None
        assert listener.socket_path.read_text() == "Another owner replaced this path"
