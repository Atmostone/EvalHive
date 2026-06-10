"""Aggregation Engine — Bradley-Terry / Elo ranking (E-19).

Pointwise scoring (E-02) gives one 0-10 number per task. The more robust way to
*rank* competitors is **pairwise**: collect many "A vs B" matches and aggregate
them into a global rating with a confidence interval. This module is that
aggregation engine — pure functions over a list of matches, no DB and no LLM, so
it is trivially unit-testable and reusable by the eventual pairwise framework
(E-21), which will feed it real matches instead of the pointwise-derived ones the
service layer (``app.quality.ranking``) supplies today.

A **match** is a single head-to-head comparison from ``player_a``'s perspective::

    {"player_a": "gpt-4o", "player_b": "claude-opus", "outcome": "a", "weight": 1}

``outcome`` is ``"a"`` (a won), ``"b"`` (b won), or ``"tie"``; ``weight`` (default
1) lets one record stand for several identical comparisons. Players are opaque
strings (a model ``api_name`` or a template id/name).

Two rating methods are offered, both mapped onto a shared **Elo-like scale**
(centred on :data:`INIT_RATING`, ~400 points per 10x odds) so a leaderboard reads
the same regardless of method:

- :func:`bradley_terry` — maximum-likelihood strengths via the classic
  minorization-maximization (MM) iteration. A small Bayesian ``prior`` (virtual
  win+loss against a phantom average opponent) keeps an undefeated player or a
  disconnected comparison graph from diverging.
- :func:`elo` — sequential Elo updates; since our derived matches have no natural
  order, ratings are averaged over several seeded-shuffled passes to remove the
  order dependence.

Confidence intervals come from a seeded bootstrap (:func:`_bootstrap_ci`): resample
the matches with replacement, re-fit, and take the central percentile band per
player. Everything is deterministic for a fixed ``seed`` so reports and tests are
stable.

:func:`rank` is the public entry point and the literal acceptance API
(``rank(pairwise_results, method='bt'|'elo')`` → ranked list with CI).
"""

from __future__ import annotations

import math
import random

# Elo-scale constants — a 400-point gap ≈ 10:1 expected odds, the chess convention.
INIT_RATING = 1500.0
SCALE = 400.0
DEFAULT_K = 32.0
DEFAULT_ELO_PASSES = 20
DEFAULT_RESAMPLES = 200
DEFAULT_PRIOR = 1.0
# Below this many distinct players a ranking is meaningless.
MIN_PLAYERS = 2

VALID_OUTCOMES = ("a", "b", "tie")


# --------------------------------------------------------------------------- #
# Match normalization + tallies
# --------------------------------------------------------------------------- #
def _normalize(matches: list[dict]) -> list[dict]:
    """Drop malformed / self matches and coerce weight; keep player_a/player_b.

    A match needs two distinct non-empty players and an outcome in
    :data:`VALID_OUTCOMES`; ``weight`` is a positive int (default 1). Invalid rows
    are skipped rather than raising — the source data is best-effort. Idempotent:
    already-normalized matches pass straight through, so the rating functions can
    defensively re-normalize their input without dropping it."""
    out: list[dict] = []
    for m in matches or []:
        a = m.get("player_a")
        b = m.get("player_b")
        outcome = m.get("outcome")
        if not a or not b or a == b or outcome not in VALID_OUTCOMES:
            continue
        try:
            weight = int(m.get("weight", 1) or 1)
        except (TypeError, ValueError):
            weight = 1
        if weight <= 0:
            continue
        out.append({"player_a": str(a), "player_b": str(b), "outcome": outcome, "weight": weight})
    return out


def _players(matches: list[dict]) -> list[str]:
    """Distinct player keys, sorted for deterministic ordering."""
    seen: set[str] = set()
    for m in matches:
        seen.add(m["player_a"])
        seen.add(m["player_b"])
    return sorted(seen)


def _tally(matches: list[dict]) -> dict[str, dict]:
    """Per-player win/loss/tie counts (weighted) over normalized matches."""
    tally: dict[str, dict] = {}

    def _row(p: str) -> dict:
        return tally.setdefault(p, {"wins": 0, "losses": 0, "ties": 0})

    for m in matches:
        a, b, w = m["player_a"], m["player_b"], m["weight"]
        ra, rb = _row(a), _row(b)
        if m["outcome"] == "a":
            ra["wins"] += w
            rb["losses"] += w
        elif m["outcome"] == "b":
            rb["wins"] += w
            ra["losses"] += w
        else:  # tie
            ra["ties"] += w
            rb["ties"] += w
    return tally


