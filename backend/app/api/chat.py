"""Chat WebSocket endpoint and history API."""

import json
import logging
import os
import uuid

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import resolve_workspace_model
from app.api.events import _ws_authenticate
from app.auth.dependencies import get_current_workspace
from app.database import async_session, get_db
from app.knowledge.rag import search_documents
from app.models.chat_message import ChatMessage
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.models.workspace import Workspace
from app.orchestrator.prompts import build_orchestrator_system_prompt
from app.plugins.llm import get_llm_provider
from app.utils.events import log_event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Chat tools for function calling
CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new task on the kanban board. The task will be created in Backlog status. Set to Ready if it should be executed immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title"},
                    "description": {"type": "string", "description": "Detailed description of what to do"},
                    "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"], "description": "Task priority"},
                    "start_immediately": {"type": "boolean", "description": "If true, set status to Ready so an agent picks it up immediately"},
                },
                "required": ["title", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": "Update the orchestrator's persistent memory (memory.md). Use when you learn something important about the user, their projects, or preferences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "Section heading, e.g. 'User Preferences', 'Project Context'"},
                    "content": {"type": "string", "description": "Content to store under this section"},
                },
                "required": ["section", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "Search the knowledge base for relevant information from uploaded documents. Uses vector similarity search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "top_k": {"type": "integer", "description": "Number of results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
]


SLASH_HELP = """Available commands:
- `/status` — quick stats (active agents, in-progress tasks, awaiting approval, today's tokens)
- `/kill all` — kill all agent containers in this workspace
- `/kill <container_id>` — kill a specific container
- `/spawn <template> "<task title>"` — create a task and mark it ready
- `/board` — link to the kanban board
- `/templates` — list templates by name
- `/tasks` — last 10 tasks
- `/help` — this list
"""


