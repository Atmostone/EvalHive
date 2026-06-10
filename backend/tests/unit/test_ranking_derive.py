"""Unit tests for the pointwise→pairwise match derivation (E-19, build_matches).

The derivation groups scored records by benchmark case, reduces each player to its
mean score within a case, and emits one match per player-pair per case (higher
mean wins, a gap within epsilon is a tie). Records alone in a case have no opponent
and are counted unmatched.
"""

from app.quality.ranking import build_matches


def test_higher_score_wins():
    scored = [
        {"case": "c1", "player": "gpt-4o", "score": 9.0},
        {"case": "c1", "player": "claude", "score": 4.0},
    ]
    matches, meta = build_matches(scored, subject="model", epsilon=0.5)
    assert len(matches) == 1
    m = matches[0]
    # players are ordered alphabetically; claude < gpt-4o
    assert (m["player_a"], m["player_b"]) == ("claude", "gpt-4o")
    assert m["outcome"] == "b"  # gpt-4o (b) has the higher score
    assert meta["n_cases"] == 1
    assert meta["n_records_used"] == 2
    assert meta["n_unmatched"] == 0
    assert meta["n_players"] == 2


def test_epsilon_tie():
    scored = [
        {"case": "c1", "player": "a", "score": 7.0},
        {"case": "c1", "player": "b", "score": 7.4},
    ]
    matches, _ = build_matches(scored, epsilon=0.5)
    assert matches[0]["outcome"] == "tie"  # |7.4 - 7.0| <= 0.5


def test_mean_reduction_per_case():
    # 'a' appears twice in one case → reduced to its mean (8.0) before pairing.
    scored = [
        {"case": "c1", "player": "a", "score": 6.0},
        {"case": "c1", "player": "a", "score": 10.0},
        {"case": "c1", "player": "b", "score": 5.0},
    ]
    matches, meta = build_matches(scored, epsilon=0.5)
    assert len(matches) == 1  # one pair per case, not per record
    assert matches[0]["outcome"] == "a"  # mean(a)=8 > b=5
    assert meta["n_records_used"] == 3


def test_lone_competitor_is_unmatched():
    scored = [
        {"case": "c1", "player": "a", "score": 9.0},  # no opponent in c1
        {"case": "c2", "player": "a", "score": 9.0},
        {"case": "c2", "player": "b", "score": 4.0},
    ]
    matches, meta = build_matches(scored, epsilon=0.5)
    assert len(matches) == 1  # only c2 yields a match
    assert meta["n_cases"] == 1
    assert meta["n_unmatched"] == 1  # the lone 'a' record in c1


def test_subject_is_passed_through_to_meta():
    scored = [
        {"case": "c1", "player": "Reviewer", "score": 8.0},
        {"case": "c1", "player": "Researcher", "score": 6.0},
    ]
    _, meta = build_matches(scored, subject="template", epsilon=0.5)
    assert meta["subject"] == "template"


def test_empty_input():
    matches, meta = build_matches([], epsilon=0.5)
    assert matches == []
    assert meta["n_cases"] == 0
    assert meta["n_players"] == 0
