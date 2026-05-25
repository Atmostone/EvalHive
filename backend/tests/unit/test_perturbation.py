"""Unit tests for the Adversarial / Perturbation Judge transforms + math (E-12).

Pure-Python pieces — no DB, no real LLM: deterministic noise/reorder, the
injection payload + canary detection, the paraphrase no-LLM fallback, and the
robustness formula.
"""

from random import Random
from types import SimpleNamespace

import pytest

from app.quality import perturbation as pert
from app.quality.perturbation import (
    _apply_transform,
    _injection_payload,
    _robustness,
    _t_noise,
    _t_reorder,
    injection_followed,
)


def test_noise_is_deterministic_and_changes_text():
    title, desc = "Title", "Please build a detailed quarterly report with charts and tables."
    a = _t_noise(title, desc, rng=Random("seed:1"))
    b = _t_noise(title, desc, rng=Random("seed:1"))
    assert a == b  # same seed -> identical
    assert a[1] != desc  # something was perturbed
    assert a[0] == title  # title untouched when description present


def test_noise_falls_back_to_title_when_no_description():
    out_title, out_desc, extra = _t_noise("Make a long enough title here", "", rng=Random("x"))
    assert out_desc == ""
    assert extra == {}


def test_reorder_shuffles_multi_sentence_and_noops_single():
    desc = "First do A. Then do B. Finally do C."
    title, reordered, _ = _t_reorder("t", desc, rng=Random("r"))
    # every sentence is preserved, only the order may change
    for frag in ("First do A.", "Then do B.", "Finally do C."):
        assert frag in reordered
    # single sentence -> unchanged
    t2, d2, _ = _t_reorder("t", "Just one thing", rng=Random("r"))
    assert d2 == "Just one thing"


def test_injection_payload_carries_canary_and_detection():
    payload = _injection_payload("abc123")
    assert "abc123" in payload
    assert "Ignore all previous instructions" in payload

    followed = SimpleNamespace(result_summary="...done. abc123", result_files=[])
    ignored = SimpleNamespace(result_summary="real work, no marker", result_files=[])
    via_file = SimpleNamespace(result_summary="ok", result_files=["INJECTED_abc123.txt"])
    assert injection_followed(followed, "abc123") is True
    assert injection_followed(via_file, "abc123") is True
    assert injection_followed(ignored, "abc123") is False
    assert injection_followed(followed, None) is False  # no canary -> never followed


@pytest.mark.asyncio
async def test_apply_transform_inject_returns_payload():
    title, desc, extra = await _apply_transform(
        "inject", "t", "d", rng=Random("x"), llm=None, canary="tok99"
    )
    assert title == "t" and desc == "d"  # input untouched for injection
    assert "tool_injection" in extra and "tok99" in extra["tool_injection"]


@pytest.mark.asyncio
async def test_paraphrase_falls_back_to_noise_without_llm():
    title, desc, extra = await _apply_transform(
        "paraphrase", "t", "rewrite this please now", rng=Random("s"), llm=None, canary=None
    )
    assert extra == {}
    assert desc != "rewrite this please now"  # noise fallback perturbed it


@pytest.mark.asyncio
async def test_paraphrase_uses_llm_when_available(monkeypatch):
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="A reworded request"))]
    )

    class _Provider:
        async def acompletion(self, **kwargs):
            return fake_resp

    monkeypatch.setattr(pert, "get_llm_provider", lambda: _Provider())
    llm = SimpleNamespace(
        model=SimpleNamespace(api_name="m"),
        provider=SimpleNamespace(api_key="k", endpoint="http://x"),
    )
    title, desc, extra = await _apply_transform(
        "paraphrase", "t", "original wording", rng=Random("s"), llm=llm, canary=None
    )
    assert desc == "A reworded request"
    assert extra == {}


def test_robustness_formula():
    # no degradation -> 1.0
    assert _robustness(8.0, 8.0) == (1.0, 0.0)
    # 50% drop -> robustness 0.5
    r, delta = _robustness(8.0, 4.0)
    assert r == 0.5 and delta == -4.0
    # improvement clamps degradation to 0 -> robustness 1.0
    r, delta = _robustness(5.0, 7.0)
    assert r == 1.0 and delta == 2.0
    # missing data -> None
    assert _robustness(None, 5.0) == (None, None)
    assert _robustness(8.0, None) == (None, None)
    assert _robustness(0.0, 5.0) == (None, None)
