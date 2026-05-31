"""Unit tests for the Judge Calibration Protocol stats (E-17).

Pure agreement math — Pearson, Spearman (with ties), Cohen's kappa, the band
projection and mean bias — plus the report builder ``_compute_report``. No
network, no DB.
"""

import math

from app.quality import stats
from app.quality.stats import (
    BANDS,
    cohen_kappa,
    mean_bias,
    pearson,
    score_to_band,
    spearman,
)
from app.quality.judge_calibration import _compute_report


# --------------------------------------------------------------------------- #
# Pearson
# --------------------------------------------------------------------------- #
def test_pearson_perfect_positive():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == 1.0


def test_pearson_perfect_negative():
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == -1.0


def test_pearson_known_value():
    # Hand-checked: cov=10, var_x=10, var_y=14.8 → r = 10/sqrt(148) ≈ 0.822.
    r = pearson([1, 2, 3, 4, 5], [2, 1, 4, 3, 6])
    assert r is not None and abs(r - 0.822) < 1e-3


def test_pearson_zero_variance_returns_none():
    assert pearson([5, 5, 5, 5], [1, 2, 3, 4]) is None


def test_pearson_too_few_samples_returns_none():
    assert pearson([1, 2], [3, 4]) is None


# --------------------------------------------------------------------------- #
# Spearman
# --------------------------------------------------------------------------- #
def test_spearman_monotonic_nonlinear_is_one():
    # Strictly increasing but non-linear: Spearman = 1 while Pearson < 1.
    xs = [1, 2, 3, 4, 5]
    ys = [1, 4, 9, 16, 25]
    assert spearman(xs, ys) == 1.0
    assert pearson(xs, ys) < 1.0


def test_spearman_handles_ties():
    # Ties get average ranks; a clean monotonic relation with a tie still ~1.
    rho = spearman([1, 2, 2, 3], [10, 20, 20, 30])
    assert rho == 1.0


def test_spearman_too_few_returns_none():
    assert spearman([1, 2], [1, 2]) is None


def test_rank_average_for_ties():
    # values 2,2 occupy positions 2 and 3 (1-based) → average rank 2.5 each.
    assert stats._rank([1, 2, 2, 5]) == [1.0, 2.5, 2.5, 4.0]


# --------------------------------------------------------------------------- #
# Cohen's kappa
# --------------------------------------------------------------------------- #
def test_kappa_perfect_agreement():
    a = ["good", "bad", "improve", "good"]
    assert cohen_kappa(a, list(a), BANDS) == 1.0


def test_kappa_known_confusion():
    # 8 of 10 agree, with balanced marginals → kappa between 0 and 1.
    a = ["good", "good", "good", "good", "good", "bad", "bad", "bad", "bad", "bad"]
    b = ["good", "good", "good", "good", "bad", "bad", "bad", "bad", "bad", "good"]
    k = cohen_kappa(a, b, BANDS)
    assert k is not None and 0.5 < k < 0.7


def test_kappa_degenerate_single_label():
    # Everyone says "good": expected agreement is 1, observed is 1 → kappa 1.0.
    a = ["good", "good", "good"]
    assert cohen_kappa(a, list(a), BANDS) == 1.0


def test_kappa_too_few_returns_none():
    assert cohen_kappa(["good", "bad"], ["good", "bad"], BANDS) is None


# --------------------------------------------------------------------------- #
# score_to_band / mean_bias
# --------------------------------------------------------------------------- #
def test_score_to_band_boundaries():
    assert score_to_band(0) == "bad"
    assert score_to_band(3) == "bad"
    assert score_to_band(4) == "improve"
    assert score_to_band(7) == "improve"
    assert score_to_band(8) == "good"
    assert score_to_band(10) == "good"
    assert score_to_band(None) is None
    assert score_to_band(11) is None


def test_mean_bias():
    # judge consistently 1 point above human → +1.0.
    assert mean_bias([5, 6, 7], [4, 5, 6]) == 1.0
    assert mean_bias([], []) is None
    assert mean_bias([1, 2], [1]) is None


# --------------------------------------------------------------------------- #
# _compute_report
# --------------------------------------------------------------------------- #
def _pair(task_id, key, name, judge, human, *, verdict="approve", gate=True):
    band = score_to_band(human)
    return {
        "task_id": task_id,
        "dimension_key": key,
        "dimension_name": name,
        "judge_score": judge,
        "human_score": human,
        "band": band,
        "verdict": verdict,
        "judge_gate_passed": gate,
    }


def test_compute_report_reliable_and_unreliable_dimensions():
    pairs = []
    # "correctness": judge tracks human closely → reliable.
    for i, (j, h) in enumerate([(9, 9), (8, 8), (7, 7), (3, 3), (2, 2), (9, 8)]):
        pairs.append(_pair(f"t{i}", "correctness", "Correctness", j, h))
    # "tool_selection": judge is all over the place → not reliable.
    for i, (j, h) in enumerate([(9, 2), (2, 9), (8, 3), (3, 8), (9, 4), (1, 7)]):
        pairs.append(_pair(f"t{i}", "tool_selection", "Tool Selection", j, h))

    report = _compute_report(pairs, threshold_kappa=0.6)

    dims = {d["key"]: d for d in report["dimensions"]}
    assert dims["correctness"]["reliable"] is True
    assert dims["correctness"]["status"] == "ok"
    assert dims["correctness"]["n"] == 6
    assert dims["tool_selection"]["reliable"] is False

    recs = " ".join(report["recommendations"])
    assert "Correctness" in recs and "reliable" in recs.lower()
    assert "Tool Selection" in recs and "diverge" in recs.lower()
    assert report["sample_size"] == 12
    assert report["n_dimensions"] == 2


def test_compute_report_insufficient_data():
    pairs = [_pair("t0", "clarity", "Clarity", 8, 8), _pair("t1", "clarity", "Clarity", 7, 7)]
    report = _compute_report(pairs, threshold_kappa=0.6)
    dim = report["dimensions"][0]
    assert dim["status"] == "insufficient_data"
    assert dim["pearson"] is None
    assert dim["reliable"] is False
