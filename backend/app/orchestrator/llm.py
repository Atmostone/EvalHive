"""LLM calls for orchestrator decisions via the LLMProvider plugin."""

import json
import logging
import uuid as _uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import ResolvedModel
from app.models.task import Task
from app.plugins.llm import get_llm_provider
from app.utils.events import log_event
from app.utils.failures import classify_llm_error, is_contaminated

logger = logging.getLogger(__name__)


async def _record_reasoning(
    db: Optional[AsyncSession],
    task_id: Optional[_uuid.UUID | str],
    decision: str,
    reasoning: str = "",
    extra: dict | None = None,
    *,
    commit: bool = True,
):
    if db is None:
        return
    payload = {"decision": decision, "reasoning": reasoning}
    if extra:
        payload.update(extra)
    try:
        await log_event(
            db, "orchestrator_reasoning", "orchestrator", payload,
            task_id=task_id, commit=commit,
        )
    except Exception as e:
        logger.warning(f"reasoning log_event failed: {e}")

async def _flag_llm_contamination(db, task_id, exc: Exception) -> None:
    """Mark the task when a provider outage, not the orchestrator, made the call.

    These helpers do not fail on an LLM error — they DEGRADE: template selection
    falls back to the first template, and a failed decomposition decision simply
    proceeds without decomposing. The agent then runs normally and produces a
    perfectly real result, under a condition nobody chose. Only orchestrator:on
    cells reach this code (a pinned template spawns directly), so the substituted
    decision is exactly the treatment being measured — which makes the run no
    longer a measurement of it (SPA-87).

    Only contaminating types are recorded, and only over an empty slot: a reason
    already established elsewhere is not overwritten by this best-effort flag.
    """
    ft = classify_llm_error(type(exc).__name__, getattr(exc, "status_code", None))
    if not is_contaminated(ft) or db is None or task_id is None:
        return
    try:
        task = await db.get(Task, task_id if isinstance(task_id, _uuid.UUID) else _uuid.UUID(str(task_id)))
        if task is not None and task.failure_type is None:
            task.failure_type = ft
    except Exception as e:  # noqa: BLE001 — a flag must never break the run
        logger.warning(f"could not flag orchestrator LLM contamination: {e}")


# Tools the orchestrator LLM can call
ORCHESTRATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "select_template",
            "description": "Select an agent template for the task. Analyze the task description and choose the most appropriate template.",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {
                        "type": "string",
                        "description": "UUID of the selected template",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Brief explanation of why this template was chosen",
                    },
                },
                "required": ["template_id", "reasoning"],
            },
        },
    },
]


async def select_template_for_task(
    task_title: str,
    task_description: str,
    templates: list[dict],
    llm: ResolvedModel,
    db: Optional[AsyncSession] = None,
    task_id: Optional[_uuid.UUID | str] = None,
) -> dict | None:
    """Ask LLM to select the best template for a task.

    Returns dict with template_id and reasoning, or None on failure.
    """
    if not templates:
        logger.warning("No templates available")
        return None

    # If only one template, skip LLM call
    if len(templates) == 1:
        return {
            "template_id": templates[0]["id"],
            "reasoning": "Only one template available",
        }

    templates_desc = "\n".join(
        f"- ID: {t['id']}, Name: {t['name']}, Description: {t['description']}"
        for t in templates
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are an orchestrator that assigns tasks to specialized agents. "
                "Select the most appropriate agent template for the given task. "
                "Use the select_template tool to make your choice."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task: {task_title}\n"
                f"Description: {task_description or 'No description'}\n\n"
                f"Available templates:\n{templates_desc}"
            ),
        },
    ]

    try:
        response = await get_llm_provider().acompletion(
            model=llm.model.api_name,
            messages=messages,
            tools=ORCHESTRATOR_TOOLS,
            tool_choice={"type": "function", "function": {"name": "select_template"}},
            api_key=llm.provider.api_key,
            api_base=llm.provider.endpoint,
        )

        choice = response.choices[0].message
        if choice.tool_calls:
            args = json.loads(choice.tool_calls[0].function.arguments)
            logger.info(f"LLM selected template: {args}")
            await _record_reasoning(
                db, task_id, "template_selected",
                args.get("reasoning", ""),
                {"template_id": args.get("template_id"),
                 "alternatives": [{"id": t["id"], "name": t["name"]} for t in templates]},
            )
            return args

    except Exception as e:
        logger.error(f"LLM template selection failed: {e}")
        await _record_reasoning(
            db, task_id, "template_selection_failed", str(e)[:300],
        )
        await _flag_llm_contamination(db, task_id, e)

    # Fallback: pick the first template
    logger.warning("Falling back to first template")
    await _record_reasoning(
        db, task_id, "template_selected_fallback",
        "LLM selection failed; first available template used",
        {"template_id": templates[0]["id"]},
    )
    return {
        "template_id": templates[0]["id"],
        "reasoning": "Fallback: LLM selection failed",
    }


