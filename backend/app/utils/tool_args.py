"""Reading a forced tool call back off a completion, leniently.

Every judge and orchestrator call sends ``tool_choice`` forced by name and then
reads the arguments back. Plenty of OpenAI-compatible servers treat forced tool
choice as advisory and answer in ``content`` instead, and models emit JSON with
a fence, prose around it, or a truncated tail. Reading ``tool_calls[0]`` in the
raw therefore breaks on the provider, not on the work — and, at a critical
rubric dimension, that break is scored as a QUALITY failure (SPA-51 fails
closed) when it is nothing of the sort.

E-07 grew its own tolerant reader for exactly this reason and was, until
SPA-111, the only call site that survived such a provider. This module is that
reader, lifted so every site degrades the same way, plus the one distinction
the report needs: a provider that could not comply raises
``ProviderComplianceError`` rather than a bare parse error.
"""

from __future__ import annotations

import json


class ProviderComplianceError(RuntimeError):
    """The provider ignored a forced tool call and left nothing usable behind.

    Infrastructure, not work quality: the model was never given the chance to
    answer badly. Callers surface this as a declared infrastructure error so a
    failed critical dimension is not read as a failed deliverable.
    """


def loads_lenient(raw: str | None) -> dict:
    """Parse tool-call arguments, tolerating the JSON defects models emit: a
    ```json fence, prose before/after the object, or trailing junk after a
    complete object. As a last resort, close a truncated object. Was the cause
    of ~27% of E-07 evals erroring out (``Expecting value: line 1 ...``)."""
    if not raw or not raw.strip():
        raise ValueError("empty tool-call arguments")
    s = raw.strip()
    if s.startswith("```"):  # ```json ... ``` fence
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Extract the first balanced {...} object via a string/escape-aware brace scan,
    # ignoring any pre/post prose and trailing junk.
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object in tool-call arguments")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start : i + 1])
    # Unbalanced → the object was truncated mid-stream; best-effort close it.
    return json.loads(s[start:] + "}" * depth)


def extract_tool_args(choice) -> dict:
    """Pull a forced tool call's arguments off a message, falling back to
    ``content`` when the provider ignored ``tool_choice``.

    Raises ``ProviderComplianceError`` when there is neither a tool call nor a
    parseable object in the content — the provider could not comply. A tool call
    that IS present but carries malformed arguments raises the underlying parse
    error instead: that one is the model's output, and retrying it can help.
    """
    tcs = getattr(choice, "tool_calls", None)
    raw = tcs[0].function.arguments if tcs else None
    if raw and raw.strip():
        return loads_lenient(raw)
    # An EMPTY tool call is the same failure as no tool call: the provider
    # announced the call and put the answer somewhere else. Reasoning models do
    # this routinely — MiniMax-M3 as an E-07 judge returns a named tool call with
    # blank arguments and the JSON in `content`. Falling through here is the
    # behaviour E-07's private reader always had; dropping it broke every site it
    # was lifted into, which is a thing a shared helper can do quietly.
    content = getattr(choice, "content", None)
    try:
        return loads_lenient(content)
    except (ValueError, json.JSONDecodeError) as e:
        raise ProviderComplianceError(
            "provider returned no usable tool-call arguments and no parseable "
            f"JSON in content ({type(e).__name__}: {e})"
        ) from e


def error_class(exc: BaseException) -> str:
    """``infrastructure`` when the provider could not comply, else ``evaluation``.

    The distinction an evaluator's error dict has to carry: one of these says the
    work could not be judged, the other says judging it went wrong. Only the
    first is exculpatory for the run under test.
    """
    return "infrastructure" if isinstance(exc, ProviderComplianceError) else "evaluation"
