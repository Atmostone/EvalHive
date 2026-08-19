"""Significance tests added for SPA-40 experiment reports (pure stats)."""

import math

import pytest

from app.quality.stats import (
    _student_t_two_sided_p,
    mann_whitney_u,
    rank_auc,
    welch_t_test,
)


class TestWelch:
    def test_known_case(self):
        # Equal variances, shifted by 1: t = -1.0, Welch df = 8,
        # two-sided p = P(|T| > 1 | df=8) ≈ 0.3466 (standard t-table value).
        res = welch_t_test([1, 2, 3, 4, 5], [2, 3, 4, 5, 6])
        assert res is not None
        assert res["t"] == -1.0
        assert res["df"] == 8.0
        assert abs(res["p"] - 0.3466) < 5e-4
        assert res["mean_a"] == 3.0
        assert res["mean_b"] == 4.0

    def test_t_cdf_matches_critical_values(self):
        # Standard two-sided 5% critical values of the t distribution.
        assert abs(_student_t_two_sided_p(12.706, 1) - 0.05) < 1e-3
        assert abs(_student_t_two_sided_p(2.776, 4) - 0.05) < 1e-3
        assert abs(_student_t_two_sided_p(2.228, 10) - 0.05) < 1e-3
        # Large df converges to the normal distribution.
        assert abs(_student_t_two_sided_p(1.96, 1e6) - 0.05) < 2e-4
        # t = 0 → p = 1.
        assert _student_t_two_sided_p(0.0, 8) == 1.0

    def test_symmetry_in_sign(self):
        a, b = [1.0, 2.0, 3.0, 9.0], [4.0, 5.0, 6.0, 7.0]
        assert welch_t_test(a, b)["p"] == welch_t_test(b, a)["p"]

    def test_small_groups_return_none(self):
        assert welch_t_test([1, 2], [1, 2, 3]) is None
        assert welch_t_test([1, 2, 3], [1, 2]) is None

    def test_both_groups_flat_return_none(self):
        assert welch_t_test([5, 5, 5], [3, 3, 3]) is None

    def test_one_flat_group_still_computes(self):
        res = welch_t_test([5, 5, 5, 5], [1, 2, 3, 4])
        assert res is not None
        assert res["p"] < 0.05

    def test_identical_groups_not_significant(self):
        res = welch_t_test([1, 2, 3, 4], [1, 2, 3, 4])
        assert res["t"] == 0.0
        assert res["p"] == 1.0


class TestMannWhitney:
    def test_complete_separation(self):
        # u = 0; normal approximation with continuity correction:
        # z = (0 - 8 + 0.5) / sqrt(12) ≈ -2.165 → p ≈ 0.0304.
        res = mann_whitney_u([1, 2, 3, 4], [5, 6, 7, 8])
        assert res is not None
        assert res["u"] == 0.0
        assert abs(res["p"] - 0.0304) < 2e-3
        assert res["approx"] is True

    def test_symmetric_in_arguments(self):
        a, b = [1.0, 3.0, 5.0, 7.0], [2.0, 4.0, 6.0, 8.0]
        ra, rb = mann_whitney_u(a, b), mann_whitney_u(b, a)
        assert ra["u"] == rb["u"]
        assert ra["p"] == rb["p"]

    def test_ties_are_corrected_not_fatal(self):
        res = mann_whitney_u([1, 1, 2, 2, 3], [2, 2, 3, 3, 4])
        assert res is not None
        assert 0.0 < res["p"] <= 1.0

    def test_all_identical_returns_none(self):
        assert mann_whitney_u([2, 2, 2, 2], [2, 2, 2, 2]) is None

    def test_small_groups_return_none(self):
        assert mann_whitney_u([1, 2, 3], [1, 2, 3, 4]) is None
        assert mann_whitney_u([1, 2, 3, 4], [1, 2, 3]) is None

    def test_overlapping_groups_not_significant(self):
        res = mann_whitney_u([1, 3, 5, 7], [2, 4, 6, 8])
        assert res["p"] > 0.5


class TestRankAuc:
    """P(a random positive outranks a random negative), ties ½ — the
    threshold-free judge↔checker statistic (SPA-87)."""

    def test_perfect_separation(self):
        assert rank_auc([6.0, 7.0, 8.0], [1.0, 2.0]) == 1.0

    def test_perfectly_inverted(self):
        # Below 0.5 = the judge ranks them BACKWARDS, which a 2×2 at one cut-off
        # can report as respectable agreement.
        assert rank_auc([1.0, 2.0], [6.0, 7.0, 8.0]) == 0.0

    def test_all_ties_is_chance(self):
        assert rank_auc([5.0, 5.0], [5.0, 5.0]) == 0.5

    def test_one_tie_counts_as_half(self):
        # pairs: (5,5)=½, (5,1)=1 → 1.5 / 2
        assert rank_auc([5.0], [5.0, 1.0]) == 0.75

    def test_empty_group_has_no_answer(self):
        assert rank_auc([], [1.0]) is None
        assert rank_auc([1.0], []) is None
        assert rank_auc([], []) is None

    def test_agrees_with_mann_whitney_direction(self):
        pos, neg = [9.0, 8.0, 7.0, 6.0, 5.5], [4.0, 3.0, 2.0, 1.0, 0.5]
        assert rank_auc(pos, neg) == 1.0
        assert mann_whitney_u(pos, neg)["p"] < 0.05

    def test_no_minimum_sample_size(self):
        # Deliberately unlike mann_whitney_u: a descriptive statistic reports on
        # whatever data exists, and the counts beside it say how much to trust it.
        assert rank_auc([8.0], [2.0]) == 1.0


