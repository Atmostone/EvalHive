"""Reading a forced tool call back off a completion (SPA-111).

The lenient reader was E-07's private robustness fix; SPA-111 made it the
shared path for all twelve forced-tool-choice call sites, so its behaviour —
including the one case that must NOT be swallowed — is pinned here.
"""

import json
from types import SimpleNamespace

import pytest

from app.utils.tool_args import (
    ProviderComplianceError,
    extract_tool_args,
    loads_lenient,
)


def test_loads_lenient_plain_and_fenced():
    obj = {"efficiency": {"score": 7}, "summary": "ok"}
    assert loads_lenient(json.dumps(obj)) == obj
    assert loads_lenient("```json\n" + json.dumps(obj) + "\n```") == obj
    assert loads_lenient("```\n" + json.dumps(obj) + "\n```") == obj


def test_loads_lenient_strips_prose_and_trailing_junk():
    obj = {"a": 1, "b": "x"}
    assert loads_lenient("Here is the score: " + json.dumps(obj)) == obj
    assert loads_lenient(json.dumps(obj) + "\n\nThat is my assessment.") == obj


def test_loads_lenient_closes_cleanly_truncated_object():
    # response cut off after a complete value — the missing braces are added back
    out = loads_lenient('{"efficiency": {"score": 6}, "summary": "done"')
    assert out["efficiency"]["score"] == 6 and out["summary"] == "done"


def test_loads_lenient_raises_on_unrecoverable():
    for bad in ("", "   ", "no json object here"):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            loads_lenient(bad)


def test_extract_tool_args_falls_back_to_content():
    # provider ignored the forced tool call → JSON arrived in message.content
    obj = {"efficiency": {"score": 8}}
    choice = SimpleNamespace(tool_calls=None, content=json.dumps(obj))
    assert extract_tool_args(choice) == obj


def test_extract_tool_args_flags_a_non_compliant_provider():
    """No tool call and nothing parseable in content is the provider failing to
    comply — a distinct error, so a critical dimension that dies this way is not
    reported as bad work."""
    choice = SimpleNamespace(tool_calls=None, content="I cannot use tools.")
    with pytest.raises(ProviderComplianceError):
        extract_tool_args(choice)


def test_an_empty_tool_call_falls_through_to_content():
    """A named tool call with blank arguments is the same failure as no tool call
    at all — the provider announced the call and put the answer somewhere else.
    Reasoning models do it routinely: MiniMax-M3 as an E-07 judge returns exactly
    this. E-07's private reader always fell through here; the shared one has to
    as well, or lifting it breaks every site it was lifted into."""
    obj = {"efficiency": {"score": 6}}
    for empty in ("", "   ", None):
        choice = SimpleNamespace(
            tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments=empty))],
            content=json.dumps(obj),
        )
        assert extract_tool_args(choice) == obj


def test_extract_tool_args_keeps_a_present_but_malformed_call_as_a_parse_error():
    """A tool call that IS there but carries junk is the MODEL's output, not the
    provider's compliance — it stays an ordinary parse error, which callers may
    retry. Conflating the two would let a bad-output retry masquerade as an
    infrastructure fault (and vice versa)."""
    choice = SimpleNamespace(
        tool_calls=[SimpleNamespace(function=SimpleNamespace(arguments="not json"))],
        content=None,
    )
    with pytest.raises((ValueError, json.JSONDecodeError)) as exc:
        extract_tool_args(choice)
    assert not isinstance(exc.value, ProviderComplianceError)
