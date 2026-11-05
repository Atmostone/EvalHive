"""Benchmark execution path (SPA-40): webhook bypass, decomposition
inheritance, parent rollup, and board origin filtering."""

import uuid

import pytest
from app.models.task import Task, TaskStatus
from app.orchestrator.engine import _subtask_run_config, check_parent_task_completion
from httpx import AsyncClient


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
