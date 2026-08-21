"""The model's own deliberation, from chunk to judge (SPA-114).

A reasoning model returns its thinking in a field separate from the answer, and
nothing in this repository read it — so the platform stored the conclusion,
discarded the reasoning that produced it, and then asked a process judge how the
agent worked. Same shape as SPA-86 (arguments were never recorded, so
`parameter_quality` had no subject) and SPA-113 (the trace was not in the order
things happened): the recording layer was at fault and the model was charged.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.quality import trajectory as traj_mod
from app.quality.trace_cleaner import clean_trajectory
from app.quality.trajectory import _serialize_trace, fit_trace_to_budget
from app.storage.minio_client import decode_log_archive, encode_log_archive

_BASE = datetime(2026, 1, 1, 12, 0, 0)


def _task():
    return SimpleNamespace(id="t", title="T", description="d", status="done")


def _ev(event_type, data, secs=0):
    return SimpleNamespace(
        event_type=event_type, data=data, created_at=_BASE + timedelta(seconds=secs)
    )


def _chunk(content, *, tool_name="bash", secs=0, call_id=None, reasoning=None, seq=0):
    return SimpleNamespace(
        content=content,
        tool_name=tool_name,
        chunk_seq=seq,
        created_at=_BASE + timedelta(seconds=secs),
        arguments={"cmd": "ls"} if tool_name else None,
        arguments_truncated=False,
        tool_call_id=call_id,
        part_index=0,
        part_total=1,
        reasoning=reasoning,
    )


# --- the trace ---------------------------------------------------------------- #


def test_reasoning_becomes_its_own_step_ahead_of_the_call_it_preceded():
    out = clean_trajectory(
        _task(),
        [_ev("agent_spawned", {}, secs=0), _ev("agent_completed", {"result_summary": "ok"}, secs=30)],
        [_chunk("files listed", secs=10, call_id="c0", reasoning="I should look at the directory first.")],
    )
    kinds = [s["kind"] for s in out["steps"]]
    assert "model_reasoning" in kinds
    assert kinds.index("model_reasoning") < kinds.index("tool")
    thought = next(s for s in out["steps"] if s["kind"] == "model_reasoning")
    assert thought["content"] == "I should look at the directory first."
    # and it did NOT get mixed into the answer
    tool_step = next(s for s in out["steps"] if s["kind"] == "tool")
    assert tool_step["content"] == "files listed"


def test_a_final_thought_with_no_tool_call_is_kept_without_inventing_a_step():
    """The last turn has nothing to ride on, so the agent sends a carrier chunk:
    no tool, no output, no call id. That carrier must surface as reasoning and
    NOT as a nameless empty tool call, which the loop counter would score as an
    action the agent never took."""
    out = clean_trajectory(
        _task(),
        [_ev("agent_spawned", {}, secs=0), _ev("agent_completed", {"result_summary": "ok"}, secs=30)],
        [
            _chunk("done", secs=10, call_id="c0", reasoning="Read the file."),
            _chunk("", tool_name=None, secs=20, reasoning="The task is complete; I will stop."),
        ],
    )
    thoughts = [s["content"] for s in out["steps"] if s["kind"] == "model_reasoning"]
    assert thoughts == ["Read the file.", "The task is complete; I will stop."]
    assert all(s["tool_name"] for s in out["steps"] if s["kind"] == "tool")


def test_reasoning_survives_archiving():
    """SPA-113's lesson, applied before it bites: a field the live trace has and
    the archived one silently does not is a trace that changes after compaction."""
    chunks = [_chunk("out", secs=5, call_id="c0", reasoning="think think")]
    decoded = decode_log_archive(encode_log_archive(chunks).decode("utf-8"))
    assert decoded[0]["reasoning"] == "think think"


def test_a_pre_spa_114_archive_decodes_to_no_reasoning_not_to_empty():
    """Absent, because the field was never requested from any provider — as
    opposed to present-and-empty, which would read as «it thought nothing»."""
    blob = '{"content": "out", "tool_name": "bash", "part_index": 0, "part_total": 1}'
    assert decode_log_archive(blob)[0]["reasoning"] is None


# --- the trim ----------------------------------------------------------------- #


def _trace_with(reasoning_tokens: int, output_tokens: int) -> dict:
    return {
        "task": {"id": "t", "title": "T", "description": "D"},
        "steps": [
            {"seq": 0, "kind": "model_reasoning", "tool_name": None, "arguments": None,
             "content": "think " * reasoning_tokens},
            {"seq": 1, "kind": "tool", "tool_name": "bash", "arguments": {"cmd": "ls"},
             "content": "output " * output_tokens},
        ],
        "stats": {},
    }


def test_deliberation_is_sacrificed_before_tool_output():
    """The order SPA-86 chose put reasoning AFTER outputs — decided when
    «reasoning» meant the orchestrator's one-line rationale. A model's full
    thinking is verbose by construction, and a tool call plus its result is
    denser evidence per token about how the agent worked, so it goes first."""
    trace = _trace_with(reasoning_tokens=800, output_tokens=800)
    full = _serialize_trace(trace)
    text, report = fit_trace_to_budget(trace, max_input_tokens=900)

    assert report["capped"] is True
    assert report["model_reasoning_cap_applied"] is not None
    assert report["model_reasoning_shrunk"] >= 1
    # the deliberation gave way first — the tool output was never touched
    assert report["outputs_shrunk"] == 0
    assert report["output_cap_applied"] is None
    assert len(text) < len(full)
    assert "output output" in text  # the evidence that answers the question survives


def test_a_trace_that_fits_loses_nothing():
    trace = _trace_with(reasoning_tokens=5, output_tokens=5)
    text, report = fit_trace_to_budget(trace, max_input_tokens=100_000)
    assert report["capped"] is False
    assert report["model_reasoning_shrunk"] == 0
    assert "think think" in text and "output output" in text


# --- what the judge is shown --------------------------------------------------- #


class _CapturingProvider:
    """Records the prompt it was handed, so the test can read what the judge saw."""

    def __init__(self):
        self.seen = ""

    async def acompletion(self, **kw):
        self.seen = "\n".join(m["content"] for m in kw["messages"])
        args = {
            ax: {"score": 7, "reason": "fine"}
            for ax in (
                "efficiency", "tool_selection", "parameter_quality",
                "error_recovery", "goal_alignment", "loop_detection",
            )
        }
        args["summary"] = "ok"
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(arguments=json.dumps(args))
                            )
                        ],
                        content=None,
                    )
                )
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


async def _judge_with(monkeypatch, db_session, *, show_reasoning):
    trace = {
        "task": {"id": "t", "title": "T", "description": "D"},
        "steps": [
            {"seq": 0, "kind": "model_reasoning", "tool_name": None, "arguments": None,
             "content": "SECRET-DELIBERATION"},
            {"seq": 1, "kind": "tool", "tool_name": "bash", "arguments": {"cmd": "ls"},
             "content": "a.txt"},
        ],
        "stats": {},
        "config": {},
    }

    async def _fake_trace(*_a, **_kw):
        return trace

    llm = SimpleNamespace(
        model=SimpleNamespace(api_name="m", input_price_per_1m_usd=1, output_price_per_1m_usd=2),
        provider=SimpleNamespace(api_key="k", endpoint="http://x"),
    )

    async def _fake_resolve(*_a, **_kw):
        return llm

    prov = _CapturingProvider()
    monkeypatch.setattr(traj_mod, "build_cleaned_trace", _fake_trace)
    monkeypatch.setattr(traj_mod, "_resolve_judge_model", _fake_resolve)
    monkeypatch.setattr(traj_mod, "get_llm_provider", lambda: prov)

    task = Task(title="x", status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
                result_summary="done", model_used="m")
    db_session.add(task)
    await db_session.flush()
    profile = await traj_mod.evaluate_task_trajectory(
        db_session, task, commit=False, max_input_tokens=0, show_reasoning=show_reasoning
    )
    return profile, prov


async def test_the_judge_sees_the_deliberation_by_default(monkeypatch, db_session, default_model):
    profile, prov = await _judge_with(monkeypatch, db_session, show_reasoning=True)
    assert "SECRET-DELIBERATION" in prov.seen
    assert profile["reasoning_shown"] is True
    assert profile["n_reasoning_steps"] == 1


async def test_withholding_it_is_recorded_as_a_condition_not_hidden(
    monkeypatch, db_session, default_model
):
    """A score computed with reasoning visible is not comparable to one computed
    without, so the policy travels with the verdict — the same shape as
    `files_only` on the outcome judge. And `n_reasoning_steps` counts what the RUN
    had, so «withheld» stays distinct from «the model never reasoned»."""
    profile, prov = await _judge_with(monkeypatch, db_session, show_reasoning=False)
    assert "SECRET-DELIBERATION" not in prov.seen
    assert "a.txt" in prov.seen  # the rest of the trace is untouched
    assert profile["reasoning_shown"] is False
    assert profile["n_reasoning_steps"] == 1


# --- the wire ------------------------------------------------------------------ #


def test_the_webhook_schema_carries_the_reasoning_split():
    """`extra: "ignore"` drops any undeclared key silently, with no error
    anywhere — which is how the first live run reported 103 reasoning tokens in
    its webhook and stored a task that had none."""
    from app.schemas.webhooks import TokenUsage

    tu = TokenUsage.model_validate(
        {"input_tokens": 2530, "output_tokens": 266, "reasoning_tokens": 103}
    )
    assert tu.reasoning_tokens == 103
    assert tu.model_dump()["reasoning_tokens"] == 103


def test_a_non_reasoning_agent_reports_absence_not_zero():
    from app.schemas.webhooks import TokenUsage

    tu = TokenUsage.model_validate({"input_tokens": 10, "output_tokens": 5})
    assert tu.reasoning_tokens is None


async def test_a_compacted_run_still_shows_its_deliberation(db_session, monkeypatch):
    """Encoder, decoder and the archive→chunk reconstruction all have to carry the
    field. Patching two of the three is invisible until a run is compacted — which
    is precisely the asymmetry SPA-113 fixed for the clock, and this caught on the
    first live run after the encoder and decoder alone were done."""
    from app.quality import trace_cleaner as tc

    chunks = [_chunk("listing", secs=5, call_id="c0", reasoning="I will look first.")]
    blob = encode_log_archive(chunks).decode("utf-8")

    task = Task(
        title="t", status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
        log_archive_s3_path="logs/fake.log",
    )
    db_session.add(task)
    await db_session.flush()
    monkeypatch.setattr(tc, "_parse_dt", lambda v: None)
    monkeypatch.setattr(
        "app.storage.minio_client.read_log_archive", lambda _p: blob.encode("utf-8")
    )

    restored = await tc._load_log_chunks(db_session, task)
    assert restored[0].reasoning == "I will look first."


# --- the vendor shapes, against the REAL agent module ---------------------------- #
#
# Nothing in agent-image/ had ever been under test, and the one assumption that
# mattered — which field each vendor's reasoning arrives in — was wrong. The
# first cut checked `message.reasoning` and a typed `content` list; the `Message`
# model actually installed (litellm 1.96.2) declares `reasoning_content`,
# `thinking_blocks` and `reasoning_items` and NO plain `reasoning`, so Anthropic
# and OpenAI fell through silently — indistinguishable from a model that did not
# reason at all. These load the real file rather than a copy, because a copy kept
# "in lockstep" is the same class of unverified assumption.

import importlib.util  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402


_AGENT_SRC = "/agent-image/agent.py"
_agent_available = pytest.mark.skipif(
    not os.path.exists(_AGENT_SRC),
    reason="agent-image is mounted read-only into the api container for these tests",
)


def _agent_module():
    if "agent_under_test" in sys.modules:
        return sys.modules["agent_under_test"]
    spec = importlib.util.spec_from_file_location("agent_under_test", _AGENT_SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _msg(**kw):
    kw.setdefault("content", "the answer")
    kw.setdefault("provider_specific_fields", None)
    return SimpleNamespace(**kw)


def test_the_installed_client_declares_the_fields_the_extractor_reads():
    """A guard against the exact mistake this replaced: the extractor's field
    list is checked against the client's model, not against documentation."""
    from litellm.types.utils import Message

    declared = set(Message.model_fields)
    assert {"reasoning_content", "thinking_blocks", "reasoning_items"} <= declared


