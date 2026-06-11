"""Significance tests added for SPA-40 experiment reports (pure stats)."""

from app.quality.stats import (
    _student_t_two_sided_p,
    mann_whitney_u,
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