async def handle_slash_command(
    db: AsyncSession, raw: str, workspace_id: uuid.UUID
) -> str:
    from datetime import datetime, timedelta

    from sqlalchemy import func

    parts = raw[1:].split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1] if len(parts) > 1 else ""

    if cmd == "help":
        return SLASH_HELP

    if cmd == "status":
        from app.plugins.runtime import get_agent_runtime

        active = get_agent_runtime().list_active(workspace_id=str(workspace_id))
        in_prog = await db.scalar(
            select(func.count(Task.id)).where(
                Task.status == TaskStatus.IN_PROGRESS.value,
                Task.workspace_id == workspace_id,
            )
        )
        awaiting = await db.scalar(
            select(func.count(Task.id)).where(
                Task.status == TaskStatus.AWAITING_APPROVAL.value,
                Task.workspace_id == workspace_id,
            )
        )
        since = datetime.utcnow() - timedelta(days=1)
        token_row = (
            await db.execute(
                select(
                    func.coalesce(
                        func.sum(func.coalesce(Task.token_usage["input_tokens"].as_integer(), 0)),
                        0,
                    ),
                    func.coalesce(
                        func.sum(func.coalesce(Task.token_usage["output_tokens"].as_integer(), 0)),
                        0,
                    ),
                ).where(
                    Task.completed_at >= since,
                    Task.workspace_id == workspace_id,
                )
            )
        ).first()
        return (
            f"**Status**\n"
            f"- Active agents: {len(active)}\n"
            f"- In-progress tasks: {int(in_prog or 0)}\n"
            f"- Awaiting approval: {int(awaiting or 0)}\n"
            f"- Tokens last 24h: input {int(token_row[0])}, output {int(token_row[1])}"
        )

    if cmd == "kill":
        from app.plugins.runtime import get_agent_runtime

        runtime = get_agent_runtime()
        target = rest.strip()
        if target == "all":
            n = runtime.kill_all(workspace_id=str(workspace_id))
            return f"Killed {n} container(s)."
        if not target:
            return "Usage: `/kill all` or `/kill <container_id>`"
        ok = runtime.kill(target, workspace_id=str(workspace_id))
        return "Killed." if ok else f"Container `{target}` not found."

    if cmd == "spawn":
        # Parse: /spawn <template_name> "task title"
        import re

        match = re.match(r'\s*(\S+)\s+"([^"]+)"\s*$', rest)
        if not match:
            return 'Usage: `/spawn <template_name> "<task title>"`'
        template_name, title = match.group(1), match.group(2)
        templ = (
            await db.execute(
                select(Template).where(
                    Template.name.ilike(template_name),
                    Template.workspace_id == workspace_id,
                )
            )
        ).scalar_one_or_none()
        if not templ:
            return f"Template `{template_name}` not found."
        task = Task(
            title=title,
            description="(spawned via /spawn)",
            template_id=templ.id,
            status=TaskStatus.READY.value,
            workspace_id=workspace_id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return f"Task `{title}` spawned (id `{str(task.id)[:8]}`) with template `{templ.name}`."

    if cmd == "board":
        return "Open the kanban board → [/tasks](/tasks)"

    if cmd == "templates":
        rows = (
            await db.execute(
                select(Template)
                .where(Template.workspace_id == workspace_id)
                .order_by(Template.name)
            )
        ).scalars().all()
        return "Templates:\n" + "\n".join(
            f"- {t.name} ({', '.join(t.tags or []) or 'no tags'})" for t in rows
        )

    if cmd == "tasks":
        rows = (
            await db.execute(
                select(Task)
                .where(Task.workspace_id == workspace_id)
                .order_by(Task.created_at.desc())
                .limit(10)
            )
        ).scalars().all()
        if not rows:
            return "No tasks yet."
        return "Last 10 tasks:\n" + "\n".join(
            f"- `{str(t.id)[:8]}` [{t.status}] {t.title}" for t in rows
        )

    return f"Unknown command: `/{cmd}`. Type `/help` for the list."


def _ws_shared_path(workspace_id: uuid.UUID, name: str) -> str:
    from app.config import get_settings as _gs

    return os.path.join(_gs().data_dir, "shared", str(workspace_id), name)


async def get_context(db: AsyncSession, workspace_id: uuid.UUID) -> dict:
    """Gather context for the orchestrator system prompt (workspace-scoped)."""
    rules = ""
    memory = ""
    try:
        with open(_ws_shared_path(workspace_id, "rules.md")) as f:
            rules = f.read()
    except FileNotFoundError:
        pass
    try:
        with open(_ws_shared_path(workspace_id, "memory.md")) as f:
            memory = f.read()
    except FileNotFoundError:
        pass

    # Templates
    result = await db.execute(
        select(Template).where(Template.workspace_id == workspace_id)
    )
    templates = result.scalars().all()
    templates_desc = "\n".join(
        f"- {t.name}: {t.description}" for t in templates
    ) or "No templates configured yet."

    # Active tasks
    result = await db.execute(
        select(Task).where(
            Task.workspace_id == workspace_id,
            Task.status.in_([
                TaskStatus.IN_PROGRESS.value,
                TaskStatus.REVIEW.value,
                TaskStatus.AWAITING_APPROVAL.value,
            ]),
        )
    )
    active_tasks = result.scalars().all()
    tasks_desc = "\n".join(
        f"- [{t.status}] {t.title}" for t in active_tasks
    ) or "No active tasks."

    return {
        "rules": rules,
        "memory": memory,
        "templates_desc": templates_desc,
        "tasks_desc": tasks_desc,
    }


async def handle_tool_call(
    db: AsyncSession, name: str, arguments: dict, workspace_id: uuid.UUID
) -> str:
    """Execute a chat tool call (workspace-scoped)."""
    if name == "create_task":
        task = Task(
            title=arguments["title"],
            description=arguments.get("description", ""),
            priority=arguments.get("priority", "medium"),
            status=TaskStatus.READY.value if arguments.get("start_immediately") else TaskStatus.BACKLOG.value,
            workspace_id=workspace_id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        await log_event(
            db, "task_created", "orchestrator",
            {"title": task.title, "from_chat": True},
            task_id=task.id, workspace_id=workspace_id,
        )

        status_msg = "and set to Ready (agent will pick it up)" if task.status == "ready" else "in Backlog"
        return f"Created task '{task.title}' (ID: {str(task.id)[:8]}) {status_msg}."

    elif name == "update_memory":
        section = arguments.get("section", "General")
        content = arguments.get("content", "")
        memory_path = _ws_shared_path(workspace_id, "memory.md")

        existing = ""
        try:
            with open(memory_path) as f:
                existing = f.read()
        except FileNotFoundError:
            pass

        section_header = f"## {section}"
        lines = existing.split("\n")
        new_lines = []
        replaced = False
        skip_until_next = False

        for line in lines:
            if skip_until_next:
                if line.startswith("## "):
                    skip_until_next = False
                    new_lines.append(line)
                continue
            if line.strip() == section_header:
                new_lines.append(section_header)
                new_lines.append(content)
                new_lines.append("")
                replaced = True
                skip_until_next = True
            else:
                new_lines.append(line)

        if not replaced:
            new_lines.append(f"\n{section_header}")
            new_lines.append(content)
            new_lines.append("")

        os.makedirs(os.path.dirname(memory_path), exist_ok=True)
        with open(memory_path, "w") as f:
            f.write("\n".join(new_lines))

        await log_event(
            db, "memory_updated", "orchestrator",
            {"section": section, "size_chars": len(content)},
            workspace_id=workspace_id,
        )
        return f"Memory updated: section '{section}'"

    elif name == "search_knowledge":
        query = arguments.get("query", "")
        top_k = arguments.get("top_k", 5)
        results = await search_documents(query, workspace_id=workspace_id, limit=top_k)

        if not results:
            return "No relevant documents found in the knowledge base."

        parts = []
        for r in results:
            parts.append(f"[{r.get('filename', '?')}] (score: {r.get('score', 0):.2f})\n{r.get('text', '')}")
        return "\n\n---\n\n".join(parts)

    return f"Unknown tool: {name}"


@router.get("/api/chat/history")
async def chat_history(
    limit: int = 50,
    offset: int = 0,
    workspace: Workspace = Depends(get_current_workspace),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.workspace_id == workspace.id)
        .order_by(ChatMessage.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    messages = result.scalars().all()
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "related_task_id": str(m.related_task_id) if m.related_task_id else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in reversed(messages)
    ]


@router.websocket("/ws/chat")
async def chat_websocket(ws: WebSocket):
    await ws.accept()
    auth = await _ws_authenticate(ws)
    if not auth:
        await ws.close(code=4401)
        return
    _, workspace = auth
    workspace_id = workspace.id
    logger.info(f"Chat WebSocket connected (ws={workspace_id})")

    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            user_content = msg.get("content", "")

            if not user_content.strip():
                continue

            async with async_session() as db:
                # Save user message
                user_msg = ChatMessage(
                    role="user", content=user_content, workspace_id=workspace_id
                )
                db.add(user_msg)
                await db.commit()

                # Slash commands: handle without LLM
                if user_content.strip().startswith("/"):
                    response_text = await handle_slash_command(
                        db, user_content.strip(), workspace_id=workspace_id
                    )
                    await ws.send_text(json.dumps({"type": "stream", "content": response_text}))
                    await ws.send_text(json.dumps({"type": "done"}))
                    db.add(ChatMessage(
                        role="assistant", content=response_text, workspace_id=workspace_id
                    ))
                    await db.commit()
                    continue

                # Get context and resolve chat model for this workspace
                ctx = await get_context(db, workspace_id=workspace_id)
                try:
                    chat_llm = await resolve_workspace_model(db, workspace_id, "chat")
                except Exception as e:
                    await ws.send_text(json.dumps({
                        "type": "stream",
                        "content": f"⚠ chat model not configured: {e}",
                    }))
                    await ws.send_text(json.dumps({"type": "done"}))
                    continue

                system_prompt = build_orchestrator_system_prompt(
                    rules_md=ctx["rules"],
                    memory_md=ctx["memory"],
                    templates_desc=ctx["templates_desc"],
                    active_tasks_desc=ctx["tasks_desc"],
                )

                # Load recent chat history for context (workspace-scoped)
                result = await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.workspace_id == workspace_id)
                    .order_by(ChatMessage.created_at.desc())
                    .limit(20)
                )
                history = list(reversed(result.scalars().all()))

                messages = [{"role": "system", "content": system_prompt}]
                for m in history:
                    messages.append({"role": m.role, "content": m.content})

                # Call LLM with streaming
                try:
                    response = await get_llm_provider().acompletion(
                        model=chat_llm.model.api_name,
                        messages=messages,
                        tools=CHAT_TOOLS,
                        tool_choice="auto",
                        stream=True,
                        api_key=chat_llm.provider.api_key,
                        api_base=chat_llm.provider.endpoint,
                    )

                    full_content = ""
                    tool_calls_data: dict[int, dict] = {}

                    async for chunk in response:
                        delta = chunk.choices[0].delta

                        # Stream text content
                        if delta.content:
                            full_content += delta.content
                            await ws.send_text(json.dumps({
                                "type": "stream",
                                "content": delta.content,
                            }))

                        # Collect tool calls
                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                idx = tc.index
                                if idx not in tool_calls_data:
                                    tool_calls_data[idx] = {
                                        "id": tc.id or "",
                                        "name": "",
                                        "arguments": "",
                                    }
                                if tc.id:
                                    tool_calls_data[idx]["id"] = tc.id
                                if tc.function:
                                    if tc.function.name:
                                        tool_calls_data[idx]["name"] = tc.function.name
                                    if tc.function.arguments:
                                        tool_calls_data[idx]["arguments"] += tc.function.arguments

                    # Process tool calls if any
                    if tool_calls_data:
                        for idx in sorted(tool_calls_data.keys()):
                            tc = tool_calls_data[idx]
                            try:
                                args = json.loads(tc["arguments"])
                            except json.JSONDecodeError:
                                args = {}

                            result_text = await handle_tool_call(
                                db, tc["name"], args, workspace_id=workspace_id
                            )

                            await ws.send_text(json.dumps({
                                "type": "tool_result",
                                "tool": tc["name"],
                                "result": result_text,
                            }))

                            # Always include tool result in saved content
                            full_content += (("\n\n" if full_content else "") + result_text)

                    # Signal end of response
                    await ws.send_text(json.dumps({"type": "done"}))

                    # Save assistant message
                    if full_content:
                        assistant_msg = ChatMessage(
                            role="assistant", content=full_content, workspace_id=workspace_id
                        )
                        db.add(assistant_msg)
                        await db.commit()

                except Exception as e:
                    logger.error(f"Chat LLM error: {e}", exc_info=True)
                    error_msg = f"Error: {e}"
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "content": error_msg,
                    }))
                    await ws.send_text(json.dumps({"type": "done"}))

    except WebSocketDisconnect:
        logger.info("Chat WebSocket disconnected")
    except Exception as e:
        logger.error(f"Chat WebSocket error: {e}", exc_info=True)
