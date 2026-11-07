"""Official scenario aggregation cannot treat a partial scenario as complete."""

import os

import pytest
from evalhive_appworld.aggregate import aggregate
from evalhive_appworld.native import NativeConfigurationError, load

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
        else [{"requirement": "Native requirement", "label": "no_op_fail", "trace": "Fixture failure"}],
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
