"""Unit tests for the Bias Mitigation Toolkit (E-18) — pure logic, no DB / LLM.

Covers the prompt-injection toggles (incl. the pinned no-op string so E-02 goldens
never drift), the model-identity heuristic behind self-preference detection, and
the bias-diagnostic math.
"""

from app.quality.judge import (
    _JUDGE_SYSTEM_PROMPT,
    _bias_mitigation_block,
    _judge_system_prompt,
)
from app.quality.model_identity import (
    model_family,
    normalize_model_name,
    same_model_or_family,
)
from app.quality.bias_mitigation import (
    _clustering_diagnostic,
    _dimensions_delta,
    _gate_passed,
    _improved,
    _self_preference_diagnostic,
    _verbosity_diagnostic,
)
from app.quality.stats import stdev


# --------------------------------------------------------------------------- #
# Prompt-level mitigations
# --------------------------------------------------------------------------- #
def test_no_mitigation_is_byte_identical():
    # Pin: with no mitigations the judge prompt must equal the base constant.
    assert _judge_system_prompt(None) == _JUDGE_SYSTEM_PROMPT
    assert _judge_system_prompt({}) == _JUDGE_SYSTEM_PROMPT
    assert _judge_system_prompt({"verbosity": False, "score_clustering": False}) == _JUDGE_SYSTEM_PROMPT
    # self_preference / position never touch the prompt.
    assert _judge_system_prompt({"self_preference": True, "position": True}) == _JUDGE_SYSTEM_PROMPT


def test_mitigations_append_instructions():
    v = _judge_system_prompt({"verbosity": True})
    assert v.startswith(_JUDGE_SYSTEM_PROMPT)
    assert "ignore length" in v.lower()

    c = _judge_system_prompt({"score_clustering": True})
    assert "full 0-10 range" in c.lower()

    both = _judge_system_prompt({"verbosity": True, "score_clustering": True}).lower()
    assert "ignore length" in both and "full 0-10 range" in both
    # Deterministic order: verbosity before score_clustering.
    assert both.index("ignore length") < both.index("full 0-10 range")


def test_bias_mitigation_block_self_preference():
    flags = {"verbosity": False, "score_clustering": False, "self_preference": True, "position": False}
    # Same model → flagged with a warning.
    blk = _bias_mitigation_block(flags, "gpt-4o", "gpt-4o")
    assert blk["self_preference"]["flagged"] is True
    assert "inflated" in blk["self_preference"]["warning"]
    assert blk["position"]["status"] == "n/a"

    # Different model → not flagged.
    blk2 = _bias_mitigation_block(flags, "gpt-4o", "claude-3-5-sonnet")
    assert blk2["self_preference"]["flagged"] is False
    assert blk2["self_preference"]["warning"] is None

    # Toggle off → never flagged even for same model.
    off = {**flags, "self_preference": False}
    blk3 = _bias_mitigation_block(off, "gpt-4o", "gpt-4o")
    assert blk3["self_preference"]["flagged"] is False


# --------------------------------------------------------------------------- #
# Model identity
# --------------------------------------------------------------------------- #
def test_normalize_model_name():
    assert normalize_model_name("openai/gpt-4o") == "gpt-4o"
    assert normalize_model_name("gpt-4o-2024-08-06") == "gpt-4o"
    assert normalize_model_name("claude-3-5-sonnet-20241022") == "claude-3-5-sonnet"
    assert normalize_model_name("claude-opus-4-2025") == "claude-opus-4"
    # Meaningful version numbers are preserved.
    assert normalize_model_name("gpt-4") == "gpt-4"
    assert normalize_model_name("claude-2") == "claude-2"
    assert normalize_model_name(None) == ""
    assert normalize_model_name("") == ""


def test_model_family():
    assert model_family("gpt-4o-mini") == "gpt-4o"
    assert model_family("claude-opus-4-20250101") == "claude-opus"
    assert model_family("") == ""


