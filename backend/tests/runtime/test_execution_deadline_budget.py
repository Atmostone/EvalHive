"""Parent deadline inheritance is explicit and cannot extend actor grants."""

import pytest
from app.benchmarks.resource_authority import execution_timeout
from evalhive_benchmark import ProtocolError


def test_inherited_command_maximum_is_bounded_by_parent_remaining_time():
    assert execution_timeout({"timeout_seconds": 120, "deadline_policy": "inherit"}, 300, 45) == 45
    assert execution_timeout({"timeout_seconds": 20, "deadline_policy": "inherit"}, 300, 45) == 20
    assert execution_timeout({}, 300, 45) == 45
    assert execution_timeout({"timeout_seconds": 45}, 300, 45) == 45


@pytest.mark.parametrize(
    "payload,maximum,remaining",
    [
        ({"timeout_seconds": 120}, 300, 45),
        ({"timeout_seconds": 120, "deadline_policy": "strict"}, 300, 45),
        ({"timeout_seconds": 120, "deadline_policy": "inherit"}, 60, 45),
        ({"timeout_seconds": True, "deadline_policy": "inherit"}, 300, 45),
        ({"timeout_seconds": 0, "deadline_policy": "inherit"}, 300, 45),
        ({"timeout_seconds": float("nan"), "deadline_policy": "inherit"}, 300, 45),
    ],
)
def test_inheritance_cannot_bypass_saved_policy_or_silently_change_strict_requests(
    payload, maximum, remaining
):
    with pytest.raises(ProtocolError) as caught:
        execution_timeout(payload, maximum, remaining)
    assert caught.value.code == "LIMIT_EXCEEDED"


@pytest.mark.parametrize("policy", ["unknown", [], False])
def test_unsupported_deadline_policy_is_rejected(policy):
    with pytest.raises(ProtocolError) as caught:
        execution_timeout({"deadline_policy": policy}, 300, 45)
    assert caught.value.code == "INVALID_RESOURCE_REQUEST"


def test_parent_expiration_is_an_execution_deadline_not_a_configuration_change():
    with pytest.raises(ProtocolError) as caught:
        execution_timeout({"timeout_seconds": 120, "deadline_policy": "inherit"}, 300, 0)
    assert caught.value.code == "TRIAL_DEADLINE" and caught.value.kind == "execution"