# --- SPA-62: paper-grade statistics -----------------------------------------


def test_pairing_sees_a_shift_that_welch_cannot():
    """The reason the whole thing is worth doing. Four cases of very different
    difficulty, config B better by exactly 1.0 on every one of them. Unpaired,
    the between-case spread swamps it; paired, the effect is unmissable."""
    from app.quality.stats import paired_t_test, welch_t_test

    a = [2.0, 5.0, 8.0, 9.0]
    b = [3.1, 6.0, 8.9, 10.2]  # better by ~1.0 everywhere, with a little jitter
    assert welch_t_test(a, b)["p"] > 0.5, "unpaired: invisible"
    paired = paired_t_test(list(zip(a, b)))
    assert paired["p"] < 0.001, "paired: unmistakable"
    assert paired["mean_diff"] == -1.05
    assert paired["n_pairs"] == 4


def test_paired_t_needs_variation_in_the_differences():
    from app.quality.stats import paired_t_test

    # Every difference identical → zero variance → nothing to test. The
    # difference itself is still knowable; the test is not.
    assert paired_t_test([(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]) is None
    assert paired_t_test([(1.0, 2.0), (3.0, 5.0)]) is None  # below MIN_SAMPLES


def test_benjamini_hochberg_matches_the_hand_calculation():
    from app.quality.stats import benjamini_hochberg

    # m=3, all p·m/rank equal → all q equal.
    assert benjamini_hochberg([0.01, 0.02, 0.03]) == [0.03, 0.03, 0.03]
    # m=2: 0.001·2/1 = 0.002 ; 0.5·2/2 = 0.5
    assert benjamini_hochberg([0.001, 0.5]) == [0.002, 0.5]
    # Monotone: a q is never larger than the q of a bigger p.
    q = benjamini_hochberg([0.04, 0.01, 0.9, 0.2])
    assert q == sorted(q, key=lambda x: x) or all(
        q[i] <= q[j] for i in range(4) for j in range(4)
        if [0.04, 0.01, 0.9, 0.2][i] <= [0.04, 0.01, 0.9, 0.2][j]
    )
    assert benjamini_hochberg([]) == []


def test_the_audits_own_false_positive_does_not_survive_correction():
    """Эксп 5b reported one nominally significant row, `dim:originality` at
    Welch p = 0.0399, among 48 tests. It is the platform's own best example of a
    finding that exists because enough coins were flipped."""
    from app.quality.stats import benjamini_hochberg

    pvalues = [0.0399] + [0.2 + 0.01 * i for i in range(47)]
    assert benjamini_hochberg(pvalues)[0] > 0.05, "must not survive FDR control"


def test_tost_turns_absence_of_evidence_into_a_claim():
    from app.quality.stats import tost_equivalence

    # Differences well inside ±1.0 and consistent → equivalent.
    tight = [(5.0, 5.1), (6.0, 5.9), (7.0, 7.05), (8.0, 7.95), (4.0, 4.1)]
    assert tost_equivalence(tight, margin=1.0)["equivalent"] is True
    # A real 5-point gap is not equivalence, however narrow the spread.
    wide = [(2.0, 7.0), (3.0, 8.0), (4.0, 9.1), (5.0, 9.9), (1.0, 6.1)]
    assert tost_equivalence(wide, margin=1.0)["equivalent"] is False
    # A margin has to be a positive number to mean anything.
    assert tost_equivalence(tight, margin=0) is None


def test_effect_sizes_are_not_interchangeable():
    from app.quality.stats import hedges_g, paired_effect_size

    a = [2.0, 5.0, 8.0, 9.0]
    b = [3.1, 6.0, 8.9, 10.2]
    # Unpaired: standardised by the spread ACROSS cases — tiny here.
    assert abs(hedges_g(a, b)) < 0.5
    # Paired: standardised by the spread of the DIFFERENCES — enormous here,
    # because the differences barely vary. Same data, different estimands.
    assert abs(paired_effect_size(list(zip(a, b)))) > 5

    # Hedges' g is Cohen's d times the small-sample correction, so always smaller.
    far_a, far_b = [1.0, 2.0, 3.0, 4.0], [8.0, 9.0, 10.0, 11.0]
    pooled_sd = math.sqrt(5 / 3)  # both groups have sample variance 5/3
    d = (sum(far_a) / 4 - sum(far_b) / 4) / pooled_sd
    j = 1 - 3 / (4 * (len(far_a) + len(far_b) - 2) - 1)
    assert hedges_g(far_a, far_b) == pytest.approx(d * j, abs=1e-3)
    assert abs(hedges_g(far_a, far_b)) < abs(d)


def test_a_bootstrap_interval_is_reproducible_and_brackets_its_estimate():
    from app.quality.stats import bootstrap_diff_ci

    pairs = [(5.0, 4.0), (6.0, 4.5), (7.0, 6.0), (8.0, 6.5), (4.0, 3.0)]
    first = bootstrap_diff_ci(pairs)
    assert first == bootstrap_diff_ci(pairs), "seeded: same data, same interval"
    assert first["lo"] <= first["mean_diff"] <= first["hi"]
    assert first["lo"] > 0, "every case moved the same way — the CI excludes zero"


def test_wilcoxon_needs_enough_nonzero_pairs():
    from app.quality.stats import wilcoxon_signed_rank

    assert wilcoxon_signed_rank([(2.0, 1.0)] * 5) is None
    consistent = [(v + 1.0, v) for v in (1.0, 2.5, 3.0, 4.5, 6.0, 7.5, 8.0)]
    out = wilcoxon_signed_rank(consistent)
    assert out["p"] < 0.05 and out["n_pairs"] == 7


def test_wilson_reports_an_interval_where_the_textbook_one_asserts_certainty():
    """A zero cell is the case the normal approximation gets exactly backwards.

    ``p ± z·sqrt(p(1-p)/n)`` collapses to [0, 0] on 0/12 — certainty from no
    evidence — which is the opposite of what an interval on an empty over-credit
    cell is for."""
    from app.quality.stats import wilson_interval

    empty = wilson_interval(0, 12)
    assert empty["p"] == 0.0
    assert empty["lo"] == 0.0 and empty["hi"] > 0.2

    full = wilson_interval(12, 12)
    assert full["hi"] == 1.0 and full["lo"] < 0.8

    half = wilson_interval(6, 12)
    assert half["lo"] < 0.5 < half["hi"]
    assert wilson_interval(3, 0) is None
    assert wilson_interval(5, 3) is None


def test_a_kappa_interval_shows_the_gate_standing_on_one_leg():
    """κ = 0.6 is a cut-off the reliability gate acts on. At the n an experiment
    produces, the interval around a point estimate near it reaches both ways —
    which is the argument for carrying the interval, not for moving the line."""
    from app.quality.stats import BANDS, bootstrap_kappa_ci, cohen_kappa

    judge = ["good"] * 8 + ["improve"] * 7 + ["bad"] * 5
    human = ["good"] * 7 + ["improve"] + ["improve"] * 5 + ["bad"] * 2 + ["bad"] * 5
    point = cohen_kappa(judge, human, BANDS)
    ci = bootstrap_kappa_ci(judge, human, BANDS)

    assert ci["kappa"] == point
    assert ci["lo"] <= point <= ci["hi"]
    assert ci["hi"] - ci["lo"] > 0.2, "20 ratings do not pin kappa down"
    assert ci == bootstrap_kappa_ci(judge, human, BANDS), "seeded"
    assert bootstrap_kappa_ci(["good", "bad"], ["good", "bad"], BANDS) is None


def test_auc_and_unpaired_difference_carry_intervals():
    from app.quality.stats import bootstrap_auc_ci, bootstrap_unpaired_diff_ci

    passed = [8.0, 7.5, 9.0, 8.5, 7.0, 8.2]
    failed = [6.0, 5.5, 7.2, 4.0, 6.5, 5.0]
    auc = bootstrap_auc_ci(passed, failed)
    assert auc["lo"] <= auc["auc"] <= auc["hi"]
    assert auc["lo"] > 0.5, "the judge does separate them, and the interval agrees"

    diff = bootstrap_unpaired_diff_ci(passed, failed)
    assert diff["lo"] <= diff["mean_diff"] <= diff["hi"]
    assert bootstrap_unpaired_diff_ci([1.0, 2.0], failed) is None


def test_power_says_what_the_design_could_and_could_not_have_seen():
    """A non-significant row without these two numbers is an unfalsifiable shrug:
    «no difference found» and «this design could never have found one» look the
    same on the page and are not the same claim."""
    from app.quality.stats import paired_power, unpaired_power

    noisy = [(8.0, 7.9), (6.0, 6.4), (9.0, 8.6), (4.0, 4.3)]
    pw = paired_power(noisy)
    assert pw["paired"] is True and pw["n"] == 4
    # Four noisy cases cannot resolve the difference they actually showed.
    assert pw["mde"] > abs(pw["observed_diff"])
    assert pw["n_required"] > pw["n"]

    clean = [(8.0, 7.0), (6.0, 5.1), (9.0, 7.9), (4.0, 3.05)]
    assert paired_power(clean)["mde"] < abs(paired_power(clean)["observed_diff"])

    up = unpaired_power([8.0, 7.5, 9.0, 8.5], [6.0, 5.5, 7.2, 4.0])
    assert up["paired"] is False and up["n_required"] >= 2
    assert unpaired_power([1.0], [2.0, 3.0]) is None
