"""The private control socket enforces scope and does not disclose native failures."""

import socket
import threading
import time

import pytest
from evalhive_mcpmark.world_control import decode, exchange, serve
from evalhive_mcpmark.world_rpc import WorldError

SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


def test_private_control_serves_operations_and_rejects_foreign_scope(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path, stop = root / "world.sock", threading.Event()
    thread = threading.Thread(
        target=serve,
        args=(lambda effect: lambda method, payload: {"native_result": "confirmed"}, path),
        kwargs={"stop": stop},
    )
    thread.start()
    try:
        for _ in range(200):
            if path.exists():
                break
            time.sleep(0.005)
        assert path.stat().st_mode & 0o777 == 0o600
        request = {
            "id": "prepare",
            "scope": SCOPE,
            "operation": "begin",
            "payload": {"method": "prepare", "params": {}},
        }
        assert exchange(request, path) == {"operation_id": "prepare"}
        poll = {
            **request,
            "id": "poll",
            "operation": "poll",
            "payload": {"operation_id": "prepare"},
        }
        for _ in range(200):
            result = exchange(poll, path)
            if result["state"] != "pending":
                break
            time.sleep(0.005)
        assert result["result"] == {"native_result": "confirmed"}
        with pytest.raises(WorldError, match="rejected"):
            exchange({**poll, "scope": {**SCOPE, "generation": 2}}, path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(b'{"id":"first","id":"second"}\n')
            assert b"NATIVE_CONTROL_REJECTED" in client.recv(4096)
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not path.exists()


def test_private_control_rejects_duplicate_fields_and_public_directory(tmp_path):
    with pytest.raises(WorldError, match="duplicate"):
        decode(b'{"scope":{"generation":1,"generation":2}}\n')
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(WorldError, match="private"):
        serve(lambda effect: None, public / "world.sock")


def test_private_control_waits_for_delayed_application_startup(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    path, stop = root / "world.sock", threading.Event()

    def delayed():
        time.sleep(0.1)
        serve(lambda effect: lambda method, payload: {"ready": True}, path, stop=stop)

    thread = threading.Thread(target=delayed)
    thread.start()
    try:
        result = exchange(
            {
                "id": "prepare",
                "scope": SCOPE,
                "operation": "begin",
                "payload": {"method": "prepare", "params": {}},
            },
            path,
            timeout=2,
        )
        assert result == {"operation_id": "prepare"}
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_private_control_startup_wait_has_one_bounded_deadline(tmp_path):
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="become ready"):
        exchange({}, tmp_path / "missing.sock", timeout=0.1)
    assert 0.09 <= time.monotonic() - started < 1
