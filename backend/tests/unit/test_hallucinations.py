"""Unit tests for Hallucination Detection (E-15).

These cover the deterministic extractors and in-trace cross-check, the LLM
fact-check parsing/budget/error handling of `_classify_with_llm` (the LLM is
mocked), the candidate→verdict mapping, and the pure aggregation logic of
`aggregate_hallucinations` (with a fake DB) — no network, no real DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.quality import hallucination as hl
from app.quality.hallucination import (
    CATEGORIES,
    _classify_with_llm,
    _corpus_from_trace,
    _extract_api_symbols,
    _extract_claims,
    _extract_numbers,
    _extract_urls,
    _llm_items_for,
    _symbol_supported,
    _url_supported,
    _verdict_map,
    aggregate_hallucinations,
)


def _llm():
    return SimpleNamespace(
        model=SimpleNamespace(
            api_name="test-model", input_price_per_1m_usd=1, output_price_per_1m_usd=2
        ),
        provider=SimpleNamespace(api_key="k", endpoint="http://e/v1"),
    )


def _args(apis=None, numbers=None, citations=None, summary="checked"):
    return {
        "apis": apis or [],
        "numbers": numbers or [],
        "citations": citations or [],
        "summary": summary,
    }


def _resp(args, pt=200, ct=50):
    fn = MagicMock()
    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _FakeProvider:
    def __init__(self, args):
        self._args = args

    async def acompletion(self, **kw):
        return _resp(self._args)


class _BoomProvider:
    async def acompletion(self, **kw):
        raise RuntimeError("api down")


class _BadJsonProvider:
    async def acompletion(self, **kw):
        fn = MagicMock()
        fn.arguments = "{not valid json"
        tc = MagicMock()
        tc.function = fn
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
        resp.usage = {}
        return resp


def _trace(steps=None):
    return {
        "task": {"id": "t", "title": "Report", "description": "Desc"},
        "steps": steps
        if steps is not None
        else [
            {"seq": 0, "kind": "tool", "tool_name": "web_fetch",
             "content": "visited https://real.org/page returning data", "truncated": False},
        ],
        "stats": {"original_tokens": 100, "cleaned_tokens": 50, "steps_total": 1},
    }


# --- deterministic extraction ------------------------------------------------


def test_extract_urls():
    txt = "See https://example.com/a, http://b.org/p) and www.foo.io/x. Dup https://example.com/a"
    assert _extract_urls(txt) == ["https://example.com/a", "http://b.org/p", "www.foo.io/x"]


def test_extract_api_symbols_only_in_code_and_dotted():
    txt = "Prose fastapi.Thing() here.\n```python\nfastapi.middleware.X()\nos.path.join(a)\nprint(y)\n```"
    out = _extract_api_symbols(txt)
    # Only dotted symbols inside the code fence; bare print() and prose excluded.
    assert "fastapi.middleware.X" in out and "os.path.join" in out
    assert "fastapi.Thing" not in out and "print" not in out


def test_extract_numbers_filters_years_and_single_digits():
    out = _extract_numbers("Grew 42% in 2024 to 1,500 units; pi is 3.14; just 5 here.")
    assert "42%" in out and "1,500" in out and "3.14" in out
    assert "2024" not in out and "5" not in out


def test_extract_claims_strips_code_and_short():
    txt = "```python\ncode.here()\n```\nThe market grew substantially over the last fiscal year. ok"
    out = _extract_claims(txt)
    assert any("market grew substantially" in c for c in out)
    assert not any("code.here" in c for c in out)


def test_corpus_and_url_supported():
    corpus = _corpus_from_trace(_trace())
    assert _url_supported("https://real.org/page", corpus) is True
    assert _url_supported("https://real.org/page/", corpus) is True  # trailing slash
    assert _url_supported("https://fake.com/missing", corpus) is False


def test_symbol_supported():
    corpus = "we used fastapi.middleware.x in the build".lower()
    assert _symbol_supported("fastapi.middleware.X", corpus) is True
    assert _symbol_supported("nonexistent.module.Foo", corpus) is False


# --- verdict mapping ---------------------------------------------------------


def test_llm_items_only_flags_unsupported():
    cands = ["42%", "99"]
    verdicts = _verdict_map(
        [
            {"value": "42%", "supported": False, "reason": "no source", "confidence": 0.8},
            {"value": "99", "supported": True, "reason": "in trace", "confidence": 0.9},
        ],
        "value",
    )
    items = _llm_items_for(cands, verdicts, key="value", with_confidence=True)
    assert len(items) == 1 and items[0]["value"] == "42%"
    assert items[0]["confidence"] == 0.8 and items[0]["kind"] == "llm"


def test_llm_items_missing_verdict_is_benefit_of_doubt():
    items = _llm_items_for(["x", "y"], {}, key="value", with_confidence=False)
    assert items == []  # no verdict → not flagged


# --- LLM fact-check ----------------------------------------------------------


async def test_classify_parses_verdicts(monkeypatch):
    args = _args(
        numbers=[{"value": "42%", "supported": False, "reason": "no src", "confidence": 0.7}],
        citations=[{"claim": "GPT-6 shipped", "supported": False, "reason": "invented", "confidence": 0.9}],
        summary="fabricated stats",
    )
    monkeypatch.setattr(hl, "get_llm_provider", lambda: _FakeProvider(args))
    out = await _classify_with_llm(
        "Grew 42%. GPT-6 shipped.", None, None, _trace(), _llm(),
        uncertain_apis=[], numbers=["42%"], claims=["GPT-6 shipped"],
        max_input_tokens=10_000,
    )
    assert out["status"] == "scored"
    assert out["judge_input_tokens"] == 200 and out["judge_cost_usd"] > 0
    assert out["args"]["numbers"][0]["value"] == "42%"
    assert out["input_capped"] is False


async def test_classify_uses_outcome_and_evidence(monkeypatch):
    monkeypatch.setattr(hl, "get_llm_provider", lambda: _FakeProvider(_args()))
    outcome = {"weighted_score": 8.0}
    evidence = {"status": "scored", "evidence_bank": [{"facts": ["fact A", "fact B"]}]}
    out = await _classify_with_llm(
        "text", outcome, evidence, _trace(), _llm(),
        uncertain_apis=[], numbers=["42%"], claims=[],
        max_input_tokens=10_000,
    )
    assert out["used_outcome_profile"] is True and out["used_trajectory_evidence"] is True


async def test_classify_llm_failure_is_error(monkeypatch):
    monkeypatch.setattr(hl, "get_llm_provider", lambda: _BoomProvider())
    out = await _classify_with_llm(
        "t", None, None, _trace(), _llm(),
        uncertain_apis=[], numbers=["42%"], claims=[], max_input_tokens=10_000,
    )
    assert out["status"] == "error" and "api down" in out["error"]


async def test_classify_bad_json_is_error(monkeypatch):
    monkeypatch.setattr(hl, "get_llm_provider", lambda: _BadJsonProvider())
    out = await _classify_with_llm(
        "t", None, None, _trace(), _llm(),
        uncertain_apis=[], numbers=["42%"], claims=[], max_input_tokens=10_000,
    )
    assert out["status"] == "error"


async def test_classify_input_capped(monkeypatch):
    monkeypatch.setattr(hl, "get_llm_provider", lambda: _FakeProvider(_args()))
    big = [
        {"seq": i, "kind": "tool", "tool_name": "t", "content": "word " * 200, "truncated": False}
        for i in range(20)
    ]
    out = await _classify_with_llm(
        "t", None, None, _trace(big), _llm(),
        uncertain_apis=[], numbers=["42%"], claims=[], max_input_tokens=200,
    )
    assert out["status"] == "scored" and out["input_capped"] is True


# --- aggregation -------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _q):
        return _FakeResult(self._rows)


def _cats(urls=(0, 0), apis=(0, 0), numbers=(0, 0), citations=(0, 0)):
    pairs = {"urls": urls, "apis": apis, "numbers": numbers, "citations": citations}
    return {
        c: {"checked": chk, "hallucinated": hal, "items": []}
        for c, (chk, hal) in pairs.items()
    }


def _rec(model, cats, status="scored"):
    count = sum(v["hallucinated"] for v in cats.values())
    return SimpleNamespace(
        model_used=model,
        template_name=None,
        template_id=None,
        hallucination_profile={
            "status": status,
            "categories": cats,
            "hallucination_count": count,
        },
    )


async def test_aggregate_by_model():
    rows = [
        _rec("m1", _cats(urls=(3, 2), numbers=(2, 1))),  # 3 hallucinations
        _rec("m1", _cats(urls=(2, 0))),                  # clean
        _rec("m2", _cats(citations=(1, 1))),
        _rec("m2", _cats(urls=(1, 1)), status="error"),  # skipped (not scored)
    ]
    out = await aggregate_hallucinations(_FakeDB(rows), workspace_id="ws")
    assert out["runs_total"] == 3 and out["hallucinated_runs"] == 2
    assert out["by_model"]["m1"]["runs_total"] == 2
    assert out["by_model"]["m1"]["by_category"]["urls"]["checked"] == 5
    assert out["by_model"]["m1"]["by_category"]["urls"]["hallucinated"] == 2
    assert out["by_model"]["m1"]["by_category"]["urls"]["rate"] == round(2 / 5, 4)
    assert set(out["by_category"].keys()) == {"urls", "numbers", "citations"}


async def test_aggregate_category_filter():
    rows = [
        _rec("m1", _cats(urls=(2, 1))),
        _rec("m1", _cats(numbers=(2, 1))),
    ]
    out = await aggregate_hallucinations(_FakeDB(rows), workspace_id="ws", category="urls")
    # Only the run with a URL hallucination is counted.
    assert out["runs_total"] == 1
    assert out["by_category"]["urls"]["by_category"]["urls"]["hallucinated"] == 1


def test_categories_constant():
    assert CATEGORIES == ("urls", "apis", "numbers", "citations")
