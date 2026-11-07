"""Native thread affinity, effect receipts and cancellation are explicit."""

import threading
import time
from copy import deepcopy

import pytest
from evalhive_mcpmark.world_rpc import World, WorldError

SCOPE = {"workspace_id": "workspace", "trial_id": "trial", "generation": 1}


def request(identity, operation, payload):
    return {"id": identity, "scope": SCOPE, "operation": operation, "payload": payload}


def poll(world, operation, sequence=0):
    for index in range(sequence, sequence + 200):
        result = world.request(request("poll." + str(index), "poll", {"operation_id": operation}))
        if result["state"] != "pending" or result["effects"]:
            return result, index + 1
        time.sleep(0.005)
    raise AssertionError("Native fixture operation did not settle")


def test_native_effects_resume_once_and_browser_objects_keep_their_thread():
    threads = []

    def factory(effect):
        threads.append(threading.get_ident())

        def service(method, payload):
            threads.append(threading.get_ident())
            if method == "prepare":
                return effect("allocate", {"resource_id": "website"})
            return {"cleaned": True}

        return service

    world = World(factory)
    try:
        start = request("prepare", "begin", {"method": "prepare", "params": {}})
        assert world.request(start) == world.request(deepcopy(start)) == {"operation_id": "prepare"}
        pending, sequence = poll(world, "prepare")
        (effect,) = pending["effects"]
        assert effect["operation"] == "allocate"
        response = request(
            "resume", "resume", {"effect_id": effect["id"], "result": {"handle": "owned-website"}}
        )
        assert world.request(response) == world.request(deepcopy(response)) == {"accepted": True}
        result, sequence = poll(world, "prepare", sequence)
        assert result["result"] == {"handle": "owned-website"}
        world.request(request("cleanup", "begin", {"method": "cleanup", "params": {}}))
        cleaned, _ = poll(world, "cleanup", sequence)
        assert cleaned["result"]["cleaned"] and len(set(threads)) == 1
    finally:
        assert world.close()


def test_world_rejects_foreign_scope_and_cancel_wakes_a_pending_broker_effect():
    def factory(effect):
        return lambda method, payload: effect("allocate", {"resource_id": "website"})

    world = World(factory)
    world.request(request("prepare", "begin", {"method": "prepare", "params": {}}))
    poll(world, "prepare")
    foreign = request("foreign", "poll", {"operation_id": "prepare"})
    foreign["scope"] = {**SCOPE, "workspace_id": "other"}
    with pytest.raises(WorldError, match="scope"):
        world.request(foreign)
    assert world.close() and not world.thread.is_alive()
