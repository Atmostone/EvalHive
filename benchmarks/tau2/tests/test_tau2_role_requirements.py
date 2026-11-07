"""Native role requirements remain data for a benchmark-agnostic launcher."""

from types import SimpleNamespace

import pytest
from evalhive_tau2.dataset import descriptor


@pytest.mark.parametrize("judge", [False, True])
def test_case_declares_user_and_only_the_required_evaluator_model(judge):
    task = SimpleNamespace(
        id="fixture",
        evaluation_criteria=SimpleNamespace(reward_basis=["NL_ASSERTION"] if judge else []),
        model_dump=lambda **kwargs: {"id": "fixture", "judge": judge},
    )
    case = descriptor(task, "airline", "base", "fixture-set", "original", "sha256:" + "a" * 64)
    required = case["requirements"]["capabilities"]
    evaluation = case["evaluation_requirements"]["capabilities"]
    assert "model.user" in required and "model.user" not in evaluation
    assert ("model.judge" in required) is judge
    assert ("model.judge" in evaluation) is judge
