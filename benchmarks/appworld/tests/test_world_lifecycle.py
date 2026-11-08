"""Native evaluation and cancellation cleanup require confirmed durable state."""

from types import SimpleNamespace

import pytest
from evalhive_appworld.world_service import NativeService
from evalhive_benchmark.world_rpc import WorldError


def service(tmp_path, *, save_error=False):
    calls = []
    value = NativeService(None, records=tmp_path / "records")

    def save():
        calls.append("save")
        if save_error:
            raise OSError("fixture persistence failure")

    def evaluate(**kwargs):
        calls.append("evaluate")
        assert calls[:2] == ["seal", "save"]
        return SimpleNamespace(success=False, to_dict=lambda: {"success": False})

    value.world = SimpleNamespace(
        save=save,
        close=lambda: calls.append("world.close"),
        evaluate=evaluate,
        task_id="abcdef0_1",
        task=SimpleNamespace(generator_id="abcdef0"),
    )
    value.api = SimpleNamespace(
        control=lambda operation: calls.append(operation), close=lambda: calls.append("api.close")
    )
    value.ready = True
    return value, calls


def test_capture_persists_before_evaluation_and_cleanup_does_not_save_twice(tmp_path):
    world, calls = service(tmp_path)
    with pytest.raises(WorldError, match="persisted"):
        world("verify", {})
    assert world("capture", {"agent_success": False}) == {"agent_success": False, "saved": True}
    assert world("verify", {})["success"] is False
    assert world("verify", {})["native_verdict_complete"]
    assert world("cleanup", {}) == world("cleanup", {}) == {"cleaned": True}
    assert calls == ["seal", "save", "evaluate", "world.close", "api.close"]


def test_cancel_before_capture_still_saves_the_remote_effects(tmp_path):
    world, calls = service(tmp_path)
    assert world("cleanup", {}) == {"cleaned": True}
    assert calls == ["seal", "save", "world.close", "api.close"]


def test_failed_save_quiesces_api_without_acknowledging_cleanup_or_evaluation(tmp_path):
    world, calls = service(tmp_path, save_error=True)
    with pytest.raises(OSError):
        world("cleanup", {})
    assert calls == ["seal", "save", "api.close"]
    assert not world.saved and not world.closed
    with pytest.raises(WorldError, match="persisted"):
        world("verify", {})


def test_conflicting_capture_receipt_is_rejected(tmp_path):
    world, calls = service(tmp_path)
    world("capture", {"agent_success": True})
    with pytest.raises(WorldError, match="conflicts"):
        world("capture", {"agent_success": False})
    assert calls == ["seal", "save"]
