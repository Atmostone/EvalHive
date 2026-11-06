"""Usage is per request, role and model; unavailable measurements stay unknown."""

import pytest
from app.benchmarks.usage import TOKEN_FIELDS, UsageLedger


def observation(**changes):
    return {
        "usage_id": "request-1",
        "role": "user",
        "model": "fixture",
        "purpose": "simulation",
        "mode": "cumulative",
        **{field: 1 for field in TOKEN_FIELDS},
        "cost": {"currency": "USD", "amount": "0.00000001", "source": "provider"},
        **changes,
    }


def group(ledger):
    return ledger.summary()["groups"][0]


def test_cumulative_snapshots_are_not_charged_twice():
    ledger = UsageLedger()
    for sequence in range(1, 4):
        ledger.add("worker", sequence, observation(input_tokens=sequence * 10))
    assert group(ledger)["tokens"]["input_tokens"] == {
        "value": 30,
        "known_sum": 30,
        "unknown_calls": 0,
    }
    assert group(ledger)["calls"] == 1 and group(ledger)["cost"]["amount"] == "0.00000001"


def test_delta_request_announced_twice_is_one_charge():
    ledger = UsageLedger()
    for sequence in range(3):
        ledger.add("worker", sequence, observation(mode="delta"))
    assert group(ledger)["calls"] == 1 and group(ledger)["tokens"]["input_tokens"]["value"] == 1
    assert ledger.summary()["valid"]


@pytest.mark.parametrize("field", [*TOKEN_FIELDS, "cost"])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_unknown_values_are_not_zero(field, explicit_null):
    ledger = UsageLedger()
    unknown = observation(usage_id="unknown")
    if explicit_null:
        unknown[field] = None
    else:
        del unknown[field]
    ledger.add("worker", 0, observation())
    ledger.add("worker", 1, unknown)
    value = group(ledger)["cost"] if field == "cost" else group(ledger)["tokens"][field]
    assert value["unknown_calls"] == 1
    assert value["amount" if field == "cost" else "value"] is None
    assert value["known_amount" if field == "cost" else "known_sum"] == (
        "0.00000001" if field == "cost" else 1
    )
    assert not ledger.summary()["measurement_complete"]


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "delta"},
        {"role": "judge"},
        {"model": None},
        {"purpose": "grading"},
        {"input_tokens": 0},
    ],
)
def test_ambiguous_series_is_invalid_and_not_silently_summed(changes):
    ledger = UsageLedger()
    ledger.add("worker", 0, observation())
    ledger.add("worker", 1, observation(**changes))
    assert not ledger.summary()["valid"]
    assert group(ledger)["tokens"]["input_tokens"]["value"] is None
    assert group(ledger)["cost"]["amount"] is None


def test_delta_changed_measurement_is_a_conflict():
    ledger = UsageLedger()
    ledger.add("worker", 0, observation(mode="delta"))
    ledger.add("worker", 1, observation(mode="delta", input_tokens=2))
    assert ledger.summary()["errors"][0]["code"] == "USAGE_DELTA_CONFLICT"


def test_request_ids_are_local_to_actor_and_roles_are_separate():
    ledger = UsageLedger()
    ledger.add("worker-a", 0, observation(role="agent"))
    ledger.add("worker-b", 0, observation(role="agent"))
    ledger.add("worker-a", 1, observation(usage_id="user-call", role="user"))
    assert [item["calls"] for item in ledger.summary()["groups"]] == [2, 1]


def test_cost_keeps_exact_decimal_and_estimation_sources():
    ledger = UsageLedger()
    ledger.add(
        "worker",
        0,
        observation(
            cost={
                "currency": "USD",
                "amount": "12345678901234567890.00000000000000000001",
                "source": "provider",
            }
        ),
    )
    ledger.add(
        "worker",
        1,
        observation(
            usage_id="second",
            cost={"currency": "USD", "amount": "0.00000000000000000002", "source": "estimate"},
        ),
    )
    assert group(ledger)["cost"]["amount"] == "12345678901234567890.00000000000000000003"
    assert group(ledger)["cost"]["sources"] == ["estimate", "provider"]


def test_empty_ledger_does_not_claim_measurement_completeness():
    assert UsageLedger().summary() == {
        "groups": [],
        "valid": True,
        "errors": [],
        "measurement_complete": False,
    }


def test_latest_unknown_snapshot_does_not_reuse_earlier_known_value():
    ledger = UsageLedger()
    ledger.add("worker", 0, observation())
    ledger.add("worker", 1, observation(input_tokens=None))
    assert group(ledger)["tokens"]["input_tokens"]["value"] is None


def test_cost_outside_policy_remains_unknown_and_reported():
    ledger = UsageLedger()
    ledger.add(
        "worker", 0, observation(cost={"currency": "USD", "amount": "1" * 81, "source": "provider"})
    )
    assert not ledger.summary()["valid"] and group(ledger)["cost"]["amount"] is None
