"""Integration tests for the Capability-isolation endpoints (E-13, part A).

POST /api/quality/records/{task_id}/evaluate-capability runs the deterministic
Glass-Box harness — did the agent actually use the required tool(s)? — combined
with outcome correctness (a pre-seeded E-02 profile here, so no LLM), and writes
the classification to quality_records.capability_profile; GET .../capability reads
it back; GET /api/quality/capability/aggregate compares by model. Skipped when the
task has no capability_spec; workspace-scoped.
"""

import uuid
from datetime import datetime

from httpx import AsyncClient

from app import database
from app.models.agent_log import AgentLogChunk
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import DEFAULT_WORKSPACE_ID


async def _seed_task(ws, *, tools, spec, weighted=None, model="m"):
    """A done task with the given tool calls, capability_spec, and (optionally) a
    pre-computed E-02 profile (weighted score) so correctness needs no LLM."""
    async with database.async_session() as s:
        t = Task(
            title="cap", status=TaskStatus.DONE.value, workspace_id=ws,
            result_summary="r", model_used=model, capability_spec=spec,
        )
        s.add(t)
        await s.flush()
        for i, name in enumerate(tools):
            s.add(AgentLogChunk(
                task_id=t.id, workspace_id=ws, chunk_seq=i,
                content=f"output of {name}", tool_name=name,
                created_at=datetime(2026, 1, 1, 12, 0, i),
            ))
        if weighted is not None:
            s.add(QualityRecord(
                task_id=t.id, workspace_id=ws, model_used=model,
                template_name="T", final_status=TaskStatus.DONE.value,
                quality_profile={"weighted_score": weighted, "dimensions": []},
            ))
        await s.commit()
        return str(t.id)


async def test_capability_genuine_and_readback(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(
        ws, tools=["web_search", "write_file"],
        spec={"required_tools": ["web_search"], "category": "fresh_data"},
        weighted=8.0,
    )
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    prof = body["capability_profile"]
    assert prof["status"] == "scored"
    assert prof["tool_used"] is True
    assert prof["outcome_correct"] is True
    assert prof["classification"] == "genuine"
    assert prof["capability_passed"] is True
    assert prof["missing_tools"] == []
    assert prof["outcome_signal"] == "judge"

    r = await auth_client.get(f"/api/quality/records/{tid}/capability")
    assert r.status_code == 200
    assert r.json()["capability_profile"]["classification"] == "genuine"


async def test_capability_cheated(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    # Correct outcome, but the required tool was never called → answered from memory.
    tid = await _seed_task(
        ws, tools=["write_file"],
        spec={"required_tools": ["web_search"], "category": "fresh_data"},
        weighted=9.0,
    )
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")
    prof = r.json()["capability_profile"]
    assert prof["tool_used"] is False
    assert prof["outcome_correct"] is True
    assert prof["classification"] == "cheated"
    assert prof["capability_passed"] is False
    assert prof["missing_tools"] == ["web_search"]


async def test_capability_failed_no_tool(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(
        ws, tools=["write_file"],
        spec={"required_tools": ["web_search"]},
        weighted=3.0,
    )
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")
    prof = r.json()["capability_profile"]
    assert prof["outcome_correct"] is False
    assert prof["tool_used"] is False
    assert prof["classification"] == "failed_no_tool"


async def test_capability_skipped_without_spec(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(ws, tools=["web_search"], spec=None, weighted=8.0)
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")
    assert r.status_code == 200
    assert r.json()["skipped"] is True
    assert r.json()["capability_profile"] is None


async def test_capability_settable_via_task_patch(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(ws, tools=["web_search"], spec=None, weighted=8.0)
    r = await auth_client.patch(
        f"/api/tasks/{tid}", json={"capability_spec": {"required_tools": ["web_search"]}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["capability_spec"]["required_tools"] == ["web_search"]
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")
    assert r.json()["skipped"] is False
    assert r.json()["capability_profile"]["classification"] == "genuine"


async def test_capability_aggregate_by_model(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    # model A: genuine; model B: cheated.
    ta = await _seed_task(
        ws, tools=["web_search"], spec={"required_tools": ["web_search"], "category": "fresh_data"},
        weighted=8.0, model="model-a",
    )
    tb = await _seed_task(
        ws, tools=["write_file"], spec={"required_tools": ["web_search"], "category": "fresh_data"},
        weighted=8.0, model="model-b",
    )
    for tid in (ta, tb):
        await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")

    r = await auth_client.get("/api/quality/capability/aggregate")
    assert r.status_code == 200
    agg = r.json()
    assert agg["total"] == 2
    assert agg["genuine"] == 1 and agg["cheated"] == 1
    assert agg["capability_score"] == 0.5
    assert agg["by_model"]["model-a"]["capability_score"] == 1.0
    assert agg["by_model"]["model-b"]["capability_score"] == 0.0
    assert agg["by_category"]["fresh_data"]["total"] == 2


async def test_capability_cross_workspace_404(auth_client: AsyncClient):
    tid = await _seed_task(
        DEFAULT_WORKSPACE_ID, tools=["x"], spec={"required_tools": ["x"]}, weighted=8.0,
    )
    r = await auth_client.post(f"/api/quality/records/{tid}/evaluate-capability")
    assert r.status_code == 404
