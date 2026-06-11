"""Configuration-matrix expansion for SPA-40 experiments (pure)."""

import pytest

from app.quality.experiments import MAX_CONFIGS, expand_matrix

TPL = "11111111-1111-1111-1111-111111111111"


def test_explicit_configurations_get_keys_and_labels():
    configs = expand_matrix(
        [
            {"template_id": TPL, "model_id": "m-1", "label": "baseline"},
            {"template_id": TPL, "model_id": "m-2"},
        ]
    )
    assert [c["config_key"] for c in configs] == ["cfg-01", "cfg-02"]
    assert configs[0]["label"] == "baseline"
    assert "model=m-2" in configs[1]["label"]
    assert "orch=off" in configs[1]["label"]
    assert configs[0]["fingerprint"] != configs[1]["fingerprint"]
    assert all(c["orchestrator"] is False for c in configs)


def test_axes_cartesian_expansion():
    configs = expand_matrix(
        None,
        axes={
            "template_id": [TPL],
            "model_id": ["m-1", "m-2"],
            "temperature": [0.0, 0.7],
        },
    )
    assert len(configs) == 4
    combos = {(c["model_id"], c["temperature"]) for c in configs}
    assert combos == {("m-1", 0.0), ("m-1", 0.7), ("m-2", 0.0), ("m-2", 0.7)}


def test_explicit_plus_axes_dedupes_by_fingerprint():
    configs = expand_matrix(
        [{"template_id": TPL, "model_id": "m-1", "temperature": 0.0, "label": "named"}],
        axes={"template_id": [TPL], "model_id": ["m-1", "m-2"], "temperature": [0.0]},
    )
    # The explicit config equals one axes combo → 2 total, explicit label wins.
    assert len(configs) == 2
    assert configs[0]["label"] == "named"


def test_orchestrator_on_off_validation():
    with pytest.raises(ValueError, match="requires template_id"):
        expand_matrix([{"model_id": "m-1"}])
    with pytest.raises(ValueError, match="must not pin template_id"):
        expand_matrix([{"orchestrator": True, "template_id": TPL}])
    with pytest.raises(ValueError, match="tools_override"):
        expand_matrix([{"orchestrator": True, "tools_override": {"disable": ["x"]}}])
    # Valid pair: off pins a template, on does not.
    configs = expand_matrix(
        [{"template_id": TPL}, {"orchestrator": True, "model_id": "m-1"}]
    )
    assert [c["orchestrator"] for c in configs] == [False, True]


def test_invalid_memory_mode_and_temperature():
    with pytest.raises(ValueError, match="memory_mode"):
        expand_matrix([{"template_id": TPL, "memory_mode": "ram"}])
    with pytest.raises(ValueError, match="temperature"):
        expand_matrix([{"template_id": TPL, "temperature": 3.5}])


def test_unknown_axis_rejected():
    with pytest.raises(ValueError, match="unknown axes"):
        expand_matrix(None, axes={"template_id": [TPL], "fan_speed": [1, 2]})


def test_empty_matrix_rejected():
    with pytest.raises(ValueError, match="at least one configuration"):
        expand_matrix([], axes=None)


def test_too_many_configurations_rejected():
    axes = {"template_id": [TPL], "seed": list(range(MAX_CONFIGS + 1))}
    with pytest.raises(ValueError, match="too many configurations"):
        expand_matrix(None, axes=axes)


def test_fingerprint_is_order_insensitive():
    a = expand_matrix([{"template_id": TPL, "model_id": "m-1", "temperature": 0.5}])
    b = expand_matrix([{"temperature": 0.5, "model_id": "m-1", "template_id": TPL}])
    assert a[0]["fingerprint"] == b[0]["fingerprint"]
