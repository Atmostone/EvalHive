"""Capacity eligibility cannot lower native requirements or grant a launch lease."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from app.runtime.admission import (
    GroupPolicy,
    HostCapacity,
    ResourceDemand,
    check_group,
    host_capacity,
)
from app.runtime.managed_spec import ManagedRuntimeError, PathMapping, Quota, RuntimePolicy


@pytest.fixture
def available():
    return {
        "demands": (
            ResourceDemand("task", Quota(2048, 1), 2048, 1, 4096),
            ResourceDemand("worker", Quota(512, 0.25), 512, 0.25, 512),
        ),
        "native_resources": {"memory_mb": 2048, "cpu_cores": 1, "disk_mb": 4096},
        "measured": HostCapacity(4096, 12000, 4, "2026-11-06T00:00:00Z"),
        "runtime_policy": RuntimePolicy(PathMapping("/host/data", "/data")),
        "policy": GroupPolicy(),
        "active_trials": 0,
    }


def test_group_budget_includes_worker_and_never_returns_a_launch_permission(available):
    result = check_group(**available)
    assert result["eligible"] and result["reasons"] == []
    assert result["group_resources"] == {"memory_mb": 2560, "cpu_cores": 1.25, "disk_mb": 4608}
    assert result["runtime_admission"] == "reservation_pending" and result["can_run"] is False


@pytest.mark.parametrize(
    "measurement,resource",
    [
        (HostCapacity(2800, 12000, 4, "now"), "memory_mb"),
        (HostCapacity(4096, 6000, 4, "now"), "disk_mb"),
        (HostCapacity(4096, 12000, 1, "now"), "cpu_cores"),
    ],
)
def test_whole_group_and_host_headroom_must_fit(available, measurement, resource):
    available["measured"] = measurement
    result = check_group(**available)
    assert not result["eligible"]
    assert any(
        reason["code"] == "insufficient_group_resources" and reason["resource"] == resource
        for reason in result["reasons"]
    )


@pytest.mark.parametrize("value", [None, True, float("nan"), -1])
def test_unknown_or_invalid_live_capacity_cannot_fall_back_to_policy_limits(available, value):
    available["measured"] = replace(available["measured"], memory_available_mb=value)
    result = check_group(**available)
    assert not result["eligible"]
    assert {"code": "capacity_unknown", "resource": "memory_mb"} in result["reasons"]


def test_serial_trial_limit_is_global_and_checked_before_preparation(available):
    available["active_trials"] = 1
    result = check_group(**available)
    assert not result["eligible"] and {"code": "host_busy"} in result["reasons"]


def test_provider_cannot_silently_lower_a_native_asset_requirement(available):
    original = available["demands"][0]
    lowered = replace(original, limits=Quota(1024, 0.5))
    available["demands"] = (lowered, available["demands"][1])
    result = check_group(**available)
    assert not result["eligible"]
    assert {"code": "lowered_native_requirements", "resource_id": "task"} in result["reasons"]
    assert lowered.limits.memory_mb == 1024 and original.minimum_memory_mb == 2048


def test_case_floor_cannot_be_lost_in_a_smaller_environment_plan(available):
    available["native_resources"] = {"memory_mb": 8192, "disk_mb": 20000}
    result = check_group(**available)
    assert not result["eligible"]
    assert {
        reason["resource"]
        for reason in result["reasons"]
        if reason["code"] == "underdeclared_group_requirements"
    } == {"memory_mb", "disk_mb"}
    assert result["group_resources"]["memory_mb"] == 2560


def test_per_container_runtime_maximum_is_enforced_before_any_effect(available):
    available["demands"] = (replace(available["demands"][0], limits=Quota(8192, 4)),)
    with pytest.raises(ManagedRuntimeError, match="exceed"):
        check_group(**available)


def test_aggregate_policy_limit_is_not_replaced_by_large_host_capacity(available):
    available["demands"] += (ResourceDemand("service", Quota(2048, 1), 2048, 1, 512),)
    available["measured"] = HostCapacity(64000, 64000, 32, "now")
    result = check_group(**available)
    assert not result["eligible"]
    assert {
        reason["resource"]
        for reason in result["reasons"]
        if reason["code"] == "insufficient_group_resources"
    } == {"memory_mb", "cpu_cores"}
    assert {reason["resource"]: reason["available"] for reason in result["reasons"]} == {
        "memory_mb": 4096,
        "cpu_cores": 2,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"active_trials": True},
        {"active_trials": -1},
        {"policy": GroupPolicy(max_concurrency=2)},
        {"policy": GroupPolicy(memory_reserve_mb=-1)},
        {"native_resources": {"cpu_cores": float("nan")}},
        {"native_resources": {"memory_mb": True}},
        {"native_resources": {"unknown": 1}},
    ],
)
def test_invalid_policy_and_native_requirements_fail_closed(available, changes):
    with pytest.raises(ManagedRuntimeError):
        check_group(**{**available, **changes})


def test_duplicate_plan_resources_are_not_counted_twice(available):
    available["demands"] += (available["demands"][0],)
    with pytest.raises(ManagedRuntimeError, match="distinct"):
        check_group(**available)


def test_host_measurement_uses_available_memory_and_reports_missing_storage(monkeypatch):
    runtime = MagicMock()
    runtime.client.info.return_value = {"NCPU": 4}
    monkeypatch.setattr(
        Path, "read_text", lambda _: "MemTotal: 10000000 kB\nMemAvailable: 3145728 kB\n"
    )
    monkeypatch.setattr(
        "app.runtime.admission.shutil.disk_usage", lambda _: (_ for _ in ()).throw(OSError())
    )
    snapshot = host_capacity(runtime, Path("/unavailable-storage"))
    assert snapshot.memory_available_mb == 3072 and snapshot.cpu_total_cores == 4
    assert snapshot.disk_free_mb is None
    runtime.check_daemon.assert_called_once()
