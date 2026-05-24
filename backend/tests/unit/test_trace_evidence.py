"""Unit tests for the TRACE Evidence Bank Judge (E-08).

The LLM calls are mocked with a fake provider that distinguishes the per-step
`assess_step` tool from the final `score_trajectory` tool, so these tests exercise
the evidence-bank accumulation (facts from earlier steps appearing in later
prompts), the step cap, groundedness/redundancy derivation, token/cost summation
over the N+1 calls, and per-step failure tolerance — without a network or a DB.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.quality import trace_evidence as te
from app.quality.trace_evidence import (
    _annotate_step,
    _fit_annotated_to_budget,
    _format_bank,
    _select_steps,
    evaluate_trajectory_with_evidence,
)
from app.quality.trace_cleaner import _count_tokens


def _llm():
    return SimpleNamespace(
        model=SimpleNamespace(
            api_name="test-model", input_price_per_1m_usd=1, output_price_per_1m_usd=2
        ),
        provider=SimpleNamespace(api_key="k", endpoint="http://e/v1"),
    )


def _resp(args, pt, ct):
    fn = MagicMock()
    fn.arguments = json.dumps(args)
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


_FINAL_ARGS = {
    "efficiency": {"score": 6, "reason": "redundant step"},
    "tool_selection": {"score": 9, "reason": "right tools"},
    "parameter_quality": {"score": 8, "reason": "ok"},
    "error_recovery": {"score": 7, "reason": "ok"},
    "goal_alignment": {"score": 9, "reason": "grounded in evidence"},
    "loop_detection": {"score": 10, "reason": "no cycles"},
    "summary": "grounded, one redundant step",
}


class _Provider:
    """Returns canned assess_step responses per call (in order) and a final
    score_trajectory response. Records every call's kwargs."""

    def __init__(self, step_specs, final_args=_FINAL_ARGS, fail_index=None):
        self.step_specs = step_specs  # list of dicts: facts/grounded/redundant/...
        self.final_args = final_args
        self.fail_index = fail_index
        self.calls = []
        self._assess_n = 0

    async def acompletion(self, **kw):
        self.calls.append(kw)
        tool_name = kw["tools"][0]["function"]["name"]
        if tool_name == "score_trajectory":
            return _resp(self.final_args, 120, 30)
        # assess_step
        i = self._assess_n
        self._assess_n += 1
        if self.fail_index is not None and i == self.fail_index:
            raise RuntimeError("step api down")
        spec = self.step_specs[i]
        args = {
            "redundant": spec.get("redundant", False),
            "grounded": spec.get("grounded", True),
            "progress": spec.get("progress", 7),
            "execution": spec.get("execution", 8),
            "new_facts": spec.get("facts", []),
            "note": spec.get("note", f"step {i}"),
        }
        return _resp(args, 10, 5)


def _trace(n=3):
    return {
        "task": {"id": "t", "title": "Build X", "description": "Desc"},
        "steps": [
            {"seq": i, "kind": "tool", "tool_name": "web_search", "content": f"out {i}", "truncated": False}
            for i in range(n)
        ],
        "stats": {"original_tokens": 100, "cleaned_tokens": 50, "steps_total": n},
    }


# --- helpers ----------------------------------------------------------------


def test_select_steps_no_cap():
    steps = _trace(4)["steps"]
    out, capped = _select_steps(steps, 30)
    assert capped is False and len(out) == 4


def test_select_steps_caps_head_and_tail():
    steps = _trace(20)["steps"]
    out, capped = _select_steps(steps, 6)
    assert capped is True and len(out) == 6
    # head (0,1,2) + tail (17,18,19), order preserved
    assert [s["seq"] for s in out] == [0, 1, 2, 17, 18, 19]


def test_format_bank_empty_and_filled():
    assert "empty" in _format_bank([])
    text = _format_bank([(0, "the sky is blue"), (1, "water is wet")])
    assert "from step 0" in text and "the sky is blue" in text


def test_annotate_step_tags_evidence():
    step = {"seq": 1, "kind": "tool", "tool_name": "web_search", "content": "x"}
    rec = {"facts": ["found Y"], "redundant": True, "grounded": False, "note": "dup"}
    line = _annotate_step(step, rec)
    assert "new evidence: found Y" in line and "redundant" in line and "ungrounded" in line


def test_fit_annotated_drops_middle_over_budget():
    blocks = [f"[{i}] tool: {'word ' * 100}" for i in range(20)]
    text, capped = _fit_annotated_to_budget("header\n", blocks, 150)
    assert capped is True and _count_tokens(text) <= 150


