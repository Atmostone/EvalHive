"""Small pure-Python statistics helpers (E-17 agreement, SPA-40 significance).

E-17 validates the LLM judge against humans purely from already-stored scores;
the Experiment Runner (SPA-40) marks configuration differences as significant or
not. Both need a handful of statistics and nothing else. The project carries
no scipy/numpy, and these primitives are tiny and easy to unit-test, so they live
here as plain functions over ``list[float]`` / ``list[str]`` with no dependencies.

Convention shared with the rest of ``app.quality``: a metric that cannot be
computed returns ``None`` rather than raising. The threshold for "enough data" is
:data:`MIN_SAMPLES`; below it every correlation/agreement returns ``None`` and the
caller marks the dimension ``insufficient_data``.
"""

from __future__ import annotations

import math
import random

# Below this many paired observations a correlation/kappa is not meaningful.
MIN_SAMPLES = 3

# The categorical projection of a 0–10 score, matching the human-feedback bands
# (see app.quality.feedback): bad 0–3, improve 4–7, good 8–10.
BANDS = ["bad", "improve", "good"]


def score_to_band(score: float | None) -> str | None:
    """Project a 0–10 score onto the three human-feedback bands.

    Mirrors the band cuts used for human feedback: ``bad`` 0–3, ``improve`` 4–7,
    ``good`` 8–10. The judge can emit 0, which counts as ``bad``. ``None`` or an
    out-of-range value yields ``None``."""
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s < 0 or s > 10:
        return None
    if s < 4:
        return "bad"
    if s < 8:
        return "improve"
    return "good"


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient, or ``None`` when undefined.

    Returns ``None`` for fewer than :data:`MIN_SAMPLES` pairs or when either side
    has zero variance (a flat series has no linear relationship to report)."""
    n = len(xs)
    if n != len(ys) or n < MIN_SAMPLES:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return round(cov / math.sqrt(vx * vy), 4)


def _rank(values: list[float]) -> list[float]:
    """Fractional ranks (1-based) with ties resolved to their average rank."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1  # average of positions i..j, converted to 1-based
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def rank_auc(positive: list[float], negative: list[float]) -> float | None:
    """P(a random ``positive`` scores above a random ``negative``), ties = ½.

    The area under the ROC curve, computed from rank sums — the normalized
    Mann-Whitney U, so it needs no threshold to exist. That is the point of it
    here (SPA-87): «does the judge tell the checker's passes from its failures»
    is answered once, instead of once per cut-off, so the headline cannot move
    when someone picks a different one. 0.5 is chance; below 0.5 the judge ranks
    them backwards, which is information a 2×2 at one threshold can hide.

    ``None`` when either group is empty — with nothing to compare, there is no
    discrimination to report. Unlike :func:`mann_whitney_u` this has no minimum
    sample size, because it is a descriptive statistic and not a significance
    test; how much to trust it is a question for the counts reported next to it.
    """
    n_pos, n_neg = len(positive), len(negative)
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = _rank(list(positive) + list(negative))
    rank_sum_pos = sum(ranks[:n_pos])
    u_pos = rank_sum_pos - n_pos * (n_pos + 1) / 2.0
    return round(u_pos / (n_pos * n_neg), 4)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (Pearson on average-tie ranks)."""
    n = len(xs)
    if n != len(ys) or n < MIN_SAMPLES:
        return None
    return pearson(_rank(xs), _rank(ys))


def cohen_kappa(a: list[str], b: list[str], labels: list[str]) -> float | None:
    """Cohen's kappa for two categorical raters over a fixed ``labels`` set.

    ``None`` below :data:`MIN_SAMPLES`. When the labels are perfectly predictable
    from the marginals (expected agreement ``pe == 1``) kappa is undefined, so we
    return ``1.0`` if the raters fully agree and ``0.0`` otherwise rather than
    dividing by zero."""
    n = len(a)
    if n != len(b) or n < MIN_SAMPLES:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = 0.0
    for lab in labels:
        pa = sum(1 for x in a if x == lab) / n
        pb = sum(1 for y in b if y == lab) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return round((po - pe) / (1 - pe), 4)


def stdev(xs: list[float]) -> float | None:
    """Population standard deviation, or ``None`` for fewer than two values.

    Used by the Bias Mitigation Toolkit (E-18) as a score-spread metric: a judge
    with score-clustering bias produces a low spread (everything bunched at 7-8)."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / n
    return round(math.sqrt(var), 4)


