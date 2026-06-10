"""Integration tests for the Reproducibility Snapshot endpoints (E-20).

The experiment_snapshot is captured into ``quality_records.reproducibility`` from
the ``agent_spawned`` event. These tests exercise the API surface: (re)capture, read,
diff two snapshots, and replay (clone the task from its snapshot, linked via
``replay_of_task_id``). Capture/replay are owner/admin-only and make no LLM calls.
"""

import uuid
from types import SimpleNamespace

from httpx import AsyncClient
from sqlalchemy import text

from app import database
from app.models.event import AgentEvent
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.quality.reproducibility import assemble_snapshot


def _spawn_data(*, model="gpt-4o", soul="You are a coder.", template_id=None):
    return {
        "template_id": str(template_id) if template_id else None,
        "template_name": "Coder",
        "model_api_name": model,
        "soul_md": soul,
        "tools": ["bash", "file_write"],
        "mcp_servers": [],
        "memory_context": "",
        "flat_memory": {"rules_md": "", "memory_md": ""},
    }


async def _seed_task(
    ws,
    *,
    model_used="gpt-4o",
    template_id=None,
    with_spawn=True,
    reproducibility=None,
    title="t",
):
    async with database.async_session() as s:
        task = Task(
            title=title,
            status=TaskStatus.DONE.value,
            workspace_id=ws,
            model_used=model_used,
            template_id=template_id,
        )
        s.add(task)
        await s.flush()
        if with_spawn:
            s.add(
                AgentEvent(
                    event_type="agent_spawned",
                    source="orchestrator",
                    data=_spawn_data(model=model_used, template_id=template_id),
                    task_id=task.id,
                    workspace_id=ws,
                )
            )
        s.add(
            QualityRecord(
                task_id=task.id,
                workspace_id=ws,
                model_used=model_used,
                template_id=template_id,
                final_status=TaskStatus.DONE.value,
                reproducibility=reproducibility,
            )
        )
        await s.commit()
        return task.id


async def _make_template(ws) -> uuid.UUID:
    async with database.async_session() as s:
        tpl = Template(name="Coder", description="d", soul_md="s", workspace_id=ws)
        s.add(tpl)
        await s.commit()
        return tpl.id


def _snapshot(model, title):
    task_like = SimpleNamespace(
        run_config=None,
        model_used=model,
        title=title,
        description=None,
        reference_answer=None,
        canonical_trajectory=None,
    )
    return assemble_snapshot(task_like, {"model_api_name": model, "tools": ["bash"]})


async def test_capture_populates_and_reads_back(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(ws, with_spawn=True)

    r = await auth_client.post(f"/api/quality/records/{tid}/capture-reproducibility")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is False
    snap = body["reproducibility"]
    assert snap["fingerprint"]
    assert snap["determinism"]["model_api_name"] == "gpt-4o"
    assert "manifest" in snap and "missing" in snap["manifest"]

    # persisted and readable
    r = await auth_client.get(f"/api/quality/records/{tid}/reproducibility")
    assert r.status_code == 200
    assert r.json()["reproducibility"]["fingerprint"] == snap["fingerprint"]


async def test_get_snapshot_404_for_unknown_task(auth_client: AsyncClient):
    r = await auth_client.get(f"/api/quality/records/{uuid.uuid4()}/reproducibility")
    assert r.status_code == 404


async def test_capture_skipped_without_execution_context(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    # No model, no spawn event ⇒ nothing to snapshot.
    tid = await _seed_task(ws, model_used=None, with_spawn=False)
    r = await auth_client.post(f"/api/quality/records/{tid}/capture-reproducibility")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["skipped"] is True
    assert body["reproducibility"] is None


async def test_diff_shows_model_change(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    ta = await _seed_task(
        ws, with_spawn=False, reproducibility=_snapshot("gpt-4o", "a"), title="a"
    )
    tb = await _seed_task(
        ws, with_spawn=False, reproducibility=_snapshot("gpt-4o-mini", "b"), title="b"
    )
    r = await auth_client.get(f"/api/quality/reproducibility/diff?task_a={ta}&task_b={tb}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["identical"] is False
    assert d["changed"]["model_api_name"] == {"from": "gpt-4o", "to": "gpt-4o-mini"}


async def test_diff_404_when_snapshot_missing(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    ta = await _seed_task(ws, with_spawn=False, reproducibility=_snapshot("gpt-4o", "a"))
    tb = await _seed_task(ws, with_spawn=False, reproducibility=None)  # no snapshot
    r = await auth_client.get(f"/api/quality/reproducibility/diff?task_a={ta}&task_b={tb}")
    assert r.status_code == 404


async def test_replay_links_task_and_pins_template(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    template_id = await _make_template(ws)
    tid = await _seed_task(ws, with_spawn=True, template_id=template_id)
    await auth_client.post(f"/api/quality/records/{tid}/capture-reproducibility")

    r = await auth_client.post(f"/api/quality/records/{tid}/replay")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source_task_id"] == str(tid)
    assert out["run_config"]["template_id"] == str(template_id)

    async with database.async_session() as s:
        clone = await s.get(Task, uuid.UUID(out["replay_task_id"]))
    assert clone is not None
    assert str(clone.replay_of_task_id) == str(tid)
    assert clone.template_id == template_id


async def test_replay_404_without_snapshot(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    tid = await _seed_task(ws, with_spawn=False, reproducibility=None)
    r = await auth_client.post(f"/api/quality/records/{tid}/replay")
    assert r.status_code == 404


async def test_capture_and_replay_require_admin(auth_client: AsyncClient):
    ws = auth_client.headers["X-Workspace-Id"]
    tid = await _seed_task(uuid.UUID(ws), with_spawn=True)
    async with database.async_session() as s:
        await s.execute(
            text("UPDATE workspace_members SET role='member' WHERE workspace_id=:w"),
            {"w": ws},
        )
        await s.commit()

    r = await auth_client.post(f"/api/quality/records/{tid}/capture-reproducibility")
    assert r.status_code == 403
    r = await auth_client.post(f"/api/quality/records/{tid}/replay")
    assert r.status_code == 403