@_agent_available
def test_reasoning_content_shape():
    agent = _agent_module()
    r, override = agent._extract_reasoning(_msg(reasoning_content="  I should look first.  "))
    assert r == "I should look first." and override is None


@_agent_available
def test_anthropic_thinking_blocks_shape():
    agent = _agent_module()
    r, _ = agent._extract_reasoning(
        _msg(thinking_blocks=[
            {"type": "thinking", "thinking": "First, read the file.", "signature": "sig"},
            {"type": "thinking", "thinking": "Then write the answer."},
        ])
    )
    assert r == "First, read the file.\nThen write the answer."


@_agent_available
def test_a_redacted_thinking_block_is_noted_not_dropped():
    """Encrypted deliberation is deliberation that happened. Dropping it silently
    would read as «the model thought nothing», which is the error this whole
    change exists to stop."""
    agent = _agent_module()
    r, _ = agent._extract_reasoning(
        _msg(thinking_blocks=[{"type": "redacted_thinking", "data": "AAAA"}])
    )
    assert r and "redacted" in r.lower()


@_agent_available
def test_openai_reasoning_items_shape():
    agent = _agent_module()
    r, _ = agent._extract_reasoning(
        _msg(reasoning_items=[
            {"type": "reasoning", "id": "rs_1",
             "summary": [{"type": "summary_text", "text": "Plan: list, then write."}]},
        ])
    )
    assert r == "Plan: list, then write."