# --------------------------------------------------------------------------- #
# Bradley-Terry (MM iteration)
# --------------------------------------------------------------------------- #
def bradley_terry(
    matches: list[dict],
    *,
    max_iter: int = 200,
    tol: float = 1e-9,
    prior: float = DEFAULT_PRIOR,
) -> dict[str, float]:
    """Maximum-likelihood Bradley-Terry strengths, returned on the Elo scale.

    Fits per-player strengths ``p_i`` so that ``P(i beats j) = p_i / (p_i + p_j)``
    via the standard MM update ``p_i <- W_i / Σ_j n_ij / (p_i + p_j)``. Ties count
    as half a win to each side. ``prior`` adds a virtual win and loss for every
    player against a phantom average opponent (strength 1): this regularizes the
    estimate so an undefeated player — or a comparison graph that splits into
    disconnected groups — converges to a finite rating instead of running off to
    infinity. Strengths are normalized to geometric mean 1, then mapped to the Elo
    scale with :func:`_strength_to_rating`. Empty input → ``{}``."""
    matches = _normalize(matches)
    players = _players(matches)
    if not players:
        return {}

    # Weighted pairwise aggregates: wins[i][j] = weighted wins of i over j
    # (a tie adds 0.5 to both directions); n[i][j] = total games between i and j.
    wins: dict[str, dict[str, float]] = {p: {} for p in players}
    games: dict[str, dict[str, float]] = {p: {} for p in players}

    def _add(i: str, j: str, wi: float, wj: float, n: float) -> None:
        wins[i][j] = wins[i].get(j, 0.0) + wi
        wins[j][i] = wins[j].get(i, 0.0) + wj
        games[i][j] = games[i].get(j, 0.0) + n
        games[j][i] = games[j].get(i, 0.0) + n

    for m in matches:
        a, b, w = m["player_a"], m["player_b"], float(m["weight"])
        if m["outcome"] == "a":
            _add(a, b, w, 0.0, w)
        elif m["outcome"] == "b":
            _add(a, b, 0.0, w, w)
        else:
            _add(a, b, 0.5 * w, 0.5 * w, w)

    # Total weighted wins per player (+ the prior's virtual win).
    total_wins = {p: prior + sum(wins[p].values()) for p in players}

    strength = {p: 1.0 for p in players}
    for _ in range(max_iter):
        new: dict[str, float] = {}
        for p in players:
            denom = prior / (strength[p] + 1.0)  # virtual games vs phantom (strength 1)
            for j, n in games[p].items():
                denom += n / (strength[p] + strength[j])
            new[p] = total_wins[p] / denom if denom > 0 else strength[p]
        # Normalize to geometric mean 1 for numerical stability + identifiability.
        log_mean = sum(math.log(v) for v in new.values()) / len(new)
        norm = math.exp(log_mean)
        new = {p: v / norm for p, v in new.items()}
        delta = max(abs(new[p] - strength[p]) for p in players)
        strength = new
        if delta < tol:
            break

    return {p: _strength_to_rating(strength[p]) for p in players}


def _strength_to_rating(strength: float) -> float:
    """Map a positive Bradley-Terry strength (geomean-normalized to 1) onto the
    Elo scale: ``INIT_RATING + (SCALE/ln10)·ln(strength)``. Strength 1 → 1500."""
    if strength <= 0:
        return INIT_RATING
    return round(INIT_RATING + (SCALE / math.log(10.0)) * math.log(strength), 2)


# --------------------------------------------------------------------------- #
# Elo (seeded multi-pass average)
# --------------------------------------------------------------------------- #
def elo(
    matches: list[dict],
    *,
    k: float = DEFAULT_K,
    init: float = INIT_RATING,
    passes: int = DEFAULT_ELO_PASSES,
    seed: int = 0,
) -> dict[str, float]:
    """Elo ratings averaged over ``passes`` seeded-shuffled match orderings.

    A single Elo sweep is order-dependent, which is wrong for matches that carry
    no inherent chronology (our pointwise-derived ones don't). Averaging the final
    ratings across many shuffled passes removes that dependence while staying fully
    deterministic for a fixed ``seed``. Each match updates both players by
    ``k·(actual - expected)`` where ``expected`` is the logistic Elo win
    probability and ``actual`` is 1/0.5/0 for win/tie/loss. Empty input → ``{}``."""
    matches = _normalize(matches)
    players = _players(matches)
    if not players:
        return {}
    rng = random.Random(seed)
    totals = {p: 0.0 for p in players}

    for _ in range(max(1, passes)):
        rating = {p: float(init) for p in players}
        order = list(matches)
        rng.shuffle(order)
        for m in order:
            a, b = m["player_a"], m["player_b"]
            for _w in range(m["weight"]):
                ea = 1.0 / (1.0 + 10.0 ** ((rating[b] - rating[a]) / SCALE))
                if m["outcome"] == "a":
                    sa = 1.0
                elif m["outcome"] == "b":
                    sa = 0.0
                else:
                    sa = 0.5
                rating[a] += k * (sa - ea)
                rating[b] += k * ((1.0 - sa) - (1.0 - ea))
        for p in players:
            totals[p] += rating[p]

    n = max(1, passes)
    return {p: round(totals[p] / n, 2) for p in players}


