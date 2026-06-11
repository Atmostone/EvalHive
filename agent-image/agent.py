"""Minimal agent: LLM tool-calling loop with built-in tools + dynamic MCP tools."""

import asyncio
import json
import logging
import os
import subprocess
import time
import uuid
from contextlib import AsyncExitStack
from datetime import datetime, timezone

import httpx
import litellm

logger = logging.getLogger(__name__)

BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command and return its output. Use for running scripts, installing packages, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write content to a file. Creates parent directories if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path (relative to /workspace/output/)"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read content of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File path to read"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": "Search the project knowledge base for relevant information. Returns text snippets from uploaded documents matching the query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
]


def execute_builtin_tool(name: str, arguments: dict) -> str:
    if name == "bash":
        try:
            result = subprocess.run(
                arguments["command"], shell=True, capture_output=True, text=True,
                timeout=120, cwd="/workspace",
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR: {result.stderr}"
            if result.returncode != 0:
                output += f"\nExit code: {result.returncode}"
            return output or "(no output)"
        except subprocess.TimeoutExpired:
            return "ERROR: Command timed out (120s limit)"
        except Exception as e:
            return f"ERROR: {e}"

    if name == "file_write":
        path = arguments["path"]
        if not path.startswith("/"):
            path = os.path.join("/workspace/output", path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(arguments["content"])
        return f"Written to {path}"

    if name == "file_read":
        path = arguments["path"]
        if not path.startswith("/"):
            path = os.path.join("/workspace", path)
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            return f"ERROR: File not found: {path}"

    if name == "search_knowledge_base":
        query = arguments.get("query", "")
        limit = arguments.get("limit", 5)
        try:
            with httpx.Client(timeout=30.0) as client:
                token = os.environ.get("SPAWNHIVE_AGENT_TOKEN", "")
                task_id = os.environ.get("TASK_ID", "")
                resp = client.post(
                    "http://api:8000/api/knowledge/search",
                    json={"query": query, "limit": limit, "task_id": task_id},
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                )
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    if not results:
                        return "No relevant documents found in knowledge base."
                    parts = [
                        f"[{r.get('filename', '?')}] (score: {r.get('score', 0):.2f})\n{r.get('text', '')}"
                        for r in results
                    ]
                    return "\n\n---\n\n".join(parts)
                return f"ERROR: Knowledge search returned status {resp.status_code}"
        except Exception as e:
            return f"ERROR: Knowledge search failed: {e}"

    return f"ERROR: Unknown builtin tool: {name}"


def build_system_prompt(soul: str) -> str:
    parts = [
        "# Mandatory rules (injected by system)\n",
        "You are executing a specific task. When the task is complete:\n"
        "1. Save all output files to /workspace/output/\n"
        "2. Do NOT interact with the user directly — report through the system\n"
        "3. If the task is impossible or needs info — explain why\n",
    ]
    try:
        with open("/data/rules.md") as f:
            rules = f.read().strip()
            if rules:
                parts.append(f"\n# Rules\n{rules}\n")
    except FileNotFoundError:
        pass

    structured = (os.environ.get("AGENT_MEMORY_CONTEXT") or "").strip()
    if structured:
        parts.append(f"\n{structured}\n")
    else:
        try:
            with open("/data/memory.md") as f:
                memory = f.read().strip()
                if memory:
                    parts.append(f"\n# Memory\n{memory}\n")
        except FileNotFoundError:
            pass

    if soul:
        parts.append(f"\n# Your Role\n{soul}\n")
    return "\n".join(parts)


MCP_SEPARATOR = "__"
PROGRESS_MIN_INTERVAL = 5.0  # seconds


async def _send_progress(payload: dict) -> None:
    webhook_url = os.environ.get("WEBHOOK_URL")
    if not webhook_url:
        return
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    payload["idempotency_key"] = uuid.uuid4().hex
    token = os.environ.get("SPAWNHIVE_AGENT_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(webhook_url, json=payload, headers=headers)
    except Exception as e:
        logger.debug(f"progress webhook failed (suppressed): {e}")


# Per-tool-call max chunk size — must stay below the backend's 256 KB Pydantic
# cap. Larger tool outputs are split into N consecutive chunks by chunk_seq.
LOG_CHUNK_MAX_BYTES = 240 * 1024


async def _send_log_chunk(content: str, tool_name: str | None, seq_iter: list[int]) -> None:
    """POST /api/v1/agent-log/{task_id} — full-output streaming.

    `seq_iter` is a single-element list used as a mutable counter shared with
    the caller (each tool invocation increments it). Long outputs are split
    into multiple consecutive chunks so each POST stays under the backend cap.
    """
    base_url = os.environ.get("API_BASE_URL", "http://api:8000")
    task_id = os.environ.get("TASK_ID", "")
    token = os.environ.get("SPAWNHIVE_AGENT_TOKEN", "")
    if not task_id or not token:
        return

    pieces = [
        content[i : i + LOG_CHUNK_MAX_BYTES]
        for i in range(0, max(len(content), 1), LOG_CHUNK_MAX_BYTES)
    ] or [""]

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{base_url}/api/v1/agent-log/{task_id}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            for piece in pieces:
                payload = {
                    "chunk_seq": seq_iter[0],
                    "content": piece,
                    "tool_name": tool_name,
                    "idempotency_key": uuid.uuid4().hex,
                }
                seq_iter[0] += 1
                try:
                    await client.post(url, json=payload, headers=headers)
                except Exception as e:
                    logger.debug(f"log chunk POST failed (suppressed): {e}")
    except Exception as e:
        logger.debug(f"log chunk transport failed (suppressed): {e}")


async def _connect_mcp_servers(stack: AsyncExitStack, configs: list[dict]) -> tuple[list[dict], dict]:
    """Spawn each MCP server, harvest its tools. Returns (extra_tool_specs, name->session)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    extra_specs: list[dict] = []
    routing: dict[str, tuple] = {}  # prefixed_name -> (session, original_tool_name)

    for cfg in configs:
        srv_name = cfg.get("name")
        cmd = cfg.get("command")
        if not srv_name or not cmd:
            logger.warning(f"Skipping MCP config without name/command: {cfg}")
            continue
        params = StdioServerParameters(
            command=cmd,
            args=cfg.get("args", []) or [],
            env={**os.environ, **(cfg.get("env") or {})},
        )
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_resp = await session.list_tools()
        except Exception as e:
            logger.error(f"MCP server '{srv_name}' failed to start: {e}")
            continue

        for t in tools_resp.tools:
            prefixed = f"{srv_name}{MCP_SEPARATOR}{t.name}"
            extra_specs.append({
                "type": "function",
                "function": {
                    "name": prefixed,
                    "description": t.description or f"MCP tool {t.name} from {srv_name}",
                    "parameters": t.inputSchema or {"type": "object", "properties": {}},
                },
            })
            routing[prefixed] = (session, t.name)
        logger.info(f"MCP server '{srv_name}' contributed {len(tools_resp.tools)} tool(s)")

    return extra_specs, routing


async def _call_mcp_tool(session, tool_name: str, arguments: dict) -> str:
    try:
        result = await session.call_tool(tool_name, arguments=arguments or {})
    except Exception as e:
        return f"ERROR: MCP call_tool failed: {e}"
    parts = []
    for c in result.content:
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(c))
    return "\n".join(parts) if parts else "(no content)"


async def run_agent() -> dict:
    """Run the agent's LLM tool-calling loop. Returns result dict."""
    from feedback_server import queue as control_queue, state as control_state

    task_id = os.environ.get("TASK_ID", "unknown")
    task_description = os.environ.get("TASK_DESCRIPTION", "No task provided")
    agent_soul = os.environ.get("AGENT_SOUL", "")
    model = os.environ.get("LLM_MODEL") or ""
    api_key = os.environ.get("OPENAI_API_KEY") or None
    api_base = os.environ.get("OPENAI_BASE_URL") or None
    # Perturbation judge (E-12): a poisoned tool response. When set, the payload
    # is appended to the FIRST tool result the agent receives, simulating a
    # malicious web page / tool output. Empty in normal runs.
    tool_injection = os.environ.get("AGENT_TOOL_INJECTION") or ""
    # Per-run sampling overrides (SPA-40 experiment axes). Unset in normal runs.
    try:
        temperature = float(os.environ["LLM_TEMPERATURE"])
    except (KeyError, ValueError):
        temperature = None
    try:
        seed = int(os.environ["LLM_SEED"])
    except (KeyError, ValueError):
        seed = None
    if not model:
        raise RuntimeError(
            "LLM_MODEL env var is empty — the orchestrator did not pass a model. "
            "Check the template's model_id and the workspace's system_*_model_id."
        )

    # Built-in tools selection
    try:
        enabled_tools = json.loads(os.environ.get("AGENT_TOOLS", "[]"))
    except json.JSONDecodeError:
        enabled_tools = []
    if enabled_tools:
        tools = [t for t in BUILTIN_TOOLS if t["function"]["name"] in enabled_tools]
    else:
        tools = list(BUILTIN_TOOLS)
    if not tools:
        tools = list(BUILTIN_TOOLS)

    # MCP servers
    try:
        mcp_configs = json.loads(os.environ.get("MCP_SERVERS", "[]"))
    except json.JSONDecodeError:
        mcp_configs = []

    system_prompt = build_system_prompt(agent_soul)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Task: {task_description}"},
    ]
    os.makedirs("/workspace/output", exist_ok=True)

    total_input_tokens = 0
    total_output_tokens = 0
    max_iterations = 20
    last_progress_at = 0.0
    log_chunk_seq = [0]
    injection_done = False

    async with AsyncExitStack() as stack:
        mcp_routing: dict = {}
        if mcp_configs:
            extra_specs, mcp_routing = await _connect_mcp_servers(stack, mcp_configs)
            tools.extend(extra_specs)

        for iteration in range(max_iterations):
            control_state["iteration"] = iteration + 1
            control_state["current_step"] = f"llm_call_{iteration + 1}"

            # Drain control queue (feedback / switch_model / abort)
            aborted = False
            abort_reason = None
            while not control_queue.empty():
                try:
                    cmd = control_queue.get_nowait()
                except Exception:
                    break
                ctype = cmd.get("type")
                if ctype == "feedback":
                    msg = cmd.get("message", "")
                    if msg:
                        messages.append({
                            "role": "user",
                            "content": f"[user feedback]\n{msg}",
                        })
                        logger.info(f"[{task_id}] Injected feedback: {msg[:120]}")
                elif ctype == "switch_model":
                    new_model = cmd.get("model")
                    new_base = cmd.get("base_url")
                    new_key = cmd.get("api_key")
                    if new_model:
                        model = new_model
                    if new_base:
                        api_base = new_base
                    if new_key:
                        api_key = new_key
                    logger.info(f"[{task_id}] Switched model: {model} ({api_base})")
                elif ctype == "abort":
                    aborted = True
                    abort_reason = cmd.get("reason") or "user requested"
            if aborted or control_state.get("abort_requested"):
                return {
                    "task_id": task_id,
                    "event": "aborted",
                    "data": {
                        "reason": abort_reason or control_state.get("abort_reason") or "aborted",
                        "token_usage": {
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
                    },
                }

            logger.info(f"[{task_id}] LLM call iteration {iteration + 1}")
            try:
                completion_kwargs = {}
                if temperature is not None:
                    completion_kwargs["temperature"] = temperature
                if seed is not None:
                    completion_kwargs["seed"] = seed
                response = await litellm.acompletion(
                    model=f"openai/{model}",
                    messages=messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None,
                    api_key=api_key,
                    api_base=api_base,
                    **completion_kwargs,
                )
            except Exception as e:
                logger.error(f"[{task_id}] LLM call failed: {e}")
                return {
                    "task_id": task_id, "event": "failed",
                    "data": {
                        "error": f"LLM call failed: {e}",
                        "token_usage": {
                            "input_tokens": total_input_tokens,
                            "output_tokens": total_output_tokens,
                        },
                    },
                }

            usage = response.usage
            if usage:
                total_input_tokens += usage.prompt_tokens or 0
                total_output_tokens += usage.completion_tokens or 0
            control_state["tokens_input"] = total_input_tokens
            control_state["tokens_output"] = total_output_tokens

            message = response.choices[0].message
            messages.append(message.model_dump())

            if message.tool_calls:
                for tool_call in message.tool_calls:
                    fn = tool_call.function
                    try:
                        args = json.loads(fn.arguments)
                    except json.JSONDecodeError:
                        args = {}
                    control_state["current_step"] = f"tool:{fn.name}"
                    logger.info(f"[{task_id}] Tool call: {fn.name}({json.dumps(args)[:200]})")

                    if fn.name in mcp_routing:
                        session, original = mcp_routing[fn.name]
                        result = await _call_mcp_tool(session, original, args)
                    else:
                        # Built-in tools are sync; offload bash subprocess to a thread
                        result = await asyncio.to_thread(execute_builtin_tool, fn.name, args)

                    # Perturbation injection: poison the first tool response only.
                    if tool_injection and not injection_done:
                        result = f"{result}\n\n{tool_injection}"
                        injection_done = True
                        logger.info(f"[{task_id}] Injected perturbation payload into tool result")

                    logger.info(f"[{task_id}] Tool result: {str(result)[:200]}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(result),
                    })

                    await _send_log_chunk(str(result), fn.name, log_chunk_seq)

                    now = time.monotonic()
                    if now - last_progress_at >= PROGRESS_MIN_INTERVAL:
                        last_progress_at = now
                        await _send_progress({
                            "task_id": task_id,
                            "event": "progress",
                            "data": {
                                "current_step": f"tool:{fn.name}",
                                "tool_name": fn.name,
                                "iteration": iteration + 1,
                                "tokens_used_so_far": {
                                    "input": total_input_tokens,
                                    "output": total_output_tokens,
                                },
                                "recent_output": str(result)[:500],
                            },
                        })
                continue

            result_summary = message.content or "Task completed"
            logger.info(f"[{task_id}] Agent finished: {result_summary[:200]}")
            output_files = []
            output_dir = "/workspace/output"
            if os.path.exists(output_dir):
                for root, _dirs, files in os.walk(output_dir):
                    for f in files:
                        rel_path = os.path.relpath(os.path.join(root, f), output_dir)
                        output_files.append(rel_path)
            return {
                "task_id": task_id, "event": "completed",
                "data": {
                    "result_summary": result_summary,
                    "files": output_files,
                    "token_usage": {
                        "input_tokens": total_input_tokens,
                        "output_tokens": total_output_tokens,
                    },
                },
            }

        return {
            "task_id": task_id, "event": "failed",
            "data": {
                "error": f"Agent exceeded max iterations ({max_iterations})",
                "token_usage": {
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                },
            },
        }
