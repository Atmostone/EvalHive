"""The orchestrator's own LLM calls, costed and attributed (SPA-111).

Judges were costed and agents were costed; template selection, the decomposition
decision and result evaluation were not — so every cost figure the platform
reported was an undercount by an unknown margin, and the experiment budget cap
only counted part of what was being spent.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.orchestrator import llm as orch
from app.utils.cost import llm_call_cost, tokens_from_response
from app.utils.failures import FAILURE_INFRA, classify_llm_error, is_contaminated


def _llm(inp="0.30", outp="1.20"):
    return SimpleNamespace(
        provider=SimpleNamespace(api_key="k", endpoint="http://x"),
        model=SimpleNamespace(
            api_name="test-model",
            input_price_per_1m_usd=Decimal(inp),
            output_price_per_1m_usd=Decimal(outp),
        ),
    )


class _Task:
    def __init__(self):
        self.id = uuid.uuid4()  # both helpers coerce task_id through UUID()
        self.orchestrator_usage = {}
        self.orchestrator_cost_usd = Decimal("0")
        self.failure_type = None
        self.condition_contaminated = False


class _DB:
    """Just enough session: `get` hands back the one task, nothing commits."""

    def __init__(self, task=None):
        self.task = task

    async def get(self, _model, _pk):
        return self.task


def _resp(prompt_tokens=1000, completion_tokens=500, *, tool_args=None, content=None):
    message = SimpleNamespace(
        tool_calls=(
            [SimpleNamespace(function=SimpleNamespace(name="select_template", arguments=tool_args))]
            if tool_args is not None
            else None
        ),
        content=content,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


# --- the primitives ---------------------------------------------------------- #


def test_tokens_read_from_both_usage_shapes():
    obj = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3))
    dct = SimpleNamespace(usage={"prompt_tokens": 7, "completion_tokens": 3})
    assert tokens_from_response(obj) == (7, 3)
    assert tokens_from_response(dct) == (7, 3)


def test_absent_usage_is_zero_not_an_exception():
    """A missing usage block is a gap in what the provider told us. Raising here
    would let a cost figure kill a run that otherwise succeeded."""
    assert tokens_from_response(SimpleNamespace(usage=None)) == (0, 0)
    assert tokens_from_response(SimpleNamespace()) == (0, 0)


def test_llm_call_cost_uses_the_model_row_prices():
    # 1M in @ 0.30 + 1M out @ 1.20
    assert llm_call_cost(_llm(), 1_000_000, 1_000_000) == pytest.approx(1.5)


# --- attribution ------------------------------------------------------------- #


async def test_usage_is_attributed_to_the_task():
    task = _Task()
    await orch._record_orchestrator_usage(
        _DB(task), task.id, _llm(), _resp(1000, 500), "template_selection"
    )
    assert task.orchestrator_usage["input_tokens"] == 1000
    assert task.orchestrator_usage["output_tokens"] == 500
    assert task.orchestrator_usage["calls"] == 1
    assert task.orchestrator_usage["by_decision"] == {"template_selection": 1}
    # 1000 @ 0.30/1M + 500 @ 1.20/1M
    assert task.orchestrator_cost_usd == Decimal("0.000900")


async def test_usage_accumulates_across_decisions_and_retries():
    """One task can pass through decomposition, selection and evaluation, and a
    re-queue runs some of them again. Assigning rather than accumulating would
    report only the last call."""
    task = _Task()
    db = _DB(task)
    for decision in ("decomposition", "template_selection", "template_selection"):
        await orch._record_orchestrator_usage(db, task.id, _llm(), _resp(100, 100), decision)
    assert task.orchestrator_usage["calls"] == 3
    assert task.orchestrator_usage["input_tokens"] == 300
    assert task.orchestrator_usage["by_decision"] == {
        "decomposition": 1,
        "template_selection": 2,
    }


async def test_a_provider_that_reports_no_usage_records_nothing():
    task = _Task()
    resp = SimpleNamespace(choices=[], usage=None)
    await orch._record_orchestrator_usage(_DB(task), task.id, _llm(), resp, "x")
    assert task.orchestrator_usage == {}
    assert task.orchestrator_cost_usd == Decimal("0")


async def test_attribution_never_raises():
    """A cost figure must not be able to fail a run — every failure mode here is
    swallowed, including a session that cannot find the task."""

    class _Broken:
        async def get(self, *_):
            raise RuntimeError("session is gone")

    await orch._record_orchestrator_usage(_Broken(), uuid.uuid4(), _llm(), _resp(), "x")
    await orch._record_orchestrator_usage(None, None, _llm(), _resp(), "x")


# --- the whole call, end to end ---------------------------------------------- #


class _Provider:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    async def acompletion(self, **_kw):
        self.calls += 1
        return self._response


async def test_template_selection_costs_its_own_call(monkeypatch):
    task = _Task()
    prov = _Provider(_resp(2000, 100, tool_args='{"template_id": "tpl-9", "reasoning": "fits"}'))
    monkeypatch.setattr(orch, "get_llm_provider", lambda: prov)
    monkeypatch.setattr(orch, "_record_reasoning", _noop)

    out = await orch.select_template_for_task(
        "T", "D",
        [{"id": "tpl-9", "name": "A", "description": ""},
         {"id": "tpl-8", "name": "B", "description": ""}],
        _llm(), db=_DB(task), task_id=task.id,
    )
    assert out["template_id"] == "tpl-9"
    assert task.orchestrator_usage["by_decision"] == {"template_selection": 1}
    assert task.orchestrator_cost_usd > 0


async def test_a_provider_that_ignores_forced_tool_choice_is_recovered_from(monkeypatch):
    """The JSON arrived in `content` instead of a tool call. Before SPA-111 this
    fell through to «pick the first template» — a substituted decision recorded
    as if it were the orchestrator's own."""
    task = _Task()
    prov = _Provider(
        _resp(500, 50, content='{"template_id": "tpl-8", "reasoning": "text mode"}')
    )
    monkeypatch.setattr(orch, "get_llm_provider", lambda: prov)
    monkeypatch.setattr(orch, "_record_reasoning", _noop)

    out = await orch.select_template_for_task(
        "T", "D",
        [{"id": "tpl-9", "name": "A", "description": ""},
         {"id": "tpl-8", "name": "B", "description": ""}],
        _llm(), db=_DB(task), task_id=task.id,
    )
    assert out["template_id"] == "tpl-8"  # not the fallback (tpl-9, the first)
    assert task.orchestrator_cost_usd > 0  # and the call was still paid for


