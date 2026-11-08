"""Resource assembly and MCP access preserve workspace and attempt boundaries."""

import uuid

import pytest
from app.auth.tokens import issue_runner_token
from app.harnesses.attempts import create_attempt, mark_attempt_lost
from app.harnesses.catalog import get_manifest
from app.harnesses.resources import (
    KNOWLEDGE_REF,
    assemble_resources,
    provision_knowledge,
    workspace_context,
)
from app.models.task import Task


def test_resource_assembly_keeps_secrets_private_and_off_memory_empty():
    declarations, credentials = assemble_resources(
        tools=[],
        memory_mode="off",
        memory_context="must not appear",
        mcp_servers=[
            {
                "name": "test-server",
                "command": "node",
                "args": ["server.mjs"],
                "env": {"API_KEY": "private-fixture-value"},
            }
        ],
    )
    assert "private-fixture-value" not in str(declarations)
    assert "must not appear" not in str(declarations)
    assert list(credentials.values()) == ["private-fixture-value"]
    assert declarations[0]["config"]["names"] == ["mcp__m0_test-server__*"]
    empty, _ = assemble_resources(tools=[])
    default, _ = assemble_resources(tools=None)
    assert empty[0]["config"]["names"] == []
    assert not any(item["kind"] == "tools" for item in default)


def test_memory_off_never_opens_memory_file(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    identity = uuid.uuid4()
    root = tmp_path / "shared" / str(identity)
    root.mkdir(parents=True)
    (root / "rules.md").write_text("Workspace rules")
    (root / "memory.md").symlink_to("/unavailable/should-not-read")
    assert workspace_context(identity, "off") == ("Workspace rules", "")
    with pytest.raises(ValueError, match="symlink"):
        workspace_context(identity, "flat")


async def test_knowledge_mcp_is_read_only_attempt_scoped(auth_client, db_session, monkeypatch):
    workspace = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    task = Task(title="Knowledge MCP fixture", workspace_id=workspace)
    db_session.add(task)
    await db_session.flush()
    attempt_id = uuid.uuid4()
    declarations, _ = assemble_resources(tools=["search_knowledge_base"], attempt_id=attempt_id)
    spec = {
        "protocol_version": "1.0",
        "workspace_id": str(workspace),
        "task_id": str(task.id),
        "attempt_id": str(attempt_id),
        "task": {"prompt": "Fixture"},
        "cwd": "/workspace",
        "config": {},
        "environment": {"id": "fixture", "resources": declarations},
    }
    attempt = await create_attempt(
        db_session,
        workspace_id=workspace,
        task_id=task.id,
        manifest=get_manifest("pi"),
        run_spec=spec,
    )
    private = await provision_knowledge(db_session, attempt, spec, {})
    runner = await issue_runner_token(
        db_session, attempt_id=attempt.id, task_id=task.id, workspace_id=workspace
    )
    await db_session.commit()
    observed = []

    async def search(query, *, workspace_id, limit):
        observed.append((query, workspace_id, limit))
        return [{"content": "Fixture result"}]

    monkeypatch.setattr("app.knowledge.rag.search_documents", search)
    path = f"/api/v1/attempts/{attempt.id}/knowledge/mcp"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_knowledge_base", "arguments": {"query": "fixture", "limit": 3}},
    }
    headers = {"Authorization": private[KNOWLEDGE_REF]}
    response = await auth_client.post(path, json=payload, headers=headers)
    assert response.status_code == 200
    assert "Fixture result" in response.text
    assert observed == [("fixture", workspace, 3)]
    assert (
        await auth_client.post(path, json=payload, headers={"Authorization": "Bearer " + runner})
    ).status_code == 401
    assert (
        await auth_client.post(
            path.replace(str(attempt.id), str(uuid.uuid4())), json=payload, headers=headers
        )
    ).status_code == 401
    payload["params"]["arguments"]["workspace_id"] = str(uuid.uuid4())
    assert (await auth_client.post(path, json=payload, headers=headers)).json()["error"][
        "code"
    ] == -32602
    assert len(observed) == 1
    for method in ("initialize", "tools/list", "ping"):
        rpc = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": method,
            "params": {"protocolVersion": "2025-03-26"} if method == "initialize" else {},
        }
        assert "result" in (await auth_client.post(path, json=rpc, headers=headers)).json()
    assert (
        await auth_client.post(
            path, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
        )
    ).status_code == 202
    assert (
        await auth_client.post(
            path, json={"jsonrpc": "2.0", "id": {}, "method": "ping"}, headers=headers
        )
    ).status_code == 422
    assert (
        await auth_client.post(
            path, json={"jsonrpc": "2.0", "id": 3, "method": "ping", "params": []}, headers=headers
        )
    ).status_code == 422
    other = Task(
        title="Unrelated task cannot extend access",
        workspace_id=workspace,
        current_attempt_id=attempt.id,
    )
    db_session.add(other)
    task.current_attempt_id = None
    await db_session.commit()
    assert (await auth_client.post(path, json=payload, headers=headers)).status_code == 401
    task.current_attempt_id, other.current_attempt_id = attempt.id, None
    await db_session.commit()
    await mark_attempt_lost(db_session, attempt.id, "Fixture complete")
    await db_session.commit()
    assert (await auth_client.post(path, json=payload, headers=headers)).status_code == 401
