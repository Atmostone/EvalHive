import asyncio
import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._resolve_model import resolve_workspace_model
from app.auth.tokens import verify_agent_token
from app.config import get_settings
from app.database import get_db
from app.memory.extractor import extract_memory
from app.models.task import Task, TaskStatus
from app.models.webhook_delivery import WebhookDelivery
from app.orchestrator.engine import check_parent_task_completion
from app.orchestrator.llm import evaluate_agent_result
from app.schemas.webhooks import AgentWebhookEvent
from app.utils.events import broadcast_committed_event, log_event

logger = logging.getLogger(__name__)

# /api/v1/agent-webhook is the canonical path; /api/agent-webhook is a legacy alias.
router = APIRouter(tags=["webhooks"])


_ADAPTER = TypeAdapter(AgentWebhookEvent)


def _is_benchmark(task: Task) -> bool:
    """Benchmark execution path marker (SPA-40).

    Runs created by the Experiment Runner carry ``run_config.benchmark_mode``
    and bypass the human loop entirely: no inline LLM review, no approval
    status, no rejection retries. The run is judged by the quality pipeline
    (E-02/E-07) instead, so a retry here would distort N-run semantics and
    burn the experiment budget.
    """
    return bool((task.run_config or {}).get("benchmark_mode"))


async def _process_webhook(
    task_id: str,
    body: dict,
    request: Request,
    db: AsyncSession,
):
    """Shared core for both /api/v1/agent-webhook and /api/agent-webhook.

    Idempotency contract: the WebhookDelivery row, all event rows, and all task
    mutations live in a SINGLE transaction. Either the whole effect lands
    durably or nothing does. A crash mid-processing leaves no delivery row, so
    the retry can re-process. A concurrent replay races on the UNIQUE
    (task_id, idempotency_key) — the loser rolls back its in-flight mutations
    and returns `{"status":"duplicate"}` cleanly.
    """
    logger.info(f"Webhook received for task {task_id}: event={body.get('event')}")

    # 1. Validate the body BEFORE any DB lookup so attackers can't probe task IDs.
    try:
        validated = _ADAPTER.validate_python(body)
    except ValidationError as ve:
        logger.warning(f"webhook validation failed for {task_id}: {ve}")
        raise HTTPException(status_code=422, detail=ve.errors())

    # 2. Authenticate via the per-task agent service token.
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    plain_token = auth[7:]
    try:
        task_uuid = uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid task id")
    token_row = await verify_agent_token(db, plain=plain_token, task_id=task_uuid)
    if token_row is None:
        raise HTTPException(status_code=401, detail="invalid or expired agent token")

    task = await db.get(Task, task_uuid)
    if not task:
        logger.warning(f"Task {task_id} not found for webhook")
        return {"status": "error", "detail": "Task not found"}

    event = validated.event
    data = validated.data.model_dump()
    idem_key = validated.idempotency_key

    # 3. Reserve the delivery row first (under our open transaction). If a
    # concurrent replay slips in, one of us hits IntegrityError on commit.
    if idem_key:
        delivery = WebhookDelivery(
            task_id=task.id, event_type=event, idempotency_key=idem_key
        )
        db.add(delivery)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            logger.info(
                f"webhook duplicate detected for task {task_id} key={idem_key}"
            )
            return {"status": "duplicate"}

    # All event-writes from here on use commit=False so they live or die with
    # the delivery row. Broadcast happens after the final commit.
    pending_events: list = []

    async def _logev(event_type: str, source: str, payload: dict, *, agent_cid: str | None = None):
        ev = await log_event(
            db, event_type, source, payload,
            task_id=task.id,
            agent_container_id=agent_cid,
            workspace_id=task.workspace_id,
            commit=False,
        )
        pending_events.append(ev)
        return ev

    await _logev("webhook_received", "agent", body, agent_cid=task.agent_container_id)

    try:
        if event == "completed":
            from app.utils.cost import calculate_cost

            task.result_summary = data.get("result_summary", "")
            task.result_files = data.get("files", [])
            task.token_usage = data.get("token_usage", {})
            task.cost_usd = calculate_cost(task, task.token_usage)

            # MinIO upload is best-effort and external; failures don't roll back the tx.
            try:
                from app.storage.minio_client import (
                    upload_task_results,
                    upload_task_results_root,
                )
                settings = get_settings()
                workspace_dir = os.path.join(settings.data_dir, "workspaces", str(task.id))
                # Benchmark/Toolathlon deliverables land at the workspace root with
                # exact filenames; product runs put them under output/. Harvest the
                # matching location so the judge (E-02) actually receives the files
                # instead of grading the self-reported summary alone (SPA-70).
                if _is_benchmark(task):
                    s3_paths = upload_task_results_root(str(task.id), workspace_dir)
                else:
                    s3_paths = upload_task_results(str(task.id), workspace_dir)
                if s3_paths:
                    task.result_files = s3_paths
                    logger.info(f"Uploaded {len(s3_paths)} files to MinIO for task {task.id}")
            except Exception as e:
                logger.warning(f"MinIO upload failed: {e}")

            await _logev(
                "agent_completed", "agent",
                {"result_summary": task.result_summary},
                agent_cid=task.agent_container_id,
            )

            if _is_benchmark(task):
                # Benchmark execution path (SPA-40): terminal immediately.
                task.status = TaskStatus.DONE.value
                task.completed_at = datetime.utcnow()
                await _logev(
                    "benchmark_auto_done", "system",
                    {"reason": "benchmark_mode"},
                )
            else:
                # Run LLM evaluation inside the same transaction. The user briefly
                # sees REVIEW status only if processing takes long enough; otherwise
                # the task moves straight to AWAITING_APPROVAL/READY/FAILED.
                task.status = TaskStatus.REVIEW.value
                try:
                    orchestrator_llm = await resolve_workspace_model(
                        db, task.workspace_id, "orchestrator"
                    )
                    evaluation = await evaluate_agent_result(
                        task.title,
                        task.description or "",
                        task.result_summary or "",
                        task.result_files or [],
                        orchestrator_llm,
                        db=db,
                        task_id=task.id,
                        commit=False,
                    )
                except Exception as e:
                    logger.warning(f"evaluation skipped — model not configured: {e}")
                    evaluation = {"approved": True, "feedback": ""}

                if evaluation.get("approved", True):
                    task.status = TaskStatus.AWAITING_APPROVAL.value
                    await _logev(
                        "orchestrator_decision", "orchestrator",
                        {"action": "auto_review_passed"},
                    )
                else:
                    feedback = evaluation.get("feedback", "Result did not meet requirements")
                    task.orchestrator_feedback = feedback

                    if task.retry_count < task.max_retries:
                        task.retry_count += 1
                        task.status = TaskStatus.READY.value
                        await _logev(
                            "orchestrator_feedback", "orchestrator",
                            {"feedback": feedback, "retry": task.retry_count},
                        )
                    else:
                        task.status = TaskStatus.FAILED.value
                        task.completed_at = datetime.utcnow()
                        await _logev(
                            "orchestrator_decision", "orchestrator",
                            {
                                "action": "auto_review_failed",
                                "feedback": feedback,
                                "retries_exhausted": True,
                            },
                        )

        elif event == "failed":
            from app.utils.cost import calculate_cost
            from app.utils.failures import (
                classify_agent_failure,
                is_contaminated,
                merge_failure_type,
            )

            error = data.get("error", "Unknown error")
            # The type, not just the sentence (SPA-87). The agent reports facts;
            # the classification of what they mean for the experiment happens in
            # one place. NULL when an older image sent nothing — «unclassified»,
            # which is ordinary data, not an accusation and not a clean bill.
            # MERGED, not assigned: the orchestrator may already have recorded a
            # quota death that made this run stop measuring the model, and the
            # agent's own later reason — a cap-hit, say — must not demote it back
            # to an ordinary weak result.
            # Kept because the retry branch below needs to tell the CONDITION
            # this attempt ran under from what the attempt itself did.
            prior_failure = task.failure_type
            task.failure_type = merge_failure_type(
                prior_failure, classify_agent_failure(data.get("failure"))
            )
            task.token_usage = data.get("token_usage", {})
            task.cost_usd = calculate_cost(task, task.token_usage)

            # A benchmark run that ends in `failed` (most often a max-iteration
            # cap-hit) has usually already written real deliverables to the workspace
            # root. Harvest them so they show up in the UI and the judge can grade
            # what is there — the experiment runner judges incomplete benchmark runs
            # when `judge_incomplete_runs` is set (SPA-70 / cap-hit fix). Best-effort.
            if _is_benchmark(task):
                if data.get("result_summary"):
                    task.result_summary = data.get("result_summary")
                try:
                    from app.storage.minio_client import upload_task_results_root

                    settings = get_settings()
                    workspace_dir = os.path.join(
                        settings.data_dir, "workspaces", str(task.id)
                    )
                    s3_paths = upload_task_results_root(str(task.id), workspace_dir)
                    if s3_paths:
                        task.result_files = s3_paths
                        logger.info(
                            f"Harvested {len(s3_paths)} deliverable(s) from failed "
                            f"benchmark task {task.id}"
                        )
                except Exception as e:
                    logger.warning(f"MinIO harvest on failure failed: {e}")

            if not _is_benchmark(task) and task.retry_count < task.max_retries:
                task.retry_count += 1
                task.status = TaskStatus.READY.value
                # Clear only what THIS attempt did. A contamination recorded
                # before the agent ran describes the condition, not the attempt —
                # and the condition survives the retry: when the orchestrator's
                # LLM died on a quota it fell back to a substituted template, and
                # `task.template_id` is now pinned, so the next attempt skips the
                # orchestrator entirely and repeats the same outage-chosen setup.
                # Clearing it wholesale let a retry that then succeeded land in
                # the leaderboard as clean (SPA-87).
                task.failure_type = (
                    prior_failure if is_contaminated(prior_failure) else None
                )
                await _logev(
                    "task_retry", "system",
                    {
                        "retry": task.retry_count,
                        "error": error,
                        "carried_failure_type": task.failure_type,
                    },
                )
            else:
                task.status = TaskStatus.FAILED.value
                task.completed_at = datetime.utcnow()
                await _logev(
                    "agent_failed", "agent",
                    {
                        "error": error,
                        "failure_type": task.failure_type,
                        "retries_exhausted": True,
                    },
                    agent_cid=task.agent_container_id,
                )

        elif event == "progress":
            await _logev(
                "agent_progress", "agent", data,
                agent_cid=task.agent_container_id,
            )

        elif event == "aborted":
            from app.utils.cost import calculate_cost

            task.token_usage = data.get("token_usage", {})
            task.cost_usd = calculate_cost(task, task.token_usage)
            task.status = TaskStatus.FAILED.value
            task.completed_at = datetime.utcnow()
            await _logev(
                "agent_aborted", "agent",
                {"reason": data.get("reason", "aborted")},
                agent_cid=task.agent_container_id,
            )

        # 4. Single commit covers: delivery row + all task mutations + all event rows.
        try:
            await db.commit()
        except IntegrityError:
            # Concurrent replay racing on UNIQUE(task_id, idempotency_key).
            await db.rollback()
            logger.info(
                f"webhook duplicate (commit-time) for task {task_id} key={idem_key}"
            )
            return {"status": "duplicate"}
    except Exception:
        await db.rollback()
        raise

    # 5. Broadcast committed events (best-effort).
    for ev in pending_events:
        await broadcast_committed_event(ev)

    # 6a. Quality Data Lake (E-01): assemble the execution record on a settled
    # terminal state, BEFORE log compaction prunes the chunks (the tool_name
    # source). Best-effort — failure is picked up later by the backfill job.
    if event in ("completed", "failed", "aborted") and task.status in (
        TaskStatus.AWAITING_APPROVAL.value,
        TaskStatus.FAILED.value,
        # Benchmark runs (SPA-40) land on DONE directly.
        TaskStatus.DONE.value,
    ):
        try:
            from app.quality.data_lake import build_quality_record
            await build_quality_record(db, task)
        except Exception as e:
            logger.warning(f"quality record build failed for task {task.id}: {e}")

    # 6b. Compact agent log chunks → MinIO blob on terminal events. Best-effort:
    # failure leaves chunks in DB and `log_archive_s3_path` NULL, so the next
    # webhook delivery (or manual replay) can retry compaction.
    if event in ("completed", "failed", "aborted"):
        try:
            await _compact_agent_log(db, task)
        except Exception as e:
            logger.warning(f"agent log compaction failed for task {task.id}: {e}")

    # 7. Cross-task fan-out — happens AFTER commit so it sees the durable state.
    if event == "completed" and task.status == TaskStatus.AWAITING_APPROVAL.value:
        asyncio.create_task(extract_memory(task.id))
    await check_parent_task_completion(db, task)

    return {"status": "ok"}