@_agent_available
def test_inline_think_tags_are_lifted_out_of_the_answer():
    agent = _agent_module()
    r, override = agent._extract_reasoning(
        _msg(content="<think>Let me work this out.</think>\n\n42")
    )
    assert r == "Let me work this out."
    assert override == "42"  # the answer no longer carries the blob


@_agent_available
def test_a_model_that_does_not_reason_yields_nothing():
    agent = _agent_module()
    assert agent._extract_reasoning(_msg()) == (None, None)


# --- absence vs zero ------------------------------------------------------------- #


@_agent_available
def test_no_usage_detail_reports_absence_not_a_measured_zero():
    """«The provider said nothing about reasoning» and «the model reasoned for
    zero tokens» are different facts. Collapsing them makes the report claim a
    measured 0% on every run by a non-reasoning model."""
    agent = _agent_module()
    assert agent._reasoning_tokens(SimpleNamespace(completion_tokens_details=None)) is None
    assert agent._reasoning_tokens(SimpleNamespace()) is None
    assert agent._reasoning_tokens({"completion_tokens_details": {}}) is None


@_agent_available
def test_a_reported_zero_is_kept_as_a_measurement():
    agent = _agent_module()
    usage = SimpleNamespace(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0)
    )
    assert agent._reasoning_tokens(usage) == 0


@_agent_available
def test_a_reported_split_is_read_from_either_shape():
    agent = _agent_module()
    obj = SimpleNamespace(completion_tokens_details=SimpleNamespace(reasoning_tokens=44))
    dct = {"completion_tokens_details": {"reasoning_tokens": 44}}
    assert agent._reasoning_tokens(obj) == 44
    assert agent._reasoning_tokens(dct) == 44
