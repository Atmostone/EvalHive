"""Official scenario aggregation cannot treat a partial scenario as complete."""

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from evalhive_appworld.aggregate import aggregate
from evalhive_appworld.native import NativeConfigurationError, canonical, load
from evalhive_appworld.worker import AppWorldConnector
from evalhive_benchmark import ProtocolError, validate

pytestmark = pytest.mark.skipif(
    os.getenv("EVALHIVE_NATIVE_APPWORLD_CATALOG") != "1",
    reason="Requires the verified official AppWorld package and data installation",
)


def receipt():
    return {
        "cases": [
            {"native_ref": {"task_id": task, "scenario_id": task.split("_")[0]}}
            for task in ("abcdef0_1", "abcdef0_2", "1234567_1")
        ]
    }


def tracker(success):
    return {
        "success": success,
        "difficulty": 1,
        "num_tests": 1,
        "passes": [{"requirement": "Native requirement", "label": "no_op_fail"}] if success else [],
        "failures": []
        if success
        else [
            {"requirement": "Native requirement", "label": "no_op_fail", "trace": "Fixture failure"}
        ],
    }


def test_official_scenario_metric_differs_from_average_task_success():
    load(os.environ["APPWORLD_ROOT"])
    result = aggregate(
        receipt(),
        {"abcdef0_1": tracker(True), "abcdef0_2": tracker(False), "1234567_1": tracker(True)},
    )
    assert result["native"]["aggregate"] == {
        "task_goal_completion": 66.7,
        "scenario_goal_completion": 50.0,
    }
    assert (
        result["native"]["complete_dataset"]
        and result["native"]["scenario_goal_completion_complete"]
    )


def test_partial_scenario_and_missing_scenarios_are_explicit():
    load(os.environ["APPWORLD_ROOT"])
    result = aggregate(receipt(), {"abcdef0_1": tracker(True)})
    assert result["observed"] == 1 and result["expected"] == 3
    assert result["native"]["incomplete_scenarios"] == ["abcdef0"]
    assert result["native"]["missing_scenarios"] == ["1234567"]
    assert not result["native"]["scenario_goal_completion_complete"]
    assert aggregate(receipt(), {})["native"]["aggregate"] is None
    with pytest.raises(NativeConfigurationError, match="uninstalled"):
        aggregate(receipt(), {"aaaaaaa_1": tracker(True)})


def test_worker_uses_authenticated_trackers_and_hides_incomplete_scenario_metric():
    catalog = receipt()
    dataset = SimpleNamespace(
        root=Path(os.environ["APPWORLD_ROOT"]),
        receipt=catalog,
        cases={case["native_ref"]["task_id"]: case for case in catalog["cases"]},
    )
    worker = AppWorldConnector(dataset=dataset)
    value = tracker(True)
    result = {
        "status": "completed",
        "success": True,
        "metrics": [],
        "artifacts": [],
        "completeness": {},
        "native": {
            "task_id": "abcdef0_1",
            "scenario_id": "abcdef0",
            "test_tracker": value,
            "tracker_sha256": hashlib.sha256(canonical(value)).hexdigest(),
        },
    }
    output = worker.aggregate({"results": [result], "expected": 1})
    validate("aggregate_result", output)
    assert output["expected"] == 3 and output["observed"] == 1
    assert (
        next(
            metric for metric in output["metrics"] if metric["name"] == "scenario_goal_completion"
        )["value"]
        is None
    )
    assert output["native"]["incomplete_scenarios"] == ["abcdef0"]
    result["native"]["test_tracker"]["difficulty"] = 2
    with pytest.raises(ProtocolError, match="trackers"):
        worker.aggregate({"results": [result], "expected": 1})
