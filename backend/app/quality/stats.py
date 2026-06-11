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