async def test_a_provider_that_cannot_comply_contaminates_the_condition(monkeypatch):
    """Neither a tool call nor parseable content: the orchestrator falls back to
    the first template, which SUBSTITUTES the treatment being measured. That is
    infrastructure, and it must mark the run rather than pass as a decision."""
    task = _Task()
    prov = _Provider(_resp(500, 50, content="I am unable to call tools."))
    monkeypatch.setattr(orch, "get_llm_provider", lambda: prov)
    monkeypatch.setattr(orch, "_record_reasoning", _noop)

    out = await orch.select_template_for_task(
        "T", "D",
        [{"id": "tpl-9", "name": "A", "description": ""},
         {"id": "tpl-8", "name": "B", "description": ""}],
        _llm(), db=_DB(task), task_id=task.id,
    )
    assert out["template_id"] == "tpl-9"  # the fallback
    assert task.failure_type == FAILURE_INFRA
    assert task.condition_contaminated is True
    assert is_contaminated(task.failure_type)
    # the tokens the failed call burned are still attributed
    assert task.orchestrator_cost_usd > 0


async def test_decomposition_still_reads_no_tool_call_as_a_decision(monkeypatch):
    """The one orchestrator call made with tool_choice="auto". An answer with no
    tool call means «execute directly» — reading content as a fallback would turn
    a decision into a parse error."""
    task = _Task()
    prov = _Provider(_resp(300, 30, content="This task is simple enough."))
    monkeypatch.setattr(orch, "get_llm_provider", lambda: prov)
    monkeypatch.setattr(orch, "_record_reasoning", _noop)

    out = await orch.decide_decomposition(
        "T", "D", [{"name": "A", "description": ""}], _llm(), db=_DB(task), task_id=task.id
    )
    assert out is None
    assert task.failure_type is None  # NOT contamination
    assert task.orchestrator_usage["by_decision"] == {"decomposition": 1}


def test_non_compliance_classifies_as_infrastructure():
    assert classify_llm_error("ProviderComplianceError", None) == FAILURE_INFRA
    assert is_contaminated(FAILURE_INFRA)


async def _noop(*_a, **_kw):
    return None