async def _compact_agent_log(db: AsyncSession, task: Task) -> None:
    """Serialize all agent_log_chunks for this task → MinIO blob, then prune from DB.

    Uses the JSON-lines archive format (`encode_log_archive`) so each chunk keeps its
    `tool_name` — the cleaned trace (E-06) and trajectory matcher (E-09) stay tool-aware
    after compaction. Atomic: upload first, set s3_path, DELETE rows in same transaction —
    if upload fails, nothing changes.
    """
    from app.models.agent_log import AgentLogChunk
    from app.storage.minio_client import encode_log_archive, upload_log_archive

    if task.log_archive_s3_path:
        return  # idempotent — already compacted on a prior delivery

    result = await db.execute(
        select(AgentLogChunk)
        .where(AgentLogChunk.task_id == task.id)
        .order_by(AgentLogChunk.chunk_seq)
    )
    chunks = result.scalars().all()
    if not chunks:
        return

    blob = encode_log_archive(chunks)
    s3_path = upload_log_archive(str(task.id), blob)

    task.log_archive_s3_path = s3_path
    await db.execute(
        AgentLogChunk.__table__.delete().where(AgentLogChunk.task_id == task.id)
    )
    await db.commit()
    logger.info(
        f"Compacted {len(chunks)} log chunks for task {task.id} → {s3_path}"
    )


@router.post("/api/v1/agent-webhook/{task_id}")
async def agent_webhook_v1(
    task_id: str,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    return await _process_webhook(task_id, body, request, db)


_LEGACY_HEADERS = {
    "Sunset": "Sat, 01 Aug 2026 00:00:00 GMT",
    "Deprecation": "true",
    "Link": '</api/v1/agent-webhook>; rel="successor-version"',
}


@router.post("/api/agent-webhook/{task_id}")
async def agent_webhook_legacy(
    task_id: str,
    body: dict,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Apply Sunset/Deprecation headers up-front so they survive HTTPException paths too.
    for k, v in _LEGACY_HEADERS.items():
        response.headers[k] = v
    try:
        return await _process_webhook(task_id, body, request, db)
    except HTTPException as exc:
        # Re-raise with the deprecation headers attached.
        raise HTTPException(status_code=exc.status_code, detail=exc.detail, headers=_LEGACY_HEADERS)
