"""LLM provider abstraction. Default impl wraps litellm.

The default provider adds two cross-cutting guards around every backend
``acompletion`` call (SPA-47):

- **Transient retry** — 429/5xx/connection errors are retried with exponential
  backoff + jitter (``LLM_TRANSIENT_RETRIES`` extra attempts, default 3;
  ``LLM_RETRY_BASE_SECONDS`` initial delay, default 1.5). Distinct from the
  task-level quality retries: this is one HTTP call healing itself, invisible
  to N-run semantics.
- **Per-provider concurrency** — an ``asyncio.Semaphore`` registry keyed by
  ``(api_base, api_key)``. Limits come from ``providers.max_concurrency``
  (NULL → unbounded) and are pushed into the registry wherever a Provider row
  is resolved for use. Subscription providers (e.g. Z.ai coding plan) limit
  *concurrent* requests, not tokens — without this, a judge fanning out 6
  dimensions via ``asyncio.gather`` trips their limiter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying: rate limit, timeouts, server-side failures.
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
# litellm exception class names that are transient but may carry no status.
_TRANSIENT_EXC_NAMES = {
    "RateLimitError",
    "Timeout",
    "APIConnectionError",
    "ServiceUnavailableError",
    "InternalServerError",
}


def _is_transient_llm_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in _TRANSIENT_STATUS:
        return True
    return type(exc).__name__ in _TRANSIENT_EXC_NAMES


def _retry_config() -> tuple[int, float]:
    """(extra attempts, base delay seconds) — env-tunable, safe fallbacks."""
    try:
        retries = max(0, int(os.environ.get("LLM_TRANSIENT_RETRIES", "3")))
    except ValueError:
        retries = 3
    try:
        base = max(0.0, float(os.environ.get("LLM_RETRY_BASE_SECONDS", "1.5")))
    except ValueError:
        base = 1.5
    return retries, base


# ---------------------- per-provider concurrency registry ----------------------

# key -> (limit, semaphore, event loop it was created on). Semaphores are
# loop-bound, so a registration surviving into a different loop (tests, CLI)
# is recreated transparently.
_provider_limits: dict[str, int] = {}
_semaphores: dict[str, tuple[int, asyncio.Semaphore, Any]] = {}


def _concurrency_key(api_base: str | None, api_key: str | None) -> str:
    # Same endpoint under different keys = different subscription = own limit.
    # Only the key's tail goes into the registry key (no full secrets in keys).
    return f"{api_base or ''}|{(api_key or '')[-8:]}"


def set_provider_concurrency(
    api_base: str | None, api_key: str | None, limit: int | None
) -> None:
    """Register (or clear with ``None``/0) a provider's concurrent-call limit."""
    key = _concurrency_key(api_base, api_key)
    if not limit or limit <= 0:
        _provider_limits.pop(key, None)
        _semaphores.pop(key, None)
        return
    _provider_limits[key] = int(limit)


def _get_semaphore(api_base: str | None, api_key: str | None) -> asyncio.Semaphore | None:
    key = _concurrency_key(api_base, api_key)
    limit = _provider_limits.get(key)
    if limit is None:
        return None
    loop = asyncio.get_running_loop()
    entry = _semaphores.get(key)
    if entry is None or entry[0] != limit or entry[2] is not loop:
        entry = (limit, asyncio.Semaphore(limit), loop)
        _semaphores[key] = entry
    return entry[1]


def reset_provider_concurrency() -> None:
    """Clear the registry. Used by tests."""
    _provider_limits.clear()
    _semaphores.clear()


class LLMProvider(ABC):
    @abstractmethod
    async def acompletion(
        self,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any: ...


class LiteLLMProvider(LLMProvider):
    async def acompletion(
        self,
        model: str,
        messages: list[dict],
        tools: list | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        import litellm

        prefixed = model if "/" in model else f"openai/{model}"

        async def _call():
            return await litellm.acompletion(
                model=prefixed,
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                stream=stream,
                **kwargs,
            )

        sem = _get_semaphore(kwargs.get("api_base"), kwargs.get("api_key"))
        retries, base = _retry_config()
        # The semaphore is held across backoff sleeps on purpose: a 429 from
        # this provider means it wants LESS in-flight work, not a freed slot.
        # (For stream=True only call setup is guarded — the slot is released
        # once the stream object is returned.)
        if sem is None:
            return await self._call_with_retry(_call, prefixed, retries, base)
        async with sem:
            return await self._call_with_retry(_call, prefixed, retries, base)

    @staticmethod
    async def _call_with_retry(call, model: str, retries: int, base: float) -> Any:
        for attempt in range(retries + 1):
            try:
                return await call()
            except Exception as e:  # noqa: BLE001 — classified right below
                if attempt >= retries or not _is_transient_llm_error(e):
                    raise
                delay = base * (2**attempt) * (1 + random.random() * 0.25)
                logger.warning(
                    f"transient LLM error from {model} "
                    f"(attempt {attempt + 1}/{retries + 1}): {e} — retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover


_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _provider
    if _provider is not None:
        return _provider
    name = os.environ.get("LLM_PROVIDER", "litellm")
    if name == "litellm":
        _provider = LiteLLMProvider()
    else:
        raise ValueError(f"unknown LLM_PROVIDER={name}")
    return _provider


def set_llm_provider(provider: LLMProvider | None) -> None:
    """Override (or reset with None). Used by tests."""
    global _provider
    _provider = provider
