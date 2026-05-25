"""Unit tests for the Variance / Robustness Harness stats (E-11).

Pure-Python distribution / percentile / tool-stability helpers — no DB, no LLM.
"""

from app.quality.runs_common import distribution as _distribution, percentile as _percentile
from app.quality.variance import _tool_stability


def test_percentile_interpolates():
    vals = [0, 10]
    assert _percentile(vals, 0) == 0
    assert _percentile(vals, 50) == 5
    assert _percentile(vals, 100) == 10
    assert _percentile([5], 50) == 5
    assert _percentile([], 50) is None


def test_distribution_basic():
    d = _distribution([2, 4, 4, 4, 5, 5, 7, 9])
    assert d["n"] == 8
    assert d["mean"] == 5.0
    assert d["min"] == 2
    assert d["max"] == 9
    assert d["p50"] == 4.5
    # population stdev of the classic dataset is 2.0
    assert d["std"] == 2.0
    assert d["values"] == [2, 4, 4, 4, 5, 5, 7, 9]


def test_distribution_drops_none_and_handles_empty():
    assert _distribution([])["n"] == 0
    d = _distribution([3, None, 3])
    assert d["n"] == 2
    assert d["std"] == 0.0  # identical values


def test_tool_stability_perfectly_reproducible():
    seqs = [["a", "b"], ["a", "b"], ["a", "b"]]
    st = _tool_stability(seqs)
    assert st["runs"] == 3
    assert st["distinct_signatures"] == 1
    assert st["modal_share"] == 1.0
    by_tool = {t["tool"]: t for t in st["per_tool"]}
    assert by_tool["a"]["mean"] == 1.0 and by_tool["a"]["std"] == 0.0
    assert by_tool["a"]["present_in_runs"] == 3


def test_tool_stability_divergent_paths():
    seqs = [["a", "b"], ["a", "b", "b"], ["a"]]
    st = _tool_stability(seqs)
    assert st["runs"] == 3
    assert st["distinct_signatures"] == 3
    assert st["modal_share"] == round(1 / 3, 3)
    by_tool = {t["tool"]: t for t in st["per_tool"]}
    # tool "b": counts across runs are [1, 2, 0] -> mean 1.0, present in 2 runs
    assert by_tool["b"]["mean"] == 1.0
    assert by_tool["b"]["present_in_runs"] == 2


def test_tool_stability_empty():
    st = _tool_stability([])
    assert st["runs"] == 0
    assert st["modal_share"] is None
    assert st["per_tool"] == []
