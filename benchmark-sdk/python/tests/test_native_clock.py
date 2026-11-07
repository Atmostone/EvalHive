"""Native simulated clocks cannot disable the SDK service deadlines."""

import time

import pytest
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
