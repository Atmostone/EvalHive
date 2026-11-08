"""Delegate native TGC/SGC mathematics while identifying incomplete scenarios."""

from __future__ import annotations

from collections import defaultdict

from .native import NativeConfigurationError


def aggregate(receipt, trackers):
    from appworld.evaluator import Metric, TestTracker

    membership = {
        case["native_ref"]["task_id"]: case["native_ref"]["scenario_id"]
        for case in receipt["cases"]
    }
    if not isinstance(trackers, dict) or not set(trackers) <= set(membership):
        raise NativeConfigurationError("Native aggregate contains an uninstalled task")
    expected, observed = defaultdict(set), defaultdict(set)
    for task, scenario in membership.items():
        expected[scenario].add(task)
        if task in trackers:
            observed[scenario].add(task)
    incomplete = sorted(
        scenario for scenario in observed if expected[scenario] != observed[scenario]
    )
    native = None
    if trackers:
        # The upstream include_details=False branch is incompatible at this revision.
        native = Metric.compute_metrics(
            {identity: TestTracker.from_dict(value) for identity, value in trackers.items()},
            include_details=True,
        )["aggregate"]
    return {
        "expected": len(membership),
        "observed": len(trackers),
        "native": {
            "aggregate": native,
            "incomplete_scenarios": incomplete,
            "missing_scenarios": sorted(set(expected) - set(observed)),
            "complete_dataset": len(trackers) == len(membership),
            "scenario_goal_completion_complete": bool(trackers)
            and not incomplete
            and len(trackers) == len(membership),
        },
    }