def test_same_model_or_family():
    assert same_model_or_family("gpt-4o", "openai/gpt-4o") == (True, "same model")
    assert same_model_or_family("gpt-4o-2024-08-06", "gpt-4o") == (True, "same model")
    matched, kind = same_model_or_family("gpt-4o", "gpt-4o-mini")
    assert matched is True and kind == "same family"
    assert same_model_or_family("gpt-4o", "claude-3-5-sonnet") == (False, None)
    assert same_model_or_family("gpt-4o", None) == (False, None)
    assert same_model_or_family(None, "gpt-4o") == (False, None)


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def test_stdev():
    assert stdev([5, 5, 5]) == 0.0
    assert stdev([1]) is None
    assert stdev([]) is None
    assert stdev([0, 10]) == 5.0


def test_gate_passed():
    dims = {
        "correctness": {"key": "correctness", "critical": True, "threshold": 6},
        "tool": {"key": "tool", "critical": False, "threshold": 6},
    }
    # critical passes → gate passes regardless of non-critical.
    assert _gate_passed({"correctness": 8, "tool": 2}, dims) is True
    # critical fails threshold → gate fails.
    assert _gate_passed({"correctness": 3, "tool": 9}, dims) is False
    # critical failed to score → gate fails.
    assert _gate_passed({"correctness": None, "tool": 9}, dims) is False


def test_improved():
    assert _improved(0.5, 0.7) is True
    assert _improved(0.7, 0.5) is False
    assert _improved(None, 0.5) is True   # any defined improvement over nothing
    assert _improved(0.5, None) is False


def test_clustering_diagnostic():
    # Clustered OFF (all 7-8), spread out ON.
    off = [7, 8, 7, 8, 7, 8]
    on = [2, 9, 4, 8, 1, 10]
    d = _clustering_diagnostic(off, on)
    assert d["status"] == "ok"
    assert d["clustered_off"] is True
    assert d["pct_in_7_8_off"] == 1.0
    assert d["spread_on"] > d["spread_off"]
    assert d["improved"] is True

    # Below MIN_SAMPLES → insufficient_data.
    assert _clustering_diagnostic([7, 8], [1, 9])["status"] == "insufficient_data"


def test_verbosity_diagnostic():
    # judge_off strongly tracks length; judge_on tracks the human (length-blind).
    # rows: (length, off, on, human)
    rows = [
        (10, 2, 9, 9),
        (50, 5, 8, 8),
        (100, 8, 8, 8),
        (200, 10, 2, 2),
    ]
    d = _verbosity_diagnostic(rows)
    assert d["status"] == "ok"
    # ON correlation is closer to the human baseline than OFF.
    assert d["improved"] is True

    assert _verbosity_diagnostic([(1, 2, 3, 4)])["status"] == "insufficient_data"


def test_self_preference_diagnostic():
    d = _self_preference_diagnostic("gpt-4o", ["gpt-4o", "gpt-4o", "claude-3"])
    assert d["flagged"] is True
    assert d["n_self_judged"] == 2
    assert d["auto_swap"] is False
    assert "inflated" in d["warning"]

    clean = _self_preference_diagnostic("gpt-4o", ["claude-3", "claude-3"])
    assert clean["flagged"] is False
    assert clean["warning"] is None


def test_dimensions_delta():
    before = {"dimensions": [{"key": "c", "name": "C", "cohen_kappa": 0.2, "pearson": 0.3, "mean_bias": 1.0}]}
    after = {"dimensions": [{"key": "c", "name": "C", "cohen_kappa": 0.8, "pearson": 0.9, "mean_bias": 0.1}]}
    delta = _dimensions_delta(before, after)
    assert len(delta) == 1
    assert delta[0]["cohen_kappa_before"] == 0.2
    assert delta[0]["cohen_kappa_after"] == 0.8
    assert delta[0]["improved"] is True