def mean_bias(judge: list[float], human: list[float]) -> float | None:
    """Mean signed gap ``judge - human``; positive means the judge scores higher.

    ``None`` for empty or mismatched inputs."""
    n = len(judge)
    if n == 0 or n != len(human):
        return None
    return round(sum(j - h for j, h in zip(judge, human)) / n, 3)


# --- significance tests (SPA-40 experiment reports) -------------------------

# The Mann-Whitney normal approximation is meaningless on tiny groups; below
# this per-group size we return None (and the report shows no marker).
MIN_MW_SAMPLES = 4


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (Lentz's method)."""
    max_iter = 300
    eps = 3e-12
    fpmin = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _student_t_two_sided_p(t: float, df: float) -> float:
    """Exact two-sided p-value for Student's t: P(|T| > |t|) with ``df`` dof."""
    return _betai(df / 2.0, 0.5, df / (df + t * t))


def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def welch_t_test(a: list[float], b: list[float]) -> dict | None:
    """Welch's unequal-variances t-test, two-sided, exact p (no approximation).

    Returns ``{"t", "df", "p", "mean_a", "mean_b"}``. ``None`` when either
    group has fewer than :data:`MIN_SAMPLES` values, or when both groups have
    zero variance (nothing to test against — Mann-Whitney still applies)."""
    na, nb = len(a), len(b)
    if na < MIN_SAMPLES or nb < MIN_SAMPLES:
        return None
    ma = sum(a) / na
    mb = sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return None
    t = (ma - mb) / math.sqrt(se2)
    df = se2**2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
    p = _student_t_two_sided_p(t, df)
    return {
        "t": round(t, 4),
        "df": round(df, 2),
        "p": round(p, 6),
        "mean_a": round(ma, 4),
        "mean_b": round(mb, 4),
    }


def mann_whitney_u(a: list[float], b: list[float]) -> dict | None:
    """Mann-Whitney U test, two-sided normal approximation with tie correction.

    Returns ``{"u", "z", "p", "approx": True}``. ``None`` when either group has
    fewer than :data:`MIN_MW_SAMPLES` values or all values are identical. The
    approximation is weak below n≈8 per group — results carry ``approx: True``
    and Welch (exact) is the primary significance signal in reports."""
    na, nb = len(a), len(b)
    if na < MIN_MW_SAMPLES or nb < MIN_MW_SAMPLES:
        return None
    combined = list(a) + list(b)
    ranks = _rank(combined)
    r_a = sum(ranks[:na])
    u1 = r_a - na * (na + 1) / 2.0
    u = min(u1, na * nb - u1)
    n = na + nb
    counts: dict[float, int] = {}
    for v in combined:
        counts[v] = counts.get(v, 0) + 1
    tie_term = sum(c**3 - c for c in counts.values() if c > 1)
    sigma2 = na * nb / 12.0 * ((n + 1) - tie_term / (n * (n - 1)))
    if sigma2 <= 0:
        return None
    mu = na * nb / 2.0
    z = (u - mu + 0.5) / math.sqrt(sigma2)  # continuity correction
    p = min(1.0, 2.0 * _phi(z))
    return {"u": round(u, 1), "z": round(z, 4), "p": round(p, 6), "approx": True}


# --- paper-grade statistics (SPA-62) ----------------------------------------
# The experiment matrix runs the SAME cases across every configuration, which
# makes it a paired design — and pairing is not a refinement here, it is the
# difference between a test that can see an effect and one that cannot. Two
# configs differing by a constant on every case look identical to Welch when the
# between-case spread is larger than the between-config one, which on a 4-case
# matrix it almost always is.

# Below this many pairs the signed-rank normal approximation is meaningless.
MIN_WILCOXON_PAIRS = 6
# Percentile-bootstrap resamples for a difference CI. Seeded, so a report is
# reproducible: an interval that moves between two runs of the same data is not
# evidence of anything.
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 0


def paired_t_test(pairs: list[tuple[float, float]]) -> dict | None:
    """Paired-samples t-test over ``(a, b)`` observations of the same case.

    Tests the mean of the within-case differences against zero, so every source
    of variation the two configurations share — a hard case, a lenient rubric
    dimension — cancels instead of drowning the effect. ``None`` below
    :data:`MIN_SAMPLES` pairs or when every difference is identical (zero
    variance leaves nothing to test; the difference itself is still reported)."""
    diffs = [float(a) - float(b) for a, b in pairs]
    n = len(diffs)
    if n < MIN_SAMPLES:
        return None
    mean_d = sum(diffs) / n
    var = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
    if var <= 0:
        return None
    se = math.sqrt(var / n)
    t = mean_d / se
    df = n - 1
    return {
        "t": round(t, 4),
        "df": df,
        "p": round(_student_t_two_sided_p(t, df), 6),
        "mean_diff": round(mean_d, 4),
        "n_pairs": n,
    }


def wilcoxon_signed_rank(pairs: list[tuple[float, float]]) -> dict | None:
    """Wilcoxon signed-rank test, normal approximation with tie correction.

    The non-parametric partner to :func:`paired_t_test`: it asks whether the
    differences are symmetric about zero using only their ranks, so a judge whose
    scale is uneven cannot manufacture significance. Zero differences are dropped
    (the standard Pratt-free treatment) and the test needs
    :data:`MIN_WILCOXON_PAIRS` non-zero pairs to mean anything."""
    diffs = [float(a) - float(b) for a, b in pairs]
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n < MIN_WILCOXON_PAIRS:
        return None
    ranks = _rank([abs(d) for d in nonzero])
    w_plus = sum(r for d, r in zip(nonzero, ranks) if d > 0)
    mu = n * (n + 1) / 4.0
    counts: dict[float, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    tie_term = sum(c**3 - c for c in counts.values() if c > 1)
    sigma2 = n * (n + 1) * (2 * n + 1) / 24.0 - tie_term / 48.0
    if sigma2 <= 0:
        return None
    z = (w_plus - mu + (0.5 if w_plus < mu else -0.5)) / math.sqrt(sigma2)
    p = min(1.0, 2.0 * _phi(-abs(z)))
    return {
        "w": round(w_plus, 1),
        "z": round(z, 4),
        "p": round(p, 6),
        "n_pairs": n,
        "approx": True,
    }


def hedges_g(a: list[float], b: list[float]) -> float | None:
    """Standardised mean difference with the small-sample correction.

    Cohen's d overstates the effect at the sample sizes an experiment matrix
    produces (n ≈ 4–10 per config); Hedges' g applies the bias correction that
    makes the number honest at those sizes rather than merely conventional."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    pooled = ((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)
    if pooled <= 0:
        return None
    d = (ma - mb) / math.sqrt(pooled)
    dof = na + nb - 2
    j = 1.0 - 3.0 / (4.0 * dof - 1.0)
    return round(d * j, 4)


def paired_effect_size(pairs: list[tuple[float, float]]) -> float | None:
    """Cohen's d_z — the mean within-case difference in units of its own spread.

    Deliberately not comparable to :func:`hedges_g`: d_z is standardised by the
    variability of the *differences*, which is the quantity a paired design
    actually estimates. Reporting them under one name would invite exactly the
    comparison that makes no sense."""
    diffs = [float(a) - float(b) for a, b in pairs]
    n = len(diffs)
    if n < 2:
        return None
    mean_d = sum(diffs) / n
    var = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
    if var <= 0:
        return None
    return round(mean_d / math.sqrt(var), 4)


def bootstrap_diff_ci(
    pairs: list[tuple[float, float]],
    *,
    alpha: float = 0.05,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict | None:
    """Percentile bootstrap CI for the mean within-case difference.

    Resamples CASES, not observations: the case is the unit the design repeats,
    so treating individual scores as exchangeable would understate the interval
    exactly where the clustering lives. Seeded — an interval that moves between
    two runs over the same data is not evidence of anything."""
    n = len(pairs)
    if n < MIN_SAMPLES:
        return None
    diffs = [float(a) - float(b) for a, b in pairs]
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_resamples):
        means.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[max(0, int((alpha / 2) * n_resamples) - 1)]
    hi = means[min(n_resamples - 1, int((1 - alpha / 2) * n_resamples))]
    point = sum(diffs) / n
    return {
        "mean_diff": round(point, 4),
        "lo": round(lo, 4),
        "hi": round(hi, 4),
        "alpha": alpha,
        "n_resamples": n_resamples,
    }


def tost_equivalence(
    pairs: list[tuple[float, float]], margin: float, *, alpha: float = 0.05
) -> dict | None:
    """Two one-sided tests: is the paired difference INSIDE ±``margin``?

    «Not significant» is not «no difference» — it is «we could not tell», and a
    4-case matrix cannot tell much. TOST turns the absence of a finding into a
    claim that can be right or wrong: equivalent when both one-sided tests reject,
    inconclusive when they do not. The margin is the smallest difference worth
    caring about and has to be chosen BEFORE the data, like any threshold here."""
    if margin <= 0:
        return None
    diffs = [float(a) - float(b) for a, b in pairs]
    n = len(diffs)
    if n < MIN_SAMPLES:
        return None
    mean_d = sum(diffs) / n
    var = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
    if var <= 0:
        # Every case moved by exactly the same amount: equivalence is decidable
        # without a test, and pretending otherwise would be theatre.
        return {
            "margin": margin,
            "p": 0.0 if abs(mean_d) < margin else 1.0,
            "equivalent": abs(mean_d) < margin,
            "n_pairs": n,
            "exact": True,
        }
    se = math.sqrt(var / n)
    df = n - 1
    # Lower test: H0 diff <= -margin. Upper test: H0 diff >= +margin.
    t_lo = (mean_d + margin) / se
    t_hi = (mean_d - margin) / se
    p_lo = _student_t_two_sided_p(t_lo, df) / 2 if t_lo > 0 else 1 - _student_t_two_sided_p(t_lo, df) / 2
    p_hi = _student_t_two_sided_p(t_hi, df) / 2 if t_hi < 0 else 1 - _student_t_two_sided_p(t_hi, df) / 2
    p = max(p_lo, p_hi)
    return {
        "margin": margin,
        "p": round(p, 6),
        "equivalent": p < alpha,
        "n_pairs": n,
        "exact": False,
    }


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """BH-adjusted q-values, in the input order.

    An experiment matrix runs dozens of tests — 42 in Эксп 4b, 48 in 5b — and at
    α = 0.05 roughly two of them are expected to look significant with nothing
    there. FDR control rather than FWER because this table is a screen: the cost
    of one false lead among several true ones is not the cost of one false claim.
    Monotone by construction (each q is capped by the next larger one), so the
    ranking of findings survives the adjustment."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running = 1.0
    for rank, idx in reversed(list(enumerate(order, start=1))):
        running = min(running, pvalues[idx] * m / rank)
        q[idx] = round(min(1.0, running), 6)
    return q


# Two-sided 95% and 80%-power z-quantiles. Fixed constants rather than an inverse
# normal CDF: alpha and target power are policy here, not free parameters, and a
# rational approximation to Phi^-1 would be code nobody can check by eye.
ALPHA = 0.05
Z_ALPHA_TWO_SIDED = 1.959964
Z_POWER_80 = 0.841621
TARGET_POWER = 0.80


def wilson_interval(successes: int, n: int) -> dict | None:
    """Wilson 95% score interval for a proportion.

    The textbook ``p ± z·sqrt(p(1-p)/n)`` is indefensible at the counts a 2×2 over
    forty runs produces, and on a zero cell it collapses to ``[0, 0]`` — an
    interval asserting certainty from no evidence at all. Wilson is the closed
    form that stays inside [0, 1] and stays honest when a cell is empty.

    95% only: the level is fixed by :data:`Z_ALPHA_TWO_SIDED`, and taking an
    ``alpha`` we could not honour would be a parameter that lies."""
    if n <= 0 or successes < 0 or successes > n:
        return None
    z = Z_ALPHA_TWO_SIDED
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {
        "p": round(p, 4),
        "lo": round(max(0.0, centre - half), 4),
        "hi": round(min(1.0, centre + half), 4),
        "n": n,
        "alpha": ALPHA,
    }


def _percentiles(values: list[float], alpha: float) -> tuple[float, float]:
    """Lower/upper percentile of a sorted-in-place resample distribution."""
    values.sort()
    m = len(values)
    lo = values[max(0, int((alpha / 2) * m) - 1)]
    hi = values[min(m - 1, int((1 - alpha / 2) * m))]
    return lo, hi


def bootstrap_unpaired_diff_ci(
    a: list[float],
    b: list[float],
    *,
    alpha: float = 0.05,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict | None:
    """Percentile bootstrap CI for a difference of means across two groups.

    The unpaired partner to :func:`bootstrap_diff_ci`: each side is resampled
    independently, because in an unpaired slice there is no case to resample
    jointly. Seeded, like everything else here."""
    na, nb = len(a), len(b)
    if na < MIN_SAMPLES or nb < MIN_SAMPLES:
        return None
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(n_resamples):
        ma = sum(a[rng.randrange(na)] for _ in range(na)) / na
        mb = sum(b[rng.randrange(nb)] for _ in range(nb)) / nb
        diffs.append(ma - mb)
    lo, hi = _percentiles(diffs, alpha)
    return {
        "mean_diff": round(sum(a) / na - sum(b) / nb, 4),
        "lo": round(lo, 4),
        "hi": round(hi, 4),
        "alpha": alpha,
        "n_resamples": n_resamples,
    }


def bootstrap_kappa_ci(
    a: list[str],
    b: list[str],
    labels: list[str],
    *,
    alpha: float = 0.05,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict | None:
    """Percentile bootstrap CI for :func:`cohen_kappa`.

    Resamples the PAIRS, never the two rater lists separately — kappa is a
    statement about ratings of the same unit, and breaking the pairing would
    bootstrap a quantity nobody asked about. Matters because the reliability gate
    classifies axes by a point estimate: at n ≈ 20 the interval around κ = 0.58
    straddles both the 0.4 and the 0.6 cut-off, and the gate says nothing about
    it unless the interval is carried alongside."""
    n = len(a)
    if n != len(b) or n < MIN_SAMPLES:
        return None
    point = cohen_kappa(a, b, labels)
    if point is None:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_resamples):
        idx = [rng.randrange(n) for _ in range(n)]
        k = cohen_kappa([a[i] for i in idx], [b[i] for i in idx], labels)
        if k is not None:
            draws.append(k)
    if len(draws) < n_resamples // 2:
        return None
    lo, hi = _percentiles(draws, alpha)
    return {
        "kappa": point,
        "lo": round(lo, 4),
        "hi": round(hi, 4),
        "n": n,
        "alpha": alpha,
        "n_resamples": n_resamples,
    }


def bootstrap_auc_ci(
    positive: list[float],
    negative: list[float],
    *,
    alpha: float = 0.05,
    n_resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> dict | None:
    """Percentile bootstrap CI for :func:`rank_auc`.

    RQ2's headline number is an AUC over a few dozen runs; reported bare it reads
    far more precise than it is. Both sides are resampled independently — they are
    different runs, not paired observations."""
    np_, nn = len(positive), len(negative)
    if np_ < MIN_SAMPLES or nn < MIN_SAMPLES:
        return None
    point = rank_auc(positive, negative)
    if point is None:
        return None
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_resamples):
        p = [positive[rng.randrange(np_)] for _ in range(np_)]
        q = [negative[rng.randrange(nn)] for _ in range(nn)]
        auc = rank_auc(p, q)
        if auc is not None:
            draws.append(auc)
    if len(draws) < n_resamples // 2:
        return None
    lo, hi = _percentiles(draws, alpha)
    return {
        "auc": point,
        "lo": round(lo, 4),
        "hi": round(hi, 4),
        "n_positive": np_,
        "n_negative": nn,
        "alpha": alpha,
        "n_resamples": n_resamples,
    }


def _power_from_sd(
    sd: float, observed_diff: float, n: int, *, paired: bool
) -> dict | None:
    """Shared MDE / required-n arithmetic for the two power helpers.

    Two numbers, both answering «what could this design have seen»:
    ``mde`` is the smallest difference the n we HAVE could have detected, and
    ``n_required`` is the n we would have NEEDED for the difference we actually
    observed. A non-significant row without them is an unfalsifiable shrug."""
    if sd <= 0 or n < 2:
        return None
    z = Z_ALPHA_TWO_SIDED + Z_POWER_80
    # Paired: SE = sd_diff/sqrt(n). Unpaired (equal groups of n): SE = sd*sqrt(2/n).
    factor = 1.0 if paired else 2.0
    mde = z * sd * math.sqrt(factor / n)
    # Floored at 2: you cannot estimate a spread from one observation, so an
    # "n_required: 1" would be arithmetic that no design can act on.
    n_required = (
        max(2, math.ceil(factor * (z * sd / observed_diff) ** 2))
        if observed_diff
        else None
    )
    return {
        "sd": round(sd, 4),
        "observed_diff": round(observed_diff, 4),
        "mde": round(mde, 4),
        "n_required": n_required,
        "n": n,
        "alpha": ALPHA,
        "power": TARGET_POWER,
        "paired": paired,
    }


def paired_power(pairs: list[tuple[float, float]]) -> dict | None:
    """MDE and required n for a paired comparison, at 80% power and α = 0.05."""
    diffs = [float(a) - float(b) for a, b in pairs]
    n = len(diffs)
    if n < 2:
        return None
    mean_d = sum(diffs) / n
    var = sum((d - mean_d) ** 2 for d in diffs) / (n - 1)
    return _power_from_sd(math.sqrt(var), mean_d, n, paired=True)


def unpaired_power(a: list[float], b: list[float]) -> dict | None:
    """MDE and required n PER GROUP for an unpaired comparison.

    ``n`` is the harmonic-mean group size, so unbalanced groups do not report the
    power of their larger half."""
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return None
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    pooled = ((na - 1) * va + (nb - 1) * vb) / (na + nb - 2)
    if pooled <= 0:
        return None
    n_eff = 2 * na * nb / (na + nb)
    return _power_from_sd(math.sqrt(pooled), ma - mb, int(n_eff), paired=False)


def sign_test(pairs: list[tuple[float, float]]) -> dict | None:
    """Exact two-sided sign test over the within-case differences.

    The paired test that survives what the others cannot. ``paired_t_test`` needs
    variance in the differences and ``wilcoxon_signed_rank`` needs enough non-zero
    ones; a constant shift has neither, and a constant shift is the STRONGEST
    paired evidence a small matrix can produce. Falling back to an unpaired test
    there does not answer a weaker version of the question — it answers a
    different question, and on four cases shifted by exactly +1 it answers it
    backwards (Welch p = 0.73 against a paired difference of −1 on every case).

    Uses only the SIGNS of the differences, so it is also the honest primary for a
    rank-rescued axis: nothing about the magnitude of the shift enters. Exact
    binomial, no approximation — at these n an approximation would be the larger
    error. Ties are dropped, which is the standard treatment."""
    diffs = [float(a) - float(b) for a, b in pairs]
    nonzero = [d for d in diffs if d != 0]
    n = len(nonzero)
    if n < MIN_SAMPLES:
        return None
    positive = sum(1 for d in nonzero if d > 0)
    k = min(positive, n - positive)
    # Two-sided exact p: both tails of Binomial(n, 1/2) at or beyond k.
    tail = sum(math.comb(n, i) for i in range(k + 1))
    p = min(1.0, 2.0 * tail / (2**n))
    return {
        "n_positive": positive,
        "n_negative": n - positive,
        "p": round(p, 6),
        "n_pairs": n,
        "n_ties": len(diffs) - n,
        "exact": True,
    }