# --------------------------------------------------------------------------- #
# Bootstrap confidence intervals
# --------------------------------------------------------------------------- #
def _fit(matches: list[dict], method: str, **kw) -> dict[str, float]:
    """Dispatch to the requested rating method over already-normalized matches."""
    if method == "elo":
        return elo(matches, **{k: v for k, v in kw.items() if k in ("k", "init", "passes", "seed")})
    return bradley_terry(
        matches, **{k: v for k, v in kw.items() if k in ("max_iter", "tol", "prior")}
    )


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (``pct`` in [0, 100]) of a non-empty list."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _bootstrap_ci(
    matches: list[dict],
    method: str,
    *,
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    **kw,
) -> dict[str, tuple[float, float]]:
    """Seeded bootstrap 95% CI per player.

    Resample the matches with replacement ``n_resamples`` times, re-fit each time,
    and collect each player's ratings; the CI is the [2.5, 97.5] percentile band.
    A player absent from a given resample simply contributes no sample that round.
    Fewer matches ⇒ noisier resamples ⇒ a wider band, which is exactly the
    uncertainty signal we want on the leaderboard. Deterministic for a fixed
    ``seed``."""
    players = _players(matches)
    if not players or n_resamples <= 0:
        return {p: (INIT_RATING, INIT_RATING) for p in players}

    rng = random.Random(seed)
    samples: dict[str, list[float]] = {p: [] for p in players}
    n = len(matches)
    for _ in range(n_resamples):
        resample = [matches[rng.randrange(n)] for _ in range(n)]
        fitted = _fit(resample, method, seed=seed, **kw)
        for p, r in fitted.items():
            samples[p].append(r)

    ci: dict[str, tuple[float, float]] = {}
    for p in players:
        vals = samples[p]
        if not vals:
            ci[p] = (INIT_RATING, INIT_RATING)
        else:
            ci[p] = (round(_percentile(vals, 2.5), 2), round(_percentile(vals, 97.5), 2))
    return ci


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def rank(
    matches: list[dict],
    *,
    method: str = "bt",
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    tie_epsilon: float | None = None,
    **kw,
) -> dict:
    """Rank players from pairwise matches — the E-19 acceptance API.

    ``method`` is ``"bt"`` (Bradley-Terry, default) or ``"elo"``. Returns the
    leaderboard report (see the module/`ranking` docs for the full shape):
    a ``players`` list sorted by rating descending, each with a bootstrap CI, the
    1-based ``rank``, win/loss/tie tallies and win rate. ``status`` is ``"empty"``
    (no valid matches), ``"insufficient_data"`` (< :data:`MIN_PLAYERS` players), or
    ``"ok"``. Deterministic for a fixed ``seed``. ``tie_epsilon`` is passed through
    untouched into ``params`` for provenance (the derivation applies it upstream)."""
    method = "elo" if method == "elo" else "bt"
    norm = _normalize(matches)
    players = _players(norm)

    params = {
        "method": method,
        "n_resamples": n_resamples,
        "seed": seed,
        "tie_epsilon": tie_epsilon,
    }
    if method == "elo":
        params["k"] = kw.get("k", DEFAULT_K)
        params["passes"] = kw.get("passes", DEFAULT_ELO_PASSES)
    else:
        params["prior"] = kw.get("prior", DEFAULT_PRIOR)

    if not norm:
        return {
            "schema_version": 1,
            "method": method,
            "status": "empty",
            "n_matches": 0,
            "n_players": 0,
            "players": [],
            "params": params,
        }
    if len(players) < MIN_PLAYERS:
        return {
            "schema_version": 1,
            "method": method,
            "status": "insufficient_data",
            "n_matches": len(norm),
            "n_players": len(players),
            "players": [],
            "params": params,
        }

    ratings = _fit(norm, method, seed=seed, **kw)
    ci = _bootstrap_ci(norm, method, n_resamples=n_resamples, seed=seed, **kw)
    tally = _tally(norm)

    rows: list[dict] = []
    for p in players:
        t = tally.get(p, {"wins": 0, "losses": 0, "ties": 0})
        n_p = t["wins"] + t["losses"] + t["ties"]
        decisive = t["wins"] + t["losses"]
        win_rate = round(t["wins"] / decisive, 4) if decisive else None
        lo, hi = ci.get(p, (ratings[p], ratings[p]))
        rows.append(
            {
                "player": p,
                "rating": ratings[p],
                "ci_low": lo,
                "ci_high": hi,
                "wins": t["wins"],
                "losses": t["losses"],
                "ties": t["ties"],
                "n_matches": n_p,
                "win_rate": win_rate,
            }
        )

    # Sort by rating desc, tie-break by player name for determinism.
    rows.sort(key=lambda r: (-r["rating"], r["player"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    return {
        "schema_version": 1,
        "method": method,
        "status": "ok",
        "n_matches": len(norm),
        "n_players": len(players),
        "players": rows,
        "params": params,
    }
