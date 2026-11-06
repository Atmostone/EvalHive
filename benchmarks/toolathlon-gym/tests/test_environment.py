"""Private environment helpers require actual health and contained archive state."""

import io
import socket
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from evalhive_toolathlon.environment import (
    extract_native,
    native_services,
    portal_service,
    portals,
    restore_services,
)


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


def test_checkpoint_accepts_only_restartable_native_read_only_portals(tmp_path, monkeypatch):
    task = tmp_path / "task"
    (task / "tmp/pages").mkdir(parents=True)
    monkeypatch.setattr("evalhive_toolathlon.environment.task_root", lambda identity: task)
    argv = ["python3", "-m", "http.server", "30411", "--directory", str(task / "tmp/pages")]
    expected = {"kind": "python_http", "port": 30411, "root": "task", "path": "tmp/pages"}
    assert portal_service(argv, "native-case") == expected
    assert portal_service(["python3", "/arbitrary-server.py"], "native-case") is None
    assert portal_service([*argv[:-1], "/private"], "native-case") is None
    proc = tmp_path / "proc"
    for identity, command in (
        ("900001", argv),
        ("900002", ["/evalhive-runtime/supervisor", "--socket", "owned"]),
    ):
        directory = proc / identity
        directory.mkdir(parents=True)
        (directory / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
    assert native_services("native-case", proc=proc) == [expected]
    (proc / "900002/cmdline").write_bytes(b"python3\0unknown-mutator.py\0")
    assert native_services("native-case", proc=proc) is None


@pytest.mark.parametrize("path", ["/private", "../other", "tmp/../../other", "tmp//pages"])
def test_restored_portals_cannot_escape_captured_native_state(tmp_path, monkeypatch, path):
    monkeypatch.setattr(
        "evalhive_toolathlon.environment.task_root", lambda identity: Path(tmp_path)
    )
    with pytest.raises(ValueError):
        restore_services(
            "native-case", [{"kind": "python_http", "port": 30411, "root": "task", "path": path}]
        )


def test_native_portal_restart_serves_restored_files_without_preprocessing(tmp_path, monkeypatch):
    task = tmp_path / "task"
    (task / "pages").mkdir(parents=True)
    (task / "pages/data.json").write_text('{"source":"restored checkpoint"}')
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr("evalhive_toolathlon.environment.task_root", lambda identity: task)
    children, popen = [], subprocess.Popen

    def start(*args, **options):
        process = popen(*args, **options)
        children.append(process)
        return process

    monkeypatch.setattr("evalhive_toolathlon.environment.subprocess.Popen", start)
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    url = f"http://127.0.0.1:{port}/data.json"
    try:
        restore_services(
            "native-case",
            [{"kind": "python_http", "port": port, "root": "task", "path": "pages"}],
            reports=reports,
        )
        deadline = time.monotonic() + 5
        while not portals([url])["ready"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert portals([url])["ready"]
    finally:
        for process in children:
            process.terminate()
            process.wait(timeout=3)
