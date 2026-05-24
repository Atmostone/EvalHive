"""Integration tests for the Trajectory Matching endpoints (E-09).

POST /api/quality/records/{task_id}/evaluate-trajectory-match compares a task's
real tool-trace (cleaned by E-06) against its canonical_trajectory and writes the
deterministic profile to quality_records.trajectory_match_profile; GET
.../trajectory-match reads it back. Skipped when the task has no canonical
trajectory; workspace-scoped. No LLM involved.
"""

import uuid
from datetime import datetime

from httpx import AsyncClient

from app import database
from app.models.agent_log import AgentLogChunk
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID


async def _seed_task(ws, *, tools, canonical):
    async with database.async_session() as s:
        t = Task(
            title="build", status=TaskStatus.DONE.value, workspace_id=ws,
            result_summary="r", model_used="m", canonical_trajectory=canonical,
        )
        s.add(t)
        await s.flush()
        for i, name in enumerate(tools):
            s.add(AgentLogChunk(
                task_id=t.id, workspace_id=ws, chunk_seq=i,
                content=f"output of {name}", tool_name=name,
                created_at=datetime(2026, 1, 1, 12, 0, i),
            ))
        await s.commit()
        return str(t.id)


async def test_match_scored_and_readback(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(
        ws, tools=["web_search", "write_file", "run_tests"],
        canonical=["web_search", "write_file", "run_tests"],
    )

    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-match")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["trajectory_match_profile"]
    assert prof["status"] == "scored"
    assert prof["matched"] is True
    assert prof["metrics"] == {"exact": 1.0, "edit": 1.0, "dag": 1.0}
    assert prof["actual_sequence"] == ["web_search", "write_file", "run_tests"]
    assert prof["reference_sequence"] == ["web_search", "write_file", "run_tests"]
    assert prof["trace_stats"]["tool_steps"] == 3

    r = await auth_client.get(f"/api/quality/records/{tid}/trajectory-match")
    assert r.status_code == 200
    assert r.json()["trajectory_match_profile"]["matched"] is True


async def test_match_imperfect_dag_mode(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    # actual reorders write_file before web_search → DAG precedence violated
    tid = await _seed_task(
        ws, tools=["write_file", "web_search"],
        canonical={"sequence": ["web_search", "write_file"], "match_mode": "dag"},
    )
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-match")
    assert r.status_code == 200
    prof = r.json()["trajectory_match_profile"]
    assert prof["mode"] == "dag"
    assert prof["metrics"]["dag"] == 0.0
    assert prof["matched"] is False


async def test_match_skipped_without_canonical(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(ws, tools=["web_search"], canonical=None)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-match")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["trajectory_match_profile"] is None


async def test_match_settable_via_task_patch(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(ws, tools=["web_search", "write_file"], canonical=None)
    # set the canonical trajectory through the task PATCH endpoint
    r = await auth_client.patch(
        f"/api/tasks/{tid}", json={"canonical_trajectory": ["web_search", "write_file"]}
    )
    assert r.status_code == 200, r.text
    assert r.json()["canonical_trajectory"] == ["web_search", "write_file"]
    # now matching is no longer skipped
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-match")
    assert r.json()["skipped"] is False
    assert r.json()["trajectory_match_profile"]["matched"] is True


async def test_match_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task(DEFAULT_WORKSPACE_ID, tools=["x"], canonical=["x"])
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-trajectory-match")
    assert r.status_code == 404
