"""Finalization seals native work, is repeatable, and remains available at expiry."""

import time

import pytest
from evalhive_mcpmark.finalizer import finalize
from evalhive_mcpmark.world_rpc import World, WorldError

SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


def test_finalizer_closes_native_state_once_and_rejects_new_work_after_expiry():
    operations = []

    def factory(effect):
        def service(method, payload):
            operations.append(method)
            return {"cleaned": True}

        return service

    world = World(factory)
    try:
        world.deadline = time.monotonic() - 1
        assert (
            finalize(SCOPE, control=world.request)
            == finalize(SCOPE, control=world.request)
            == {"finalized": True}
        )
        assert operations == ["cleanup"]
        world.deadline = time.monotonic() + 10
        with pytest.raises(WorldError, match="sealed"):
            world.request(
                {
                    "id": "new.prepare",
                    "scope": SCOPE,
                    "operation": "begin",
                    "payload": {"method": "prepare", "params": {}},
                }
            )
    finally:
        assert world.close()


def test_native_cleanup_failure_cannot_be_reported_as_finalized():
    def factory(effect):
        def service(method, payload):
            raise RuntimeError("fixture failure")

        return service

    world = World(factory)
    try:
        with pytest.raises(ValueError, match="not confirmed"):
            finalize(SCOPE, control=world.request)
    finally:
        assert world.close()
