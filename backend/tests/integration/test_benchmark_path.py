"""Benchmark execution path (SPA-40): webhook bypass, decomposition
inheritance, parent rollup, and board origin filtering."""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.auth.tokens import issue_agent_token
from app.models.task import Task, TaskStatus
from app.orchestrator.engine import _subtask_run_config, check_parent_task_completion


async def _task_with_token(auth_client, db_session, *, run_config=None, max_retries=1):
    create = await auth_client.post(
        "/api/tasks", json={"title": "bench", "description": "x", "priority": "low"}
    )
    assert create.status_code == 201
    task_id = uuid.UUID(create.json()["id"])
    task = await db_session.get(Task, task_id)
    if run_config is not None:
        task.run_config = run_config
    task.max_retries = max_retries
    plain = await issue_agent_token(
        db_session, task_id=task_id, workspace_id=task.workspace_id
    )
    await db_session.commit()
    return task, {"Authorization": f"Bearer {plain}"}


@pytest.mark.asyncio
async def test_webhook_completed_benchmark_goes_straight_done(
    auth_client: AsyncClient, db_session, monkeypatch
):
    task, headers = await _task_with_token(
        auth_client, db_session, run_config={"benchmark_mode": True}
    )

    import app.api.webhooks as webhooks_mod
    import app.quality.data_lake as data_lake_mod

    eval_mock = AsyncMock()
    monkeypatch.setattr(webhooks_mod, "evaluate_agent_result", eval_mock)
    record_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(data_lake_mod, "build_quality_record", record_mock)

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "completed",
            "task_id": str(task.id),
            "idempotency_key": "b-done-1",
            "data": {
                "result_summary": "answer",
                "files": [],
                "token_usage": {"input": 10, "output": 5},
            },
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.DONE.value
    assert task.completed_at is not None
    # No inline LLM review on the benchmark path…
    eval_mock.assert_not_called()
    # …but the quality record still gets built on the terminal DONE.
    record_mock.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_failed_benchmark_never_retries(
    auth_client: AsyncClient, db_session
):
    task, headers = await _task_with_token(
        auth_client, db_session, run_config={"benchmark_mode": True}, max_retries=2
    )

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-fail-1",
            "data": {"error": "boom", "token_usage": {"input": 1, "output": 1}},
        },
    )
    assert r.status_code == 200
    await db_session.refresh(task)
    # Retries remain (max_retries=2) but the benchmark path ignores them.
    assert task.status == TaskStatus.FAILED.value
    assert task.retry_count == 0


@pytest.mark.asyncio
async def test_webhook_completed_normal_path_unchanged(
    auth_client: AsyncClient, db_session
):
    task, headers = await _task_with_token(auth_client, db_session)

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "completed",
            "task_id": str(task.id),
            "idempotency_key": "n-done-1",
            "data": {
                "result_summary": "answer",
                "files": [],
                "token_usage": {"input": 10, "output": 5},
            },
        },
    )
    assert r.status_code == 200
    await db_session.refresh(task)
    # No orchestrator model configured in tests → evaluation skipped → approved.
    assert task.status == TaskStatus.AWAITING_APPROVAL.value


def test_subtask_run_config_keeps_run_level_keys_only():
    assert _subtask_run_config(None) is None
    assert _subtask_run_config({}) is None
    assert _subtask_run_config({"template_id": "t", "tools_override": {"disable": ["x"]}}) is None
    inherited = _subtask_run_config(
        {
            "benchmark_mode": True,
            "model_id": "m-1",
            "temperature": 0.2,
            "seed": 7,
            "memory_mode": "off",
            "template_id": "t-1",
            "tools_override": {"enable": ["a"]},
            "soul_md": "custom",
        }
    )
    assert inherited == {
        "benchmark_mode": True,
        "model_id": "m-1",
        "temperature": 0.2,
        "seed": 7,
        "memory_mode": "off",
    }


