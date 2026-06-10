"""Unit tests for the pure Aggregation Engine (E-19).

Covers the Bradley-Terry and Elo rating methods, the bootstrap confidence
interval, and the ``rank`` entry point: monotonicity/transitivity of ratings,
convergence of an undefeated player (the prior), CI containment + width, tie/weight
handling, status edges, and full determinism for a fixed seed.
"""

import math

from app.quality.aggregation import (
    INIT_RATING,
    bradley_terry,
    elo,
    rank,
)


def _beats(a, b, n=1):
    return [{"player_a": a, "player_b": b, "outcome": "a"} for _ in range(n)]


# --------------------------------------------------------------------------- #
# Bradley-Terry
# --------------------------------------------------------------------------- #
def test_bt_monotonic():
    ratings = bradley_terry(_beats("a", "b", 5))
    assert ratings["a"] > ratings["b"]


def test_bt_transitive():
    matches = _beats("a", "b", 4) + _beats("b", "c", 4) + _beats("a", "c", 4)
    ratings = bradley_terry(matches)
    assert ratings["a"] > ratings["b"] > ratings["c"]


def test_bt_symmetric_equal():
    # A round-robin where everyone splits 1-1 → identical strengths → equal ratings.
    matches = (
        _beats("a", "b") + _beats("b", "a") + _beats("b", "c") + _beats("c", "b")
        + _beats("a", "c") + _beats("c", "a")
    )
    ratings = bradley_terry(matches)
    assert abs(ratings["a"] - ratings["b"]) < 1e-6
    assert abs(ratings["b"] - ratings["c"]) < 1e-6
    # Geometric-mean-1 strengths map to the init rating.
    assert abs(ratings["a"] - INIT_RATING) < 1e-6


def test_bt_undefeated_converges():
    # 'a' never loses; without the prior the MLE strength runs to infinity. The
    # prior must keep every rating finite and 'a' on top.
    matches = _beats("a", "b", 10) + _beats("a", "c", 10) + _beats("b", "c", 3)
    ratings = bradley_terry(matches)
    for v in ratings.values():
        assert math.isfinite(v)
    assert ratings["a"] == max(ratings.values())


# --------------------------------------------------------------------------- #
# Elo
# --------------------------------------------------------------------------- #
def test_elo_ordering():
    ratings = elo(_beats("a", "b", 8), seed=0)
    assert ratings["a"] > INIT_RATING > ratings["b"]


def test_elo_deterministic_with_seed():
    matches = _beats("a", "b", 3) + _beats("b", "c", 3) + _beats("a", "c", 2)
    assert elo(matches, seed=7) == elo(matches, seed=7)


# --------------------------------------------------------------------------- #
# rank(): status, CI, determinism
# --------------------------------------------------------------------------- #
def test_rank_empty():
    out = rank([])
    assert out["status"] == "empty"
    assert out["players"] == []
    assert out["n_matches"] == 0


def test_rank_single_player_insufficient():
    # Self-matches are dropped, leaving < 2 players.
    out = rank([{"player_a": "a", "player_b": "a", "outcome": "a"}])
    assert out["status"] in ("empty", "insufficient_data")
    assert out["players"] == []


def test_rank_basic_leaderboard():
    out = rank(_beats("a", "b", 5), method="bt")
    assert out["status"] == "ok"
    assert out["n_players"] == 2
    top = out["players"][0]
    assert top["player"] == "a"
    assert top["rank"] == 1
    assert top["wins"] == 5 and top["losses"] == 0
    assert top["win_rate"] == 1.0
    # ranks are 1-based and contiguous
    assert [p["rank"] for p in out["players"]] == [1, 2]


def test_ci_contains_rating():
    out = rank(_beats("a", "b", 4) + _beats("b", "c", 4) + _beats("a", "c", 4))
    for p in out["players"]:
        assert p["ci_low"] <= p["rating"] <= p["ci_high"]


def test_ci_wider_with_fewer_matches():
    few = rank(_beats("a", "b", 2), seed=0, n_resamples=200)
    many = rank(_beats("a", "b", 40), seed=0, n_resamples=200)

    def width(out):
        p = next(pl for pl in out["players"] if pl["player"] == "a")
        return p["ci_high"] - p["ci_low"]

    assert width(few) >= width(many)


def test_tie_handling():
    out = rank(
        [{"player_a": "a", "player_b": "b", "outcome": "tie"} for _ in range(4)],
        method="bt",
    )
    a = next(p for p in out["players"] if p["player"] == "a")
    b = next(p for p in out["players"] if p["player"] == "b")
    assert a["ties"] == 4 and b["ties"] == 4
    assert a["wins"] == 0 and a["losses"] == 0
    assert a["win_rate"] is None  # no decisive games
    assert abs(a["rating"] - b["rating"]) < 1e-6


def test_weight_collapses_repeats():
    weighted = rank([{"player_a": "a", "player_b": "b", "outcome": "a", "weight": 5}], seed=0)
    expanded = rank(_beats("a", "b", 5), seed=0)
    wa = next(p for p in weighted["players"] if p["player"] == "a")
    ea = next(p for p in expanded["players"] if p["player"] == "a")
    assert wa["wins"] == ea["wins"] == 5


def test_rank_deterministic_full_report():
    matches = _beats("a", "b", 3) + _beats("b", "c", 2) + _beats("c", "a", 1)
    assert rank(matches, method="elo", seed=3) == rank(matches, method="elo", seed=3)


def test_invalid_matches_dropped():
    matches = [
        {"player_a": "a", "player_b": "b", "outcome": "a"},
        {"player_a": "a", "player_b": "a", "outcome": "a"},  # self
        {"player_a": "", "player_b": "b", "outcome": "a"},  # empty
        {"player_a": "a", "player_b": "b", "outcome": "draw"},  # bad outcome
    ]
    out = rank(matches)
    assert out["n_matches"] == 1
