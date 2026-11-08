"""Native simulated clocks cannot disable the SDK service deadlines."""

import socketserver
import threading
import time

import pytest
from evalhive_benchmark.world_control import exchange, serve
from evalhive_benchmark.world_rpc import World, WorldError, monotonic


def test_frozen_python_clock_does_not_extend_native_service_deadline(monkeypatch):
    monkeypatch.setattr(time, "monotonic", lambda: 0)
    start = monotonic()
    world = World(lambda effect: lambda method, payload: {}, deadline_seconds=0.03)
    try:
        time.sleep(0.05)
        assert monotonic() > start + 0.03
        with pytest.raises(WorldError, match="stopping"):
            world.request(
                {
                    "id": "prepare",
                    "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
                    "operation": "begin",
                    "payload": {"method": "prepare", "params": {}},
                }
            )
    finally:
        assert world.close()


def test_private_control_survives_native_clock_restoration(tmp_path, monkeypatch):
    calls, errors = [], []

    def changed_clock():
        calls.append(True)
        return 1700000000 if len(calls) == 1 else time.monotonic()

    monkeypatch.setattr(socketserver, "time", changed_clock)
    tmp_path.chmod(0o700)
    stop = threading.Event()

    def run():
        try:
            serve(
                lambda effect: lambda method, payload: {"ready": True},
                tmp_path / "world.sock",
                stop=stop,
            )
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        thread.join(timeout=0.3)
        assert thread.is_alive() and not errors
        result = exchange(
            {
                "id": "prepare",
                "scope": {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
                "operation": "begin",
                "payload": {"method": "prepare", "params": {}},
            },
            tmp_path / "world.sock",
        )
        assert result == {"operation_id": "prepare"}
        assert not calls
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not thread.is_alive() and not errors