@pytest.mark.asyncio
async def test_parent_completion_rolls_up_benchmark_summary(
    auth_client: AsyncClient, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    parent = Task(
        title="root",
        status=TaskStatus.IN_PROGRESS.value,
        workspace_id=workspace_id,
        run_config={"benchmark_mode": True},
        origin="experiment",
    )
    db_session.add(parent)
    await db_session.flush()
    children = []
    for i, summary in enumerate(["found A", "wrote B"]):
        child = Task(
            title=f"step-{i}",
            parent_id=parent.id,
            status=TaskStatus.DONE.value,
            workspace_id=workspace_id,
            result_summary=summary,
            origin="experiment",
        )
        db_session.add(child)
        children.append(child)
    await db_session.commit()

    await check_parent_task_completion(db_session, children[0])
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.DONE.value
    assert "step-0" in parent.result_summary and "found A" in parent.result_summary
    assert "step-1" in parent.result_summary and "wrote B" in parent.result_summary


@pytest.mark.asyncio
async def test_tasks_list_hides_experiment_origin_by_default(
    auth_client: AsyncClient, db_session
):
    workspace_id = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    visible = await auth_client.post(
        "/api/tasks", json={"title": "user task", "priority": "low"}
    )
    assert visible.status_code == 201
    bench = Task(
        title="bench task",
        status=TaskStatus.READY.value,
        workspace_id=workspace_id,
        origin="experiment",
    )
    db_session.add(bench)
    await db_session.commit()

    r = await auth_client.get("/api/tasks")
    titles = [t["title"] for t in r.json()]
    assert "user task" in titles
    assert "bench task" not in titles

    r2 = await auth_client.get("/api/tasks", params={"include_experiments": "true"})
    body = {t["title"]: t for t in r2.json()}
    assert "bench task" in body
    assert body["bench task"]["origin"] == "experiment"
    assert body["user task"]["origin"] == "user"


# --- the failure reason survives as a type, not as a string nobody stores (SPA-87) ---


@pytest.mark.asyncio
async def test_a_quota_failure_lands_on_the_task_as_a_type(
    auth_client: AsyncClient, db_session
):
    task, headers = await _task_with_token(
        auth_client, db_session, run_config={"benchmark_mode": True}
    )

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-quota-1",
            "data": {
                "error": "LLM call failed: rate limit exceeded",
                "failure": {
                    "kind": "llm_error",
                    "exception": "RateLimitError",
                    "status_code": 429,
                    "message": "rate limit exceeded",
                },
                "token_usage": {"input": 1, "output": 1},
            },
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value
    assert task.failure_type == "llm_rate_limit"


@pytest.mark.asyncio
async def test_a_cap_hit_is_typed_but_is_not_infrastructure(
    auth_client: AsyncClient, db_session
):
    task, headers = await _task_with_token(
        auth_client, db_session, run_config={"benchmark_mode": True}
    )

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-cap-1",
            "data": {
                "error": "Agent exceeded max iterations (150)",
                "failure": {"kind": "cap_hit"},
                "token_usage": {"input": 1, "output": 1},
            },
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    from app.utils.failures import is_contaminated

    assert task.failure_type == "cap_hit"
    assert is_contaminated(task.failure_type) is False


@pytest.mark.asyncio
async def test_an_agent_that_reports_no_facts_leaves_the_type_null(
    auth_client: AsyncClient, db_session
):
    """An image built before this existed. NULL is «not classified» and counts as
    ordinary data — never quietly filled in with a guess in either direction."""
    task, headers = await _task_with_token(
        auth_client, db_session, run_config={"benchmark_mode": True}
    )

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-nofacts-1",
            "data": {"error": "boom", "token_usage": {"input": 1, "output": 1}},
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.FAILED.value
    assert task.failure_type is None


@pytest.mark.asyncio
async def test_a_retried_task_carries_no_failure_type(auth_client: AsyncClient, db_session):
    """A plain task goes back to READY instead of failing. It has not failed, so
    it must not keep a failure on its row."""
    task, headers = await _task_with_token(auth_client, db_session, max_retries=2)

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-retry-1",
            "data": {
                "error": "LLM call failed: 503",
                "failure": {"kind": "llm_error", "status_code": 503},
                "token_usage": {"input": 1, "output": 1},
            },
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.READY.value
    assert task.retry_count == 1
    assert task.failure_type is None


@pytest.mark.asyncio
async def test_a_retry_keeps_the_contamination_of_the_condition_it_reruns(
    auth_client: AsyncClient, db_session
):
    """The condition outlives the attempt. When the orchestrator's LLM died on a
    quota it fell back to a substituted template and pinned it on the task, so the
    retry skips the orchestrator and repeats the same outage-chosen setup. Wiping
    the mark let a retry that then succeeded land in the leaderboard as clean."""
    task, headers = await _task_with_token(auth_client, db_session, max_retries=2)
    task.failure_type = "llm_rate_limit"  # flagged before the agent ever ran
    await db_session.commit()

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-carry-1",
            "data": {
                "error": "Agent exceeded max iterations (20)",
                "failure": {"kind": "cap_hit"},
                "token_usage": {"input": 1, "output": 1},
            },
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.READY.value
    assert task.retry_count == 1
    # The attempt's own reason (cap_hit) is gone; the condition's is not.
    assert task.failure_type == "llm_rate_limit"


@pytest.mark.asyncio
async def test_a_retry_drops_an_ordinary_reason(auth_client: AsyncClient, db_session):
    """Only contamination is carried. A task that merely failed on its own merits
    starts the next attempt with a clean slate."""
    task, headers = await _task_with_token(auth_client, db_session, max_retries=2)
    task.failure_type = "cap_hit"
    await db_session.commit()

    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task.id}",
        headers=headers,
        json={
            "event": "failed",
            "task_id": str(task.id),
            "idempotency_key": "b-carry-2",
            "data": {
                "error": "boom",
                "failure": {"kind": "crash"},
                "token_usage": {"input": 1, "output": 1},
            },
        },
    )
    assert r.status_code == 200, r.text
    await db_session.refresh(task)
    assert task.status == TaskStatus.READY.value
    assert task.failure_type is None
