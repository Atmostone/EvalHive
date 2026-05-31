"""Small pure-Python statistics helpers for the Judge Calibration Protocol (E-17).

E-17 validates the LLM judge against humans purely from already-stored scores, so
it needs a handful of agreement statistics and nothing else. The project carries
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


def mean_bias(judge: list[float], human: list[float]) -> float | None:
    """Mean signed gap ``judge - human``; positive means the judge scores higher.

    ``None`` for empty or mismatched inputs."""
    n = len(judge)
    if n == 0 or n != len(human):
        return None
    return round(sum(j - h for j, h in zip(judge, human)) / n, 3)
