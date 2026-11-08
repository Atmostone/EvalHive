"""Private environment helpers require actual health and contained archive state."""

import io
import socket
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from evalhive_toolathlon.environment import (
    CheckpointLimit,
    extract_native,
    native_services,
    portal_service,
    portals,
    restore_services,
    restore_tree,
    snapshot_database,
    snapshot_tree,
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


def test_restore_removes_uncaptured_initial_files_instead_of_resurrecting_deleted_state(tmp_path):
    target = tmp_path / "world"
    (target / "old-directory").mkdir(parents=True)
    (target / "old-directory/removed.txt").write_text("not in the captured state")
    restore_tree(archive(tmp_path), target)
    assert sorted(path.name for path in target.iterdir()) == ["answer.txt"]
    assert (target / "answer.txt").read_bytes() == b"native-state"


def test_checkpoint_rejects_oversize_sparse_file_before_reading_or_creating_archive(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    with (source / "large").open("wb") as stream:
        stream.truncate(1024**3)
    target = tmp_path / "captured.tar"
    with pytest.raises(CheckpointLimit):
        snapshot_tree(source, target, 1024**2)
    assert not target.exists()


def test_checkpoint_bounds_tar_overhead_and_roundtrips_regular_state(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "answer.txt").write_text("answer")
    target = tmp_path / "captured.tar"
    with pytest.raises(CheckpointLimit):
        snapshot_tree(source, target, 1000)
    assert target.stat().st_size <= 1000
    target.unlink()
    written = snapshot_tree(source, target, 65536)
    assert written == target.stat().st_size <= 65536
    extract_native(target, tmp_path / "restored")
    assert (tmp_path / "restored/answer.txt").read_text() == "answer"


def test_checkpoint_rejects_links_before_reading_external_files(tmp_path):
    source = tmp_path / "workspace"
    source.mkdir()
    (source / "link").symlink_to(tmp_path / "private")
    target = tmp_path / "captured.tar"
    with pytest.raises(ValueError):
        snapshot_tree(source, target, 65536)
    assert not target.exists()


def test_oversize_database_dump_stops_producer_without_exceeding_capture_budget(
    tmp_path, monkeypatch
):
    producer = SimpleNamespace(
        stdout=io.BytesIO(b"native dump larger than the saved budget"),
        poll=lambda: None,
        kill=Mock(),
        wait=Mock(return_value=0),
    )
    monkeypatch.setattr(
        "evalhive_toolathlon.environment.subprocess.Popen", lambda *a, **kw: producer
    )
    target = tmp_path / "db.dump"
    with pytest.raises(CheckpointLimit):
        snapshot_database(target, 8)
    assert target.stat().st_size <= 8 and producer.stdout.closed
    producer.kill.assert_called_once()
    producer.wait.assert_called_once()


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
    assert portal_service([*argv[:4], "--bind", "127.0.0.1", *argv[4:]], "native-case") == expected
    assert portal_service([*argv[:4], "--bind", "0.0.0.0", *argv[4:]], "native-case") is None
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


@pytest.mark.parametrize(
    "script,expected",
    [
        ("raise SystemExit(1)", 1),
        ("raise SystemExit('checker startup failed')", 70),
        ("raise RuntimeError('checker infrastructure failure')", 70),
        ("import evalhive_nonexistent_fixture_dependency", 70),
    ],
)
def test_checker_crash_is_distinct_from_an_explicit_incorrect_verdict(tmp_path, script, expected):
    target = tmp_path / "checker.py"
    target.write_text(script)
    result = subprocess.run(
        [sys.executable, "-m", "evalhive_toolathlon.environment", "evaluate", str(target)],
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == expected


def test_checker_wrapper_preserves_native_argv_import_path_and_main_file(tmp_path):
    (tmp_path / "native_dependency.py").write_text("VALUE = 'native dependency'\n")
    target = tmp_path / "checker.py"
    target.write_text(
        "import sys;from pathlib import Path;from native_dependency import VALUE\n"
        "assert sys.argv == [str(Path(__file__)), '--launch_time', 'frozen time']\n"
        "assert __name__ == '__main__';print(VALUE)\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "evalhive_toolathlon.environment",
            "evaluate",
            str(target),
            "--launch_time",
            "frozen time",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and result.stdout == "native dependency\n"
