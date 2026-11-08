"""Task-facing REST cannot expose native controls or race persistence."""

import asyncio
import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from evalhive_appworld.api_gate import Gate, Surface, proxy


@pytest.fixture
def surface():
    return Surface(
        {"spotify": {"show_song": {"path": "/spotify/songs/{song_id}", "method": "GET"}}}
    )


@pytest.mark.parametrize(
    "path",
    [
        "/dbs",
        "/seed",
        "/admin/dbs",
        "/api_docs/show_api_doc",
        "/docs",
        "/openapi.json",
        "/spotify/../dbs",
        "/spotify/%2e%2e/dbs",
        "/spotify/songs/a%2fb",
        "http://127.0.0.1/dbs",
        "/spotify//songs/1",
        "/spotify/songs/%00",
    ],
)
def test_task_surface_rejects_controls_and_path_confusion(surface, path):
    with pytest.raises(PermissionError):
        surface.target("GET", path)


def test_task_surface_preserves_native_routes_methods_and_query(surface):
    assert surface.target("GET", "/spotify/songs/42?mode=detail") == "/spotify/songs/42?mode=detail"
    with pytest.raises(PermissionError):
        surface.target("DELETE", "/spotify/songs/42")
    with pytest.raises(ValueError):
        Surface({"admin": {"control": {"method": "GET", "path": "/admin/dbs"}}})


def test_quiescence_tracks_native_work_after_the_proxy_request_has_ended():
    gate = Gate()
    gate.open()
    pending, finish = threading.Event(), threading.Event()

    def native_work():
        with gate.enter("native"):
            pending.set()
            finish.wait(3)

    worker = threading.Thread(target=native_work)
    worker.start()
    try:
        assert pending.wait(1)
        with pytest.raises(TimeoutError):
            gate.seal(timeout=0.05)
        with pytest.raises(PermissionError):
            with gate.enter("public"):
                pass
    finally:
        finish.set()
        worker.join(timeout=2)
    gate.seal(timeout=1)


def test_native_asgi_accounting_closes_on_failure():
    gate = Gate()

    async def app(scope, receive, send):
        assert gate.native == 1
        raise ValueError("fixture backend failure")

    with pytest.raises(ValueError):
        asyncio.run(gate.wrap_native(app)({"type": "http"}, None, None))
    assert gate.native == 0


def test_quiescence_deadline_does_not_use_the_frozen_python_clock(monkeypatch):
    gate = Gate()
    monkeypatch.setattr(time, "monotonic", lambda: 0)
    with gate.enter("native"), pytest.raises(TimeoutError):
        gate.seal(timeout=0.02)


def test_real_http_proxy_keeps_the_private_backend_unreachable(surface):
    observed = []

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            observed.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"song_id":42}')

    backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    gate = Gate()
    gate.open()
    public = proxy(surface, gate, backend.server_port, address=("127.0.0.1", 0))
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True) for server in (backend, public)
    ]
    for thread in threads:
        thread.start()
    try:
        for path, status in [("/dbs", 403), ("/spotify/songs/42", 200), ("/openapi.json", 403)]:
            connection = http.client.HTTPConnection("127.0.0.1", public.server_port, timeout=2)
            connection.request("GET", path)
            result = connection.getresponse()
            assert result.status == status
            result.read()
            connection.close()
        gate.seal(timeout=1)
        connection = http.client.HTTPConnection("127.0.0.1", public.server_port, timeout=2)
        connection.request("GET", "/spotify/songs/42")
        assert connection.getresponse().status == 403
        connection.close()
        assert observed == ["/spotify/songs/42"]
    finally:
        for server, thread in zip((backend, public), threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
