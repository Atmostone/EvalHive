"""Private environment helpers require actual health and contained archive state."""

import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from evalhive_toolathlon.environment import extract_native, portals


@pytest.fixture
def endpoint():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response({"/ready": 200, "/missing": 404, "/redirect": 302}[self.path])
            if self.path == "/redirect":
                self.send_header("Location", "https://example.invalid/outside")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_http_health_uses_actual_path_status_and_does_not_follow_redirects(endpoint):
    assert portals([endpoint + "/ready"])["ready"] is True
    assert portals([endpoint + "/missing"])["ready"] is False
    assert portals([endpoint + "/redirect"])["checks"][0]["status"] == 302
    assert portals([endpoint + "/redirect"])["ready"] is False
    with pytest.raises(ValueError):
        portals(["https://example.invalid/outside"])


def archive(tmp_path, name="answer.txt", *, kind=tarfile.REGTYPE):
    target = tmp_path / "state.tar"
    member = tarfile.TarInfo(name)
    member.type, member.linkname = kind, "../outside"
    data = b"native-state"
    member.size = len(data) if kind == tarfile.REGTYPE else 0
    with tarfile.open(target, "w") as result:
        result.addfile(member, io.BytesIO(data) if member.size else None)
    return target


def test_native_state_extracts_only_into_the_selected_owned_root(tmp_path):
    target = tmp_path / "world"
    extract_native(archive(tmp_path), target)
    assert (target / "answer.txt").read_bytes() == b"native-state"


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hard-link", tarfile.LNKTYPE),
    ],
)
def test_escaping_or_link_state_is_rejected_before_restoration(tmp_path, name, kind):
    with pytest.raises(ValueError):
        extract_native(archive(tmp_path, name, kind=kind), tmp_path / "world")
    assert not (tmp_path / "outside").exists()
