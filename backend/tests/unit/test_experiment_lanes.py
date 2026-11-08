"""Retired service-specific controls cannot bypass the SDK launch boundary."""

import pytest
from app.quality.experiments import requires_benchmark_sdk


@pytest.mark.parametrize("dataset,cases,lanes", [
    ({"source": "benchmark_suite", "suite": "historical"}, [], None),
    ({"source": "upload"}, [{"external_eval": {"eval_command": "native"}}], None),
    ({"source": "upload"}, [{"environment": {"required_services": ["database"]}}], None),
    ({"source": "tasks"}, [], 0),
    ({"source": "tasks"}, [], 1),
])
def test_legacy_execution_inputs_require_sdk(dataset, cases, lanes):
    assert requires_benchmark_sdk(dataset, cases, lanes)


def test_ordinary_inputs_remain_supported():
    assert not requires_benchmark_sdk({"source": "upload"}, [{"title": "ordinary"}], None)
