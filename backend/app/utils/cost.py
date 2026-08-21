"""Cost calculation from token usage and per-task denormalized prices."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from app.models.task import Task

logger = logging.getLogger(__name__)

_warned_tasks: set[str] = set()


def calculate_cost(task: Task, token_usage: Optional[dict] = None) -> Decimal:
    """Compute USD cost from the task's denormalized per-1M token prices.

    Prices are captured on the Task row at agent spawn time so the cost is
    stable even if the underlying LLMModel row is later edited or deleted.
    """
    if task.input_price_per_1m_usd is None and task.output_price_per_1m_usd is None:
        key = str(task.id)
        if key not in _warned_tasks:
            _warned_tasks.add(key)
            logger.warning(f"No price denorm for task {task.id}; cost=0")
        return Decimal("0")

    in_rate = Decimal(task.input_price_per_1m_usd or 0)
    out_rate = Decimal(task.output_price_per_1m_usd or 0)
    tu = token_usage if token_usage is not None else (task.token_usage or {})
    inp = int(tu.get("input_tokens") or tu.get("input") or 0)
    out = int(tu.get("output_tokens") or tu.get("output") or 0)
    cost = (Decimal(inp) / Decimal(1_000_000)) * in_rate + (
        Decimal(out) / Decimal(1_000_000)
    ) * out_rate
    return cost.quantize(Decimal("0.000001"))


def tokens_from_response(resp) -> tuple[int, int]:
    """(prompt, completion) token counts off a completion, whatever shape it has.

    Providers hand `usage` back as either an object or a plain dict, and some
    omit it entirely — an absent usage block is 0/0, never an exception: a cost
    figure must not be able to fail a run.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return 0, 0
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0), int(
        getattr(usage, "completion_tokens", 0) or 0
    )


def llm_call_cost(llm, input_tokens: int, output_tokens: int) -> float:
    """USD for one platform-side LLM call, at the resolved model's live prices.

    Unlike `calculate_cost`, which bills an agent run against the prices frozen
    on its Task row, the platform's own calls (judges, orchestrator) are priced
    from the model row: they are not the measured subject, they are overhead.
    """
    in_rate = Decimal(llm.model.input_price_per_1m_usd or 0)
    out_rate = Decimal(llm.model.output_price_per_1m_usd or 0)
    cost = (Decimal(input_tokens) / Decimal(1_000_000)) * in_rate + (
        Decimal(output_tokens) / Decimal(1_000_000)
    ) * out_rate
    return float(cost.quantize(Decimal("0.000001")))
