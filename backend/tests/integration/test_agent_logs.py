"""End-to-end tests for agent log streaming (Etap 1: ingest + paginated read)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.auth.tokens import issue_agent_token
from app.models.agent_log import AgentLogChunk
from app.models.task import Task


async def _make_task_with_token(auth_client: AsyncClient, db_session):
    create = await auth_client.post(
        "/api/tasks", json={"title": "log test", "description": "x", "priority": "low"}
    )
    assert create.status_code == 201, create.text
    task_id = uuid.UUID(create.json()["id"])
    task = await db_session.get(Task, task_id)
    plain = await issue_agent_token(
        db_session, task_id=task_id, workspace_id=task.workspace_id
    )
    await db_session.commit()
    return task_id, plain


@pytest.mark.asyncio
async def test_log_ingest_rejects_without_bearer(client: AsyncClient):
    r = await client.post(
        "/api/v1/agent-log/00000000-0000-0000-0000-000000000000",
        json={"chunk_seq": 0, "content": "x", "idempotency_key": "k1"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_log_ingest_rejects_invalid_token(client: AsyncClient):
    r = await client.post(
        "/api/v1/agent-log/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": "Bearer bogus"},
        json={"chunk_seq": 0, "content": "x", "idempotency_key": "k1"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_log_ingest_happy_path_and_paginated_read(
    auth_client: AsyncClient, db_session
):
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}

    for seq, content in enumerate(["one", "two", "three"]):
        r = await auth_client.post(
            f"/api/v1/agent-log/{task_id}",
            headers=headers,
            json={
                "chunk_seq": seq,
                "content": content,
                "tool_name": "bash",
                "idempotency_key": f"k-{seq}",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

    r = await auth_client.get(f"/api/tasks/{task_id}/log")
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] is False
    assert [c["content"] for c in body["chunks"]] == ["one", "two", "three"]

    r = await auth_client.get(f"/api/tasks/{task_id}/log?from_seq=2&limit=10")
    assert r.status_code == 200
    chunks = r.json()["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["content"] == "three"


@pytest.mark.asyncio
async def test_log_ingest_idempotent_replay(auth_client: AsyncClient, db_session):
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}
    body = {
        "chunk_seq": 0,
        "content": "once",
        "idempotency_key": "dup-key",
    }

    r1 = await auth_client.post(
        f"/api/v1/agent-log/{task_id}", headers=headers, json=body
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "ok"

    r2 = await auth_client.post(
        f"/api/v1/agent-log/{task_id}", headers=headers, json=body
    )
    assert r2.status_code == 200
    assert r2.json() == {"status": "duplicate"}

    rows = (
        await db_session.execute(
            select(AgentLogChunk).where(AgentLogChunk.task_id == task_id)
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_log_ingest_seq_collision_remaps_to_max_plus_one(
    auth_client: AsyncClient, db_session
):
    """Retry from a fresh container starts at chunk_seq=0 again — must not lose lines."""
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}

    for seq in (0, 1, 2):
        r = await auth_client.post(
            f"/api/v1/agent-log/{task_id}",
            headers=headers,
            json={"chunk_seq": seq, "content": f"first-{seq}", "idempotency_key": f"a-{seq}"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    # Second container starts seq=0 again with a different idempotency_key.
    r = await auth_client.post(
        f"/api/v1/agent-log/{task_id}",
        headers=headers,
        json={"chunk_seq": 0, "content": "second-run", "idempotency_key": "b-0"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["chunk_seq"] == 3  # remapped to max+1

    r = await auth_client.get(f"/api/tasks/{task_id}/log?limit=100")
    contents = [c["content"] for c in r.json()["chunks"]]
    assert contents == ["first-0", "first-1", "first-2", "second-run"]


@pytest.mark.asyncio
async def test_log_get_workspace_isolation(auth_client: AsyncClient, client: AsyncClient, db_session):
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await auth_client.post(
        f"/api/v1/agent-log/{task_id}",
        headers=headers,
        json={"chunk_seq": 0, "content": "secret", "idempotency_key": "i1"},
    )

    # Register a second user/workspace; they must not be able to read the first task's log.
    email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    reg = await client.post(
        "/api/auth/register",
        json={"email": email, "password": "password1234", "display_name": "Other"},
    )
    assert reg.status_code == 200
    payload = reg.json()
    other_headers = {
        "Authorization": f"Bearer {payload['access_token']}",
        "X-Workspace-Id": payload["default_workspace_id"],
    }
    r = await client.get(f"/api/tasks/{task_id}/log", headers=other_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_log_ingest_rejects_oversized_chunk(auth_client: AsyncClient, db_session):
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}
    huge = "a" * (256 * 1024 + 1)
    r = await auth_client.post(
        f"/api/v1/agent-log/{task_id}",
        headers=headers,
        json={"chunk_seq": 0, "content": huge, "idempotency_key": "too-big"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_compaction_concatenates_chunks_and_prunes_db(
    auth_client: AsyncClient, db_session, monkeypatch
):
    """Direct unit-style test of _compact_agent_log that exercises the in-process code path."""
    from app.api.webhooks import _compact_agent_log

    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}
    for i, content in enumerate(["aa", "bb", "cc"]):
        r = await auth_client.post(
            f"/api/v1/agent-log/{task_id}",
            headers=headers,
            json={"chunk_seq": i, "content": content, "idempotency_key": f"u-{i}"},
        )
        assert r.status_code == 200

    captured: dict = {}

    def fake_upload(t, c):
        captured["task_id"] = t
        captured["content"] = c
        return f"logs/{t}.log"

    monkeypatch.setattr(
        "app.storage.minio_client.upload_log_archive", fake_upload, raising=True
    )

    task = await db_session.get(Task, task_id)
    await _compact_agent_log(db_session, task)
    assert captured["task_id"] == str(task_id)
    blob = captured["content"].decode("utf-8")
    assert "aa" in blob and "bb" in blob and "cc" in blob
    assert task.log_archive_s3_path == f"logs/{task_id}.log"

    rows = (
        await db_session.execute(
            select(AgentLogChunk).where(AgentLogChunk.task_id == task_id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_compaction_skips_when_no_chunks(auth_client: AsyncClient, db_session):
    from app.api.webhooks import _compact_agent_log

    task_id, _plain = await _make_task_with_token(auth_client, db_session)
    task = await db_session.get(Task, task_id)
    # No chunks → should be a no-op without attempting upload.
    await _compact_agent_log(db_session, task)
    assert task.log_archive_s3_path is None


@pytest.mark.asyncio
async def test_log_compaction_idempotent_when_already_archived(
    auth_client: AsyncClient, db_session, monkeypatch
):
    """Re-compaction on a task that already has log_archive_s3_path is a no-op."""
    from app.api.webhooks import _compact_agent_log

    task_id, _plain = await _make_task_with_token(auth_client, db_session)
    task = await db_session.get(Task, task_id)
    task.log_archive_s3_path = "logs/already-there.log"
    await db_session.commit()

    calls: list = []

    def fake_upload(t, c):
        calls.append((t, c))
        return "logs/should-not-call.log"

    monkeypatch.setattr(
        "app.storage.minio_client.upload_log_archive", fake_upload, raising=True
    )

    await _compact_agent_log(db_session, task)
    assert calls == []
    assert task.log_archive_s3_path == "logs/already-there.log"


@pytest.mark.asyncio
async def test_log_ws_rejects_other_workspace(client: AsyncClient, db_session):
    """WS connect to a task in another workspace closes with 4404."""
    # User A registers and creates a task.
    email_a = f"a-{uuid.uuid4().hex[:8]}@example.com"
    ra = await client.post(
        "/api/auth/register",
        json={"email": email_a, "password": "password1234", "display_name": "A"},
    )
    assert ra.status_code == 200
    pa = ra.json()
    headers_a = {
        "Authorization": f"Bearer {pa['access_token']}",
        "X-Workspace-Id": pa["default_workspace_id"],
    }
    create = await client.post(
        "/api/tasks",
        json={"title": "x", "description": "y", "priority": "low"},
        headers=headers_a,
    )
    assert create.status_code == 201
    task_id = create.json()["id"]

    # User B (different workspace) cannot read its log.
    email_b = f"b-{uuid.uuid4().hex[:8]}@example.com"
    rb = await client.post(
        "/api/auth/register",
        json={"email": email_b, "password": "password1234", "display_name": "B"},
    )
    assert rb.status_code == 200
    pb = rb.json()
    headers_b = {
        "Authorization": f"Bearer {pb['access_token']}",
        "X-Workspace-Id": pb["default_workspace_id"],
    }
    r = await client.get(f"/api/tasks/{task_id}/log", headers=headers_b)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_log_compaction_on_completed_event(
    auth_client: AsyncClient, db_session, monkeypatch
):
    """End-to-end: ingest chunks → fire completed webhook → chunks moved to MinIO → GET reads from archive."""
    from app.storage import minio_client as mc

    uploaded: dict = {}

    def fake_upload(task_id: str, content: bytes) -> str:
        uploaded["task_id"] = task_id
        uploaded["content"] = content
        return f"logs/{task_id}.log"

    def fake_read(s3_path: str) -> bytes:
        return uploaded["content"]

    monkeypatch.setattr(mc, "upload_log_archive", fake_upload)
    # Re-route the imports webhooks.py + agent_logs.py do at call time:
    from app.api import webhooks as webhooks_mod
    monkeypatch.setattr(
        "app.storage.minio_client.upload_log_archive", fake_upload, raising=True
    )
    monkeypatch.setattr(
        "app.storage.minio_client.read_log_archive", fake_read, raising=True
    )

    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}
    for seq, content in enumerate(["alpha", "beta", "gamma"]):
        r = await auth_client.post(
            f"/api/v1/agent-log/{task_id}",
            headers=headers,
            json={
                "chunk_seq": seq,
                "content": content,
                "idempotency_key": f"c-{seq}",
            },
        )
        assert r.status_code == 200

    # Stub LLM evaluation + MinIO upload_task_results so webhooks.py runs cleanly.
    async def fake_evaluate(*args, **kwargs):
        return {"approved": True}
    monkeypatch.setattr(webhooks_mod, "evaluate_agent_result", fake_evaluate)
    monkeypatch.setattr(
        "app.storage.minio_client.upload_task_results", lambda *a, **k: []
    )

    body = {
        "event": "completed",
        "idempotency_key": "complete-1",
        "data": {
            "result_summary": "done",
            "files": [],
            "token_usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    r = await auth_client.post(
        f"/api/v1/agent-webhook/{task_id}", headers=headers, json=body
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    assert uploaded["task_id"] == str(task_id)
    assert b"alpha" in uploaded["content"] and b"gamma" in uploaded["content"]

    # GET now reads from archive branch.
    r = await auth_client.get(f"/api/tasks/{task_id}/log")
    body_json = r.json()
    assert body_json["archived"] is True
    assert body_json["archive_path"].endswith(".log")
    contents = [c["content"] for c in body_json["chunks"]]
    assert contents == ["alpha", "beta", "gamma"]


# --- tool-call arguments (SPA-86) -------------------------------------------


@pytest.mark.asyncio
async def test_log_ingest_records_the_call_not_just_its_name(
    auth_client: AsyncClient, db_session
):
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}

    r = await auth_client.post(
        f"/api/v1/agent-log/{task_id}",
        headers=headers,
        json={
            "chunk_seq": 0,
            "content": "written",
            "tool_name": "write_file",
            "arguments": {"path": "out/report.md", "mode": "overwrite"},
            "tool_call_id": "call_abc",
            "part_index": 0,
            "part_total": 1,
            "idempotency_key": "args-1",
        },
    )
    assert r.status_code == 200, r.text

    chunk = (
        await db_session.execute(
            select(AgentLogChunk).where(AgentLogChunk.task_id == task_id)
        )
    ).scalar_one()
    assert chunk.arguments == {"path": "out/report.md", "mode": "overwrite"}
    assert chunk.tool_call_id == "call_abc"
    assert chunk.arguments_truncated is False

    body = (await auth_client.get(f"/api/tasks/{task_id}/log")).json()
    assert body["chunks"][0]["arguments"] == {"path": "out/report.md", "mode": "overwrite"}
    assert body["chunks"][0]["tool_call_id"] == "call_abc"


@pytest.mark.asyncio
async def test_log_ingest_clips_oversized_arguments_server_side(
    auth_client: AsyncClient, db_session
):
    """The agent already clips in transit, but «the client promised» is not a size
    limit — a buggy agent must not be able to store a megabyte parameter."""
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}

    r = await auth_client.post(
        f"/api/v1/agent-log/{task_id}",
        headers=headers,
        json={
            "chunk_seq": 0,
            "content": "ok",
            "tool_name": "write_file",
            "arguments": {"path": "big.csv", "content": "x" * 200_000},
            "idempotency_key": "args-big",
        },
    )
    assert r.status_code == 200, r.text

    chunk = (
        await db_session.execute(
            select(AgentLogChunk).where(AgentLogChunk.task_id == task_id)
        )
    ).scalar_one()
    assert chunk.arguments_truncated is True
    # The value shrank; the keys did not — which parameters were passed is the signal.
    assert set(chunk.arguments) == {"path", "content"}
    assert chunk.arguments["path"] == "big.csv"
    assert len(chunk.arguments["content"]) < 200_000


@pytest.mark.asyncio
async def test_log_chunks_without_arguments_still_ingest(
    auth_client: AsyncClient, db_session
):
    """Not every chunk comes from a tool call, and an absent argument set is
    recorded as absent rather than invented."""
    task_id, plain = await _make_task_with_token(auth_client, db_session)
    headers = {"Authorization": f"Bearer {plain}"}
    r = await auth_client.post(
        f"/api/v1/agent-log/{task_id}",
        headers=headers,
        json={"chunk_seq": 0, "content": "plain", "idempotency_key": "no-args"},
    )
    assert r.status_code == 200
    chunk = (
        await db_session.execute(
            select(AgentLogChunk).where(AgentLogChunk.task_id == task_id)
        )
    ).scalar_one()
    assert chunk.arguments is None
    assert chunk.part_index == 0 and chunk.part_total == 1