# --- full evidence evaluation ----------------------------------------------


async def test_bank_accumulates_and_threads_into_prompts(monkeypatch):
    specs = [
        {"facts": ["fact-A"], "grounded": True},
        {"facts": ["fact-B"], "grounded": True},
        {"facts": ["fact-C"], "grounded": False, "redundant": True},
    ]
    prov = _Provider(specs)
    monkeypatch.setattr(te, "get_llm_provider", lambda: prov)

    out = await evaluate_trajectory_with_evidence(
        _trace(3), _llm(), max_input_tokens=10_000, max_steps=30
    )

    assert out["status"] == "scored"
    # N per-step calls + 1 final scoring call
    assert out["judge_calls"] == 4
    assert len(prov.calls) == 4

    # Bank persists between steps: the step-2 prompt must contain facts from steps 0 & 1.
    assess_calls = [c for c in prov.calls if c["tools"][0]["function"]["name"] == "assess_step"]
    step2_prompt = assess_calls[2]["messages"][1]["content"]
    assert "fact-A" in step2_prompt and "fact-B" in step2_prompt
    # The first step sees an empty bank.
    assert "empty" in assess_calls[0]["messages"][1]["content"]

    # Evidence bank persisted in the profile.
    assert len(out["evidence_bank"]) == 3
    assert out["evidence_bank"][0]["facts"] == ["fact-A"]
    assert out["redundant_steps"] == 1
    assert out["groundedness"] == round(2 / 3, 2)


async def test_six_axes_and_tokens_summed(monkeypatch):
    prov = _Provider([{"facts": ["f"]}, {"facts": ["g"]}])
    monkeypatch.setattr(te, "get_llm_provider", lambda: prov)
    out = await evaluate_trajectory_with_evidence(
        _trace(2), _llm(), max_input_tokens=10_000, max_steps=30
    )
    assert {a["key"] for a in out["axes"]} == {"efficiency", "tool_selection",
        "parameter_quality", "error_recovery", "goal_alignment", "loop_detection"}
    assert out["overall_score"] == round((6 + 9 + 8 + 7 + 9 + 10) / 6, 2)
    # 2 assess calls (10 in / 5 out) + final (120 / 30)
    assert out["judge_input_tokens"] == 2 * 10 + 120
    assert out["judge_output_tokens"] == 2 * 5 + 30
    assert out["judge_cost_usd"] > 0
    assert out["trace_stats"]["steps_assessed"] == 2


async def test_per_step_failure_does_not_abort(monkeypatch):
    prov = _Provider([{"facts": ["a"]}, {"facts": ["b"]}, {"facts": ["c"]}], fail_index=1)
    monkeypatch.setattr(te, "get_llm_provider", lambda: prov)
    out = await evaluate_trajectory_with_evidence(
        _trace(3), _llm(), max_input_tokens=10_000, max_steps=30
    )
    # Final scoring still ran → status scored; the failed step is recorded.
    assert out["status"] == "scored"
    assert len(out["evidence_bank"]) == 3
    assert out["evidence_bank"][1]["error"].startswith("step api down")
    assert out["evidence_bank"][1]["progress"] == 0 and out["evidence_bank"][1]["grounded"] is False
    assert any(e.get("seq") == 1 for e in out["errors"])


async def test_max_steps_cap_reflected(monkeypatch):
    prov = _Provider([{"facts": [f"f{i}"]} for i in range(6)])
    monkeypatch.setattr(te, "get_llm_provider", lambda: prov)
    out = await evaluate_trajectory_with_evidence(
        _trace(20), _llm(), max_input_tokens=10_000, max_steps=6
    )
    assert out["trace_stats"]["steps_assessed"] == 6
    assert out["trace_stats"]["steps_total"] == 20
    assert out["judge_calls"] == 7
    assert out["input_capped"] is True  # steps were capped


async def test_final_scoring_failure_is_error(monkeypatch):
    class _FinalBoom(_Provider):
        async def acompletion(self, **kw):
            if kw["tools"][0]["function"]["name"] == "score_trajectory":
                raise RuntimeError("final down")
            return await super().acompletion(**kw)

    prov = _FinalBoom([{"facts": ["a"]}])
    monkeypatch.setattr(te, "get_llm_provider", lambda: prov)
    out = await evaluate_trajectory_with_evidence(
        _trace(1), _llm(), max_input_tokens=10_000, max_steps=30
    )
    assert out["status"] == "error"
    # bank was still built before the final call failed
    assert len(out["evidence_bank"]) == 1
    assert any("final down" in (e.get("error") or "") for e in out["errors"])
