"""Integration tests for app.orchestrator.engine — exercises orchestrator decision flow."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import DEFAULT_WORKSPACE_ID
from app.orchestrator import engine


@pytest.mark.asyncio
async def test_process_ready_task_no_orchestrator_model_marks_failed(db_session):
    """When workspace has no orchestrator_model_id, the engine must fail loudly."""
    task = Task(
        title="t",
        description="d",
        priority="low",
        status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # No orchestrator model configured → engine fails before checking templates.
    await engine.process_ready_task(db_session, task)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_process_ready_task_no_templates_marks_failed(db_session, default_model):
    task = Task(
        title="t",
        description="d",
        priority="low",
        status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    # Orchestrator model is configured (via default_model fixture) but no templates
    # exist → engine bails out with FAILED at the second guard.
    await engine.process_ready_task(db_session, task)

    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_process_ready_task_spawns_when_template_picked(db_session, default_model, monkeypatch):
    # Seed exactly one template so the orchestrator skips decomposition (needs >1).
    tpl = Template(
        name="solo",
        description="d",
        soul_md="# soul",
        model_id=default_model.id,
        tool_ids=[],
        max_ram="1g",
        max_cpu=100000,
        timeout_minutes=60,
        tags=[],
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(tpl)
    task = Task(
        title="t",
        description="d",
        priority="low",
        status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(tpl)
    await db_session.refresh(task)

    # Patch select_template_for_task to deterministically return our template.
    async def pick(*a, **kw):
        return {"template_id": str(tpl.id), "reasoning": "only one"}

    monkeypatch.setattr("app.orchestrator.engine.select_template_for_task", pick)

    # Patch the AgentRuntime so we don't actually run Docker.
    fake_runtime = MagicMock()
    fake_runtime.spawn.return_value = "ctr-id-1234567890ab"
    monkeypatch.setattr("app.orchestrator.engine.get_agent_runtime", lambda: fake_runtime)

    # Patch issue_agent_token (would otherwise need a real DB row).
    monkeypatch.setattr(
        "app.orchestrator.engine.issue_agent_token",
        AsyncMock(return_value="fake-token"),
    )

    await engine.process_ready_task(db_session, task)
    await db_session.refresh(task)

    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.template_id == tpl.id
    assert task.agent_container_id == "ctr-id-1234567890ab"
    fake_runtime.spawn.assert_called_once()


@pytest.mark.asyncio
async def test_process_ready_task_skips_decomposition_when_disabled(
    db_session, default_model, monkeypatch
):
    """When decomposition_enabled=False, multi-template root tasks must go
    straight to single-template selection (decide_decomposition never called)."""
    from app.models.setting import Setting

    await db_session.merge(Setting(key="decomposition_enabled", value=False))

    def _tpl(name: str) -> Template:
        return Template(
            name=name, description="d", soul_md="# soul", model_id=default_model.id,
            tool_ids=[],
            max_ram="1g", max_cpu=100000, timeout_minutes=60, tags=[],
            workspace_id=DEFAULT_WORKSPACE_ID,
        )

    tpl_a, tpl_b = _tpl("a"), _tpl("b")
    db_session.add_all([tpl_a, tpl_b])
    task = Task(
        title="multi-step research", description="d", priority="low",
        status=TaskStatus.READY.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(tpl_a)
    await db_session.refresh(task)

    decompose_mock = AsyncMock(return_value=[{"title": "x", "template_id": str(tpl_a.id)}])
    monkeypatch.setattr("app.orchestrator.engine.decide_decomposition", decompose_mock)

    async def pick(*a, **kw):
        return {"template_id": str(tpl_a.id), "reasoning": "single agent path"}

    monkeypatch.setattr("app.orchestrator.engine.select_template_for_task", pick)
    fake_runtime = MagicMock()
    fake_runtime.spawn.return_value = "ctr-disabled-deco"
    monkeypatch.setattr("app.orchestrator.engine.get_agent_runtime", lambda: fake_runtime)
    monkeypatch.setattr(
        "app.orchestrator.engine.issue_agent_token",
        AsyncMock(return_value="fake-token"),
    )

    await engine.process_ready_task(db_session, task)
    await db_session.refresh(task)

    decompose_mock.assert_not_called()
    assert task.status == TaskStatus.IN_PROGRESS.value
    assert task.template_id == tpl_a.id
    assert task.agent_container_id == "ctr-disabled-deco"
    subs = (await db_session.execute(select(Task).where(Task.parent_id == task.id))).scalars().all()
    assert subs == []


@pytest.mark.asyncio
async def test_check_parent_task_completion_marks_parent_done(db_session):
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    sub = Task(
        title="s", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(sub)
    await db_session.commit()
    await db_session.refresh(sub)

    await engine.check_parent_task_completion(db_session, sub)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE.value
    assert parent.completed_at is not None


@pytest.mark.asyncio
async def test_check_parent_task_completion_marks_parent_failed_when_any_subtask_failed(db_session):
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    sub_ok = Task(
        title="s1", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    sub_bad = Task(
        title="s2", parent_id=parent.id, priority="low",
        status=TaskStatus.FAILED.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add_all([sub_ok, sub_bad])
    await db_session.commit()
    await db_session.refresh(sub_bad)

    await engine.check_parent_task_completion(db_session, sub_bad)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED.value


@pytest.mark.asyncio
async def test_check_parent_task_completion_no_op_when_some_pending(db_session):
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    sub_done = Task(
        title="s1", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    sub_running = Task(
        title="s2", parent_id=parent.id, priority="low",
        status=TaskStatus.IN_PROGRESS.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add_all([sub_done, sub_running])
    await db_session.commit()
    await db_session.refresh(sub_done)

    original_status = parent.status
    await engine.check_parent_task_completion(db_session, sub_done)
    await db_session.refresh(parent)
    assert parent.status == original_status  # unchanged


@pytest.mark.asyncio
async def test_check_parent_task_completion_skips_when_no_parent(db_session):
    orphan = Task(
        title="o", priority="low", status=TaskStatus.DONE.value,
        workspace_id=DEFAULT_WORKSPACE_ID, parent_id=None,
    )
    db_session.add(orphan)
    await db_session.commit()
    # Should not raise.
    await engine.check_parent_task_completion(db_session, orphan)


# --- a child's infrastructure failure must reach the parent (SPA-87) ----------


async def _parent_with_children(db_session, child_failure_types):
    """A decomposition parent whose children have all settled; every child failed
    with the given type. Returns (parent, last_child)."""
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    children = [
        Task(
            title=f"s{i}", parent_id=parent.id, priority="low",
            status=TaskStatus.FAILED.value, failure_type=ft,
            workspace_id=DEFAULT_WORKSPACE_ID,
        )
        for i, ft in enumerate(child_failure_types)
    ]
    db_session.add_all(children)
    await db_session.commit()
    await db_session.refresh(children[-1])
    return parent, children[-1]


@pytest.mark.asyncio
async def test_parent_inherits_a_child_infrastructure_failure(db_session):
    """Under orchestrator:on the ExperimentRun denormalizes from the PARENT, so a
    quota-killed child used to reach the report as an ordinary weak result: the
    status rolled up and the reason did not."""
    parent, last = await _parent_with_children(db_session, ["llm_rate_limit"])
    await engine.check_parent_task_completion(db_session, last)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED.value
    assert parent.failure_type == "llm_rate_limit"


@pytest.mark.asyncio
async def test_parent_does_not_inherit_a_child_own_failure(db_session):
    """A child that failed on its own merits is a result. Inheriting it would
    delete a real failure from the aggregates."""
    parent, last = await _parent_with_children(db_session, ["cap_hit", "agent"])
    await engine.check_parent_task_completion(db_session, last)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED.value
    assert parent.failure_type is None


@pytest.mark.asyncio
async def test_parent_inherits_the_infrastructure_failure_among_mixed_children(db_session):
    parent, last = await _parent_with_children(
        db_session, ["agent", "llm_auth", "cap_hit"]
    )
    await engine.check_parent_task_completion(db_session, last)
    await db_session.refresh(parent)
    assert parent.failure_type == "llm_auth"


# --- the orchestrator degrades silently on a provider outage (SPA-87) ---------


@pytest.mark.asyncio
async def test_a_quota_death_in_template_selection_flags_the_task(db_session, monkeypatch):
    """These helpers do not fail on an LLM error — they DEGRADE: selection falls
    back to the first template and the agent then runs normally, producing a real
    result under a condition nobody chose. Only orchestrator:on cells reach this
    code, so the substituted decision IS the treatment being measured."""
    from app.orchestrator import llm as orch_llm

    task = Task(
        title="t", priority="medium", status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    class _Provider:
        async def acompletion(self, *a, **kw):
            exc = Exception("rate limit exceeded")
            exc.status_code = 429
            raise exc

    monkeypatch.setattr(orch_llm, "get_llm_provider", lambda: _Provider())

    out = await orch_llm.select_template_for_task(
        "t", "d",
        [{"id": str(uuid.uuid4()), "name": "a", "description": "x"},
         {"id": str(uuid.uuid4()), "name": "b", "description": "y"}],
        llm=SimpleNamespace(
            model=SimpleNamespace(api_name="m"),
            provider=SimpleNamespace(api_key="k", endpoint=None),
        ),
        db=db_session, task_id=task.id,
    )
    # It still returns a template — that is the degradation, not a crash…
    assert out is not None
    await db_session.refresh(task)
    # …and the run is now marked as not measuring the orchestrator.
    assert task.failure_type == "llm_rate_limit"


@pytest.mark.asyncio
async def test_a_bad_answer_from_the_orchestrator_is_not_contamination(
    db_session, monkeypatch
):
    """A provider that answers, badly, is the model under test doing its job
    poorly. Only an outage counts as infrastructure."""
    from app.orchestrator import llm as orch_llm

    task = Task(
        title="t", priority="medium", status=TaskStatus.READY.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    class _Provider:
        async def acompletion(self, *a, **kw):
            exc = Exception("context length exceeded")
            exc.status_code = 400
            raise exc

    monkeypatch.setattr(orch_llm, "get_llm_provider", lambda: _Provider())

    await orch_llm.select_template_for_task(
        "t", "d",
        [{"id": str(uuid.uuid4()), "name": "a", "description": "x"},
         {"id": str(uuid.uuid4()), "name": "b", "description": "y"}],
        llm=SimpleNamespace(
            model=SimpleNamespace(api_name="m"),
            provider=SimpleNamespace(api_key="k", endpoint=None),
        ),
        db=db_session, task_id=task.id,
    )
    await db_session.refresh(task)
    assert task.failure_type is None


@pytest.mark.asyncio
async def test_a_parent_inherits_contamination_from_a_SUCCESSFUL_child(db_session):
    """The case the first fix missed: a child that fell back to a substituted
    template after a 429 SUCCEEDS. A successful run under a condition nobody
    chose is exactly the one that must not count, so inheritance cannot be
    conditional on the parent having failed."""
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Task(
        title="s", parent_id=parent.id, priority="low",
        status=TaskStatus.DONE.value, failure_type="llm_rate_limit",
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    await engine.check_parent_task_completion(db_session, child)
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE.value
    assert parent.failure_type == "llm_rate_limit"


@pytest.mark.asyncio
async def test_the_rollup_does_not_wipe_the_parent_own_contamination(db_session):
    """The parent has its own orchestrator call, so it can already be flagged.
    An unconditional assignment in the rollup erased that."""
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        failure_type="llm_auth", workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Task(
        title="s", parent_id=parent.id, priority="low",
        status=TaskStatus.FAILED.value, failure_type="cap_hit",
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)

    await engine.check_parent_task_completion(db_session, child)
    await db_session.refresh(parent)
    assert parent.failure_type == "llm_auth"


@pytest.mark.asyncio
async def test_a_spawn_failure_rolls_the_parent_up_itself(
    db_session, default_model, monkeypatch
):
    """No agent started, so no webhook will ever arrive — and the roll-up used to
    live only on that path. The parent hung until the wall clock relabelled it
    `timeout`, which is not excluded, so an infrastructure failure came back as
    an ordinary slow model."""
    parent = Task(
        title="p", priority="medium", status=TaskStatus.IN_PROGRESS.value,
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)

    child = Task(
        title="s", parent_id=parent.id, priority="low",
        status=TaskStatus.READY.value, workspace_id=DEFAULT_WORKSPACE_ID,
    )
    tpl = Template(
        name="t", description="d", soul_md="#", tool_ids=[], tags=[],
        model_id=getattr(default_model, "id", default_model),
        workspace_id=DEFAULT_WORKSPACE_ID,
    )
    db_session.add_all([child, tpl])
    await db_session.commit()
    await db_session.refresh(child)
    await db_session.refresh(tpl)

    def _boom():
        raise RuntimeError("docker socket is gone")

    monkeypatch.setattr("app.orchestrator.engine.get_agent_runtime", _boom)
    await engine._spawn_agent_for_template(db_session, child, tpl)

    await db_session.refresh(child)
    await db_session.refresh(parent)
    assert child.status == TaskStatus.FAILED.value
    assert child.failure_type == "infra"
    # …and the parent settled immediately, carrying the reason.
    assert parent.status == TaskStatus.FAILED.value
    assert parent.failure_type == "infra"
