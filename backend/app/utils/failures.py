"""Why a run died, as a type — the single classifier (SPA-87).

A failed run used to leave behind one free-text string (``LLM call failed:
<str(e)>``) that no column stored: it survived only inside an ``agent_events``
JSONB payload. Twelve-odd writers set ``status = "failed"`` and every one of them
meant something different by it — a provider quota, a dead API key, a container
that never came up, a max-iteration cap, the model giving up. Nothing could tell
them apart in SQL, which is how a five-hour Z.ai quota outage ended up inside a
leaderboard looking like a weak model.

Two ideas hold this module together:

**Classification happens here, once.** The agent runs in its own image and cannot
import this package, so it reports *facts* — what kind of failure, which exception
class, which HTTP status — and the backend turns those facts into a type. The
agent keeps a transient-error predicate of its own, but that one only decides
whether to retry an HTTP call; it can drift from this table without changing
whether a run is marked contaminated, because the decision is not made there.

**Contaminated means «not a property of the model».** Excluding a run deletes
evidence, so the bar is unambiguity, not suspicion: a provider quota, a dead key,
a transport failure that outlived its retries, a harness container that fell over.
A max-iteration cap-hit, a wall-clock timeout, a plain provider 400 — those are
things the run did, and they stay in the numbers. Everything unclassified stays
NULL and counts, because silence is not evidence of contamination either.

Distinct from ``FAILURE_CLASSES`` in :mod:`app.quality.failure_modes`: those are
behaviours an LLM judge reads out of a finished trace (the agent looped, ignored
an error), scored per run. This is what killed the process, observed at the moment
it died.
"""

from __future__ import annotations

# --- the vocabulary ---------------------------------------------------------- #

FAILURE_LLM_RATE_LIMIT = "llm_rate_limit"  # provider quota / 429, retries spent
FAILURE_LLM_AUTH = "llm_auth"  # 401/402/403 — dead key, no credit
FAILURE_LLM_TRANSIENT = "llm_transient"  # 408/5xx/connection, retries spent
FAILURE_INFRA = "infra"  # harness: container lost, eval could not start
FAILURE_LLM_ERROR = "llm_error"  # other provider error (400, bad request…)
FAILURE_CAP_HIT = "cap_hit"  # max-iteration cap
FAILURE_TIMEOUT = "timeout"  # wall-clock task timeout
FAILURE_AGENT = "agent"  # the run failed on its own merits

FAILURE_TYPES = frozenset(
    {
        FAILURE_LLM_RATE_LIMIT,
        FAILURE_LLM_AUTH,
        FAILURE_LLM_TRANSIENT,
        FAILURE_INFRA,
        FAILURE_LLM_ERROR,
        FAILURE_CAP_HIT,
        FAILURE_TIMEOUT,
        FAILURE_AGENT,
    }
)

# Infrastructure got in the way of measuring the model. These runs are flagged and
# left out of the aggregates — see ``build_report``, which also reports how many.
CONTAMINATING_FAILURES = frozenset(
    {
        FAILURE_LLM_RATE_LIMIT,
        FAILURE_LLM_AUTH,
        FAILURE_LLM_TRANSIENT,
        FAILURE_INFRA,
    }
)

# --- HTTP / exception classification ----------------------------------------- #

# Rate limiting is called out separately from the other transients because it is
# the one that arrives in bulk: a shared quota takes down every run in flight, so
# a report that cannot name it cannot explain its own missing rows.
_RATE_LIMIT_STATUS = frozenset({429})
_AUTH_STATUS = frozenset({401, 402, 403})
# Worth retrying: timeouts and server-side failures. 429 is transient too and is
# matched first, so this set is what remains.
_TRANSIENT_STATUS = frozenset({408, 500, 502, 503, 504})

_RATE_LIMIT_EXC_NAMES = frozenset({"RateLimitError"})
_AUTH_EXC_NAMES = frozenset({"AuthenticationError", "PermissionDeniedError"})
_TRANSIENT_EXC_NAMES = frozenset(
    {
        "Timeout",
        "APITimeoutError",
        "APIConnectionError",
        "ServiceUnavailableError",
        "InternalServerError",
    }
)

# Everything an HTTP retry is worth attempting on, name-or-status. Kept as one
# derived set so the retry predicate and the classifier can never disagree about
# what «transient» means.
RETRYABLE_STATUS = _RATE_LIMIT_STATUS | _TRANSIENT_STATUS
RETRYABLE_EXC_NAMES = _RATE_LIMIT_EXC_NAMES | _TRANSIENT_EXC_NAMES


def classify_llm_error(exc_name: str | None, status_code: int | None) -> str:
    """Type of an LLM call that failed for good — after its retries were spent.

    Status wins over exception class: a provider that wraps everything in one
    ``APIError`` still tells the truth in the status line."""
    if status_code in _RATE_LIMIT_STATUS:
        return FAILURE_LLM_RATE_LIMIT
    if status_code in _AUTH_STATUS:
        return FAILURE_LLM_AUTH
    if status_code in _TRANSIENT_STATUS:
        return FAILURE_LLM_TRANSIENT
    name = (exc_name or "").strip()
    if name in _RATE_LIMIT_EXC_NAMES:
        return FAILURE_LLM_RATE_LIMIT
    if name in _AUTH_EXC_NAMES:
        return FAILURE_LLM_AUTH
    if name in _TRANSIENT_EXC_NAMES:
        return FAILURE_LLM_TRANSIENT
    return FAILURE_LLM_ERROR


def is_transient_llm_error(exc: Exception) -> bool:
    """Should this HTTP call be retried? The retry predicate for backend-side LLM
    calls, derived from the same table the classifier uses."""
    if getattr(exc, "status_code", None) in RETRYABLE_STATUS:
        return True
    return type(exc).__name__ in RETRYABLE_EXC_NAMES


# --- what the agent reports --------------------------------------------------- #

# The `kind` values an agent may send in its terminal webhook. They describe the
# SITE of the failure, which the agent knows and the backend cannot infer: a
# cap-hit is not an exception at all, and a crash in the entrypoint has no HTTP
# status to read. The mapping from site to type happens here.
_KIND_TO_TYPE = {
    "cap_hit": FAILURE_CAP_HIT,
    "tool_leak": FAILURE_AGENT,
    "crash": FAILURE_AGENT,
}


def classify_agent_failure(failure: dict | None) -> str | None:
    """Type of a terminal agent failure from the structured facts it reported.

    ``None`` when the agent sent nothing usable — an older image, a webhook that
    never arrived, a path that predates this. NULL means «not classified», which
    counts as ordinary data; it must never be read as «clean» or as
    «contaminated», and no default fills it in."""
    if not isinstance(failure, dict):
        return None
    kind = str(failure.get("kind") or "").strip()
    if kind == "llm_error":
        status = failure.get("status_code")
        try:
            status = int(status) if status is not None else None
        except (TypeError, ValueError):
            status = None
        return classify_llm_error(failure.get("exception"), status)
    return _KIND_TO_TYPE.get(kind)


def is_contaminated(failure_type: str | None) -> bool:
    """Did infrastructure, rather than the model, decide this run's outcome?"""
    return failure_type in CONTAMINATING_FAILURES