DECOMPOSE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "decompose_task",
            "description": "Break a complex task into simpler subtasks. Only use if the task clearly requires different skills or multiple distinct steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subtasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "depends_on_indices": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                    "description": "Indices (0-based) of earlier subtasks that must complete first.",
                                },
                            },
                            "required": ["title", "description"],
                        },
                        "description": "List of subtasks; later subtasks may reference earlier ones via depends_on_indices.",
                    },
                },
                "required": ["subtasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_directly",
            "description": "Execute the task with a single agent without decomposition. Use for simple, focused tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reasoning": {"type": "string", "description": "Why this task doesn't need decomposition"},
                },
                "required": ["reasoning"],
            },
        },
    },
]


async def decide_decomposition(
    task_title: str,
    task_description: str,
    templates: list[dict],
    llm: ResolvedModel,
    db: Optional[AsyncSession] = None,
    task_id: Optional[_uuid.UUID | str] = None,
) -> list[dict] | None:
    """Ask LLM whether to decompose a task.

    Returns list of subtask dicts if decomposition needed, None if should execute directly.
    """
    templates_desc = "\n".join(f"- {t['name']}: {t['description']}" for t in templates)

    messages = [
        {
            "role": "system",
            "content": (
                "You decide whether a task should be decomposed into subtasks or executed by a single agent.\n"
                "Only decompose if the task clearly requires different skills or has distinct independent parts.\n"
                "Most tasks should be executed directly without decomposition.\n\n"
                f"Available agent types:\n{templates_desc}"
            ),
        },
        {
            "role": "user",
            "content": f"Task: {task_title}\nDescription: {task_description or 'No description'}",
        },
    ]

    try:
        response = await get_llm_provider().acompletion(
            model=llm.model.api_name,
            messages=messages,
            tools=DECOMPOSE_TOOLS,
            tool_choice="auto",
            api_key=llm.provider.api_key,
            api_base=llm.provider.endpoint,
        )

        choice = response.choices[0].message
        if choice.tool_calls:
            tc = choice.tool_calls[0]
            args = json.loads(tc.function.arguments)

            if tc.function.name == "decompose_task":
                subtasks = args.get("subtasks", [])
                if subtasks:
                    logger.info(f"LLM decided to decompose into {len(subtasks)} subtasks")
                    await _record_reasoning(
                        db, task_id, "decomposition_decided",
                        f"split into {len(subtasks)} subtasks",
                        {"subtasks": [s.get("title") for s in subtasks]},
                    )
                    return subtasks

            await _record_reasoning(
                db, task_id, "decomposition_skipped",
                args.get("reasoning", "execute directly"),
            )

        logger.info("LLM decided to execute directly")
        return None

    except Exception as e:
        logger.error(f"LLM decomposition decision failed: {e}")
        await _record_reasoning(
            db, task_id, "decomposition_failed", str(e)[:300],
        )
        await _flag_llm_contamination(db, task_id, e)
        return None


EVALUATE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "evaluate_result",
            "description": "Evaluate the agent's result. Approve if the task appears completed, reject with feedback if not.",
            "parameters": {
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean", "description": "True if result looks satisfactory"},
                    "feedback": {"type": "string", "description": "Feedback for the agent if not approved"},
                },
                "required": ["approved"],
            },
        },
    },
]


async def evaluate_agent_result(
    task_title: str,
    task_description: str,
    result_summary: str,
    result_files: list[str],
    llm: ResolvedModel,
    db: Optional[AsyncSession] = None,
    task_id: Optional[_uuid.UUID | str] = None,
    *,
    commit: bool = True,
) -> dict:
    """Ask LLM to evaluate an agent's result.

    Returns dict with 'approved' (bool) and 'feedback' (str).
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are reviewing the result of an AI agent's work. "
                "Check if the result matches the task requirements. "
                "Approve if it looks complete and correct. Reject with specific feedback if something is missing or wrong. "
                "Be reasonable — don't reject for minor issues."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Task: {task_title}\n"
                f"Description: {task_description or 'No description'}\n\n"
                f"Agent's result:\n{result_summary}\n\n"
                f"Output files: {', '.join(result_files) if result_files else 'none'}"
            ),
        },
    ]

    try:
        response = await get_llm_provider().acompletion(
            model=llm.model.api_name,
            messages=messages,
            tools=EVALUATE_TOOLS,
            tool_choice={"type": "function", "function": {"name": "evaluate_result"}},
            api_key=llm.provider.api_key,
            api_base=llm.provider.endpoint,
        )

        choice = response.choices[0].message
        if choice.tool_calls:
            args = json.loads(choice.tool_calls[0].function.arguments)
            logger.info(f"LLM evaluation: approved={args.get('approved')}")
            await _record_reasoning(
                db, task_id, "evaluation_done",
                args.get("feedback", "") or ("approved" if args.get("approved") else "rejected"),
                {"approved": bool(args.get("approved"))},
                commit=commit,
            )
            return args

    except Exception as e:
        logger.error(f"LLM evaluation failed: {e}")
        await _record_reasoning(
            db, task_id, "evaluation_failed", str(e)[:300],
            commit=commit,
        )

    # Default to approved on failure (don't block the user)
    return {"approved": True, "feedback": ""}
