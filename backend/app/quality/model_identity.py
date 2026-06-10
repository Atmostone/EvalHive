"""Model-name identity helpers — shared by the judge (E-02) and the Bias
Mitigation Toolkit (E-18).

Self-preference bias (a judge favouring outputs from its own model) needs to know
whether the judge model and the agent/doer model are "the same". ``LLMModel`` has
no family column, so identity is a heuristic over the ``api_name`` string:
normalize (lowercase, drop the ``provider/`` prefix, strip a trailing date/build
stamp) then compare either the whole normalized name or the ``vendor-series``
family prefix.

Pure string functions with no DB/LLM dependency — kept in their own module so both
``app.quality.judge`` and ``app.quality.bias_mitigation`` can import them without an
import cycle (``bias_mitigation`` already imports from ``judge``).
"""

from __future__ import annotations


def _is_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and 2000 <= int(token) <= 2099


def normalize_model_name(name: str | None) -> str:
    """Lowercase, drop a ``provider/`` prefix, and strip a trailing date/build stamp.

    Keeps meaningful version numbers that identify the model (``gpt-4``,
    ``claude-2``) while removing release stamps (``-2024-08-06``, ``-20241022``,
    ``-2024``). Returns ``""`` for falsy input."""
    if not name:
        return ""
    n = name.strip().lower()
    if "/" in n:
        n = n.rsplit("/", 1)[-1]
    tokens = n.split("-")
    # Trailing YYYY-MM-DD (three tokens).
    if (
        len(tokens) >= 4
        and _is_year(tokens[-3])
        and tokens[-2].isdigit()
        and len(tokens[-2]) == 2
        and tokens[-1].isdigit()
        and len(tokens[-1]) == 2
    ):
        tokens = tokens[:-3]
    # Trailing compact stamp (YYYYMM / YYYYMMDD).
    elif len(tokens) >= 2 and tokens[-1].isdigit() and len(tokens[-1]) in (6, 8):
        tokens = tokens[:-1]
    # Trailing bare year.
    elif len(tokens) >= 2 and _is_year(tokens[-1]):
        tokens = tokens[:-1]
    return "-".join(tokens)


def model_family(name: str | None) -> str:
    """The ``vendor-series`` prefix of a normalized name, e.g. ``gpt-4o`` for
    ``gpt-4o-mini``, ``claude-opus`` for ``claude-opus-4-20250101``. ``""`` when
    the name is empty."""
    norm = normalize_model_name(name)
    if not norm:
        return ""
    return "-".join(norm.split("-")[:2])


def same_model_or_family(a: str | None, b: str | None) -> tuple[bool, str | None]:
    """Heuristic self-preference check between a judge model and an agent model.

    Returns ``(True, "same model")`` when the normalized names are equal,
    ``(True, "same family")`` when they share a ``vendor-series`` prefix, and
    ``(False, None)`` otherwise (or when either name is empty). Never claims
    certainty — callers phrase the warning as "may be inflated"."""
    na, nb = normalize_model_name(a), normalize_model_name(b)
    if not na or not nb:
        return (False, None)
    if na == nb:
        return (True, "same model")
    fa, fb = model_family(a), model_family(b)
    if fa and fa == fb:
        return (True, "same family")
    return (False, None)
