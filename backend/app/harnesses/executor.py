"""Launch and supervise a connector without importing its native implementation."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from pathlib import PurePosixPath

import httpx
from evalhive_harness import validate_run
from evalhive_harness.client import ConnectorClient
from evalhive_runner import __version__ as RUNNER_VERSION
from evalhive_runner.api import HTTPAttemptAPI
from evalhive_runner.redaction import Redactor
from evalhive_runner.runner import AttemptRunner, RunnerPolicy
from evalhive_runner.spool import EventSpool
from sqlalchemy import func, or_, select, text

from app import database
from app.auth.tokens import _utcnow_naive, issue_runner_token
from app.config import get_settings
from app.harnesses.artifacts import artifact_policy, collect_artifacts
from app.harnesses.attempts import (
    FENCED_ERRORS,
    TERMINAL,
    AttemptError,
    acknowledge_command,
    create_attempt,
    ingest_events,
    locked_attempt,
    mark_attempt_lost,
)
from app.harnesses.failures import classify_facts, classify_result
from app.harnesses.projection import load_attempt_view
from app.harnesses.resources import provision_knowledge
from app.models.execution_attempt import ExecutionAttempt
from app.models.task import Task, TaskStatus
from app.runtime import get_process_runtime
from app.runtime.attach import DockerAttachedProcess
from app.runtime.spec import BindMount, NetworkSpec, ProcessSpec, ResourceLimits
from app.runtime.workspace import prepare_layout, seed_workspace, write_private_json
from app.utils.failures import merge_failure_type

_jobs: dict = {}


async def settle_task(db, attempt_id):
    attempt = await locked_attempt(db, attempt_id)
    if attempt.status not in TERMINAL:
        return
    if attempt.completeness.get("task_projection") == "committed":
        await db.commit()
        return
    task = await db.get(Task, attempt.task_id)
    view = await load_attempt_view(db, attempt_id)
    usage = view["usage"]
    task.token_usage = usage
    task.cost_usd = Decimal(usage["cost_usd"]) if usage["cost_usd"] is not None else None
    prefix = f"results/{task.id}/attempts/{attempt.id}/"
    files, invalid_artifacts = [], False
    for event in view["events"]:
        metadata = event["data"].get("metadata", {}) if event["event"] == "artifact" else {}
        if metadata.get("delivery") != "complete":
            continue
        uri = metadata.get("s3_path")
        if (
            event["source"] != "runner"
            or not isinstance(uri, str)
            or not uri.startswith(prefix)
            or len(uri) == len(prefix)
            or ".." in PurePosixPath(uri).parts
        ):
            invalid_artifacts = True
        else:
            files.append(uri)
    task.result_files = list(dict.fromkeys(files))
    invalid_usage = view["completeness"].get("usage") == "invalid"
    task.status = (
        TaskStatus.DONE.value
        if attempt.status == "success" and not invalid_usage and not invalid_artifacts
        else TaskStatus.FAILED.value
    )
    task.result_summary = (attempt.result or {}).get("summary", "Execution ended")
    if invalid_artifacts:
        task.result_summary = "Execution ended with invalid artifact delivery evidence"
    elif invalid_usage:
        task.result_summary = "Execution ended with invalid usage recording"
    task.started_at = attempt.started_at
    task.completed_at = attempt.finished_at
    if (
        attempt.status == "lost"
        or invalid_usage
        or invalid_artifacts
        or (attempt.result or {}).get("error", {}).get("code") == "ARTIFACT_DELIVERY"
    ):
        task.failure_type = merge_failure_type(task.failure_type, "infra")
    else:
        task.failure_type = merge_failure_type(
            task.failure_type, classify_result(attempt.result, status=attempt.status)
        )
    for observed in view["events"]:
        if observed["event"] == "failure.observed":
            task.failure_type = merge_failure_type(
                task.failure_type, classify_facts(observed["data"]["facts"])
            )
    attempt.completeness = {**view["completeness"], "task_projection": "committed"}
    if invalid_artifacts:
        attempt.completeness = {**attempt.completeness, "artifacts": "invalid"}
    from app.utils.events import broadcast_committed_event, log_event

    event = await log_event(
        db,
        "agent_completed" if task.status == TaskStatus.DONE.value else "agent_failed",
        "runner",
        {
            "attempt_id": str(attempt.id),
            "result_summary": task.result_summary,
            "completeness": attempt.completeness,
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        agent_container_id=attempt.container_id,
        commit=False,
    )
    from app.orchestrator.engine import check_parent_task_completion

    parent_event = await check_parent_task_completion(db, task, commit=False)
    await db.commit()
    for committed in (event, parent_event):
        if committed is not None:
            with suppress(Exception):
                await broadcast_committed_event(committed)


async def launch_attempt(
    db,
    *,
    task: Task,
    manifest: dict,
    run_spec: dict,
    credentials: dict[str, str],
    profile_id=None,
    network: NetworkSpec | None = None,
    extra_mounts=(),
    http_client=None,
    finish_hook=None,
):
    """The caller has resolved the profile/environment; no harness branches exist here."""
    validate_run(manifest, run_spec)
    settings, runtime = get_settings(), get_process_runtime()
    environment_id = run_spec.get("environment", {}).get("id", "workspace")
    image_args = (environment_id,) if environment_id in manifest.get("requirements", {}).get("environment_images", {}) else ()
    image_id = await asyncio.to_thread(runtime.resolve_image, manifest["id"], manifest["version"], *image_args)
    if expected := run_spec.get("metadata", {}).get("expected_image_id"):
        if image_id != expected:
            raise AttemptError("REPLAY_IMAGE_CHANGED", "Recorded image is unavailable; create a run with new conditions", 422)
    active = await asyncio.to_thread(runtime.list_active)
    # Serialize capacity across API replicas as well as the orchestrator.
    await db.execute(text("SELECT pg_advisory_xact_lock(8412301)"))
    count = await db.scalar(
        select(func.count())
        .select_from(ExecutionAttempt)
        .where(ExecutionAttempt.status.in_(["created", "running", "stopping"]))
    )
    if max(count or 0, len(active)) >= settings.runtime_max_concurrency:
        raise AttemptError("CAPACITY", "Runner capacity is exhausted")
    if any(row.get("task_id") == str(task.id) for row in active):
        raise AttemptError("PREVIOUS_CONTAINER", "A previous execution container is still running")
    policy = RunnerPolicy()
    spec = deepcopy(run_spec)
    spec["metadata"] = {
        **spec.get("metadata", {}),
        "image_id": image_id,
        "runner_version": RUNNER_VERSION,
        "runner_policy": asdict(policy),
    }
    spec["environment"] = {
        **spec.get("environment", {"id": "workspace"}),
        "artifact_policy": artifact_policy(spec),
    }
    attempt = await create_attempt(
        db,
        workspace_id=task.workspace_id,
        task_id=task.id,
        manifest=manifest,
        run_spec=spec,
        profile_id=profile_id,
    )
    token = await issue_runner_token(
        db, attempt_id=attempt.id, task_id=task.id, workspace_id=task.workspace_id
    )
    credentials = await provision_knowledge(db, attempt, spec, credentials)
    task.status, task.started_at = TaskStatus.IN_PROGRESS.value, _utcnow_naive()
    await db.commit()
    container = None
    layout = None
    try:
        layout = prepare_layout(str(task.id), str(attempt.id))
        if spec.get("environment", {}).get("workspace_seed") == "task":
            await asyncio.to_thread(seed_workspace, layout)
        credential_path = write_private_json(layout, "credentials.json", credentials)
        limits = spec.get("limits", {})
        process = ProcessSpec(
            task_id=str(task.id),
            workspace_id=str(task.workspace_id),
            attempt_id=str(attempt.id),
            connector_id=manifest["id"],
            connector_version=manifest["version"],
            environment_id=environment_id,
            argv=tuple(manifest["entrypoint"]),
            image_id=image_id,
            workdir=spec["cwd"],
            environment={"EVALHIVE_CREDENTIALS_FILE": "/run/evalhive/credentials.json"},
            mounts=(
                layout.workspace_mount(spec["cwd"]),
                BindMount(credential_path, "/run/evalhive/credentials.json"),
                *extra_mounts,
            ),
            resources=ResourceLimits(
                memory=f"{limits.get('memory_mb', 2048)}m",
                cpu_quota=int(limits.get("cpu_cores", 1) * 100000),
            ),
            network=network or NetworkSpec(),
        )
        container = await asyncio.to_thread(runtime.spawn, process)
        locked = await locked_attempt(db, attempt.id)
        if locked.status in TERMINAL:
            raise AttemptError("ATTEMPT_FINISHED", "Attempt ended during container startup")
        attempt.container_id, task.agent_container_id = (
            container.container_id,
            container.container_id,
        )
        await db.commit()
        attached = await DockerAttachedProcess.attach(
            runtime, container.container_id, str(task.workspace_id)
        )
        connector = ConnectorClient(attached)
    except Exception:
        if container is not None:
            with suppress(Exception):
                await asyncio.to_thread(
                    runtime.stop, container.container_id, str(task.workspace_id), grace_seconds=0
                )
        if layout is not None:
            (layout.private / "credentials.json").unlink(missing_ok=True)
        await mark_attempt_lost(db, attempt.id, "Connector could not start")
        await settle_task(db, attempt.id)
        raise AttemptError("START_FAILED", "Connector could not start", 422) from None

    async def execute():
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        runner = None

        async def finalize(result, emit):
            result, announcements = await asyncio.to_thread(collect_artifacts, layout, spec, result)
            for artifact in announcements:
                emit("artifact", artifact)
            return await finish_hook(result, emit) if finish_hook is not None else result

        try:
            with EventSpool(layout.private / "spool.sqlite") as spool:
                runner = AttemptRunner(
                    spec=spec,
                    manifest=manifest,
                    connector=connector,
                    api=HTTPAttemptAPI(settings.runner_api_url, str(attempt.id), token, client),
                    spool=spool,
                    redactor=Redactor([token, *credentials.values()]),
                    policy=policy,
                    finish_hook=finalize,
                )
                await runner.run()
        finally:
            try:
                try:
                    if runner is None:
                        await connector.close(grace=policy.shutdown_seconds)
                    elif not runner.connector_closed:
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                runtime.stop,
                                container.container_id,
                                str(task.workspace_id),
                                grace_seconds=0,
                            ),
                            3,
                        )
                except Exception:
                    pass  # Reconciliation records an unconfirmed stop.
            finally:
                with suppress(Exception):
                    await attached.aclose()
                if owns_client:
                    with suppress(Exception):
                        await client.aclose()
                with suppress(OSError):
                    (layout.private / "credentials.json").unlink(missing_ok=True)
            async with database.async_session() as session:
                await finish_interrupted_attempt(
                    session, attempt.id, "Runner stopped before completion was committed"
                )

    job = asyncio.create_task(execute())
    _jobs[attempt.id] = job

    # Observe exceptions even when an HTTP caller has already disconnected.
    def finished(future):
        _jobs.pop(attempt.id, None)
        if not future.cancelled():
            future.exception()

    job.add_done_callback(finished)
    return attempt


async def recover_spool(db, attempt):
    from pathlib import Path

    path = (
        Path(get_settings().data_dir)
        / "workspaces"
        / str(attempt.task_id)
        / str(attempt.id)
        / "private/spool.sqlite"
    )
    if not path.is_file() or path.is_symlink():
        return
    with EventSpool(path) as spool:
        while batch := spool.batch():
            receipt = await ingest_events(db, attempt.id, batch)
            await db.commit()
            spool.acknowledge(receipt["acked_through"])
        for command_id, body in spool.completed_commands():
            await acknowledge_command(db, attempt.id, uuid.UUID(command_id), body)
            await db.commit()
            spool.command_acknowledged(command_id)


async def finish_interrupted_attempt(db, attempt_id, reason, *, cleanup_confirmed=True):
    """A damaged journal must neither invent success nor keep an attempt alive."""
    try:
        attempt = await locked_attempt(db, attempt_id)
    except AttemptError as error:
        await db.rollback()
        if error.code in FENCED_ERRORS:
            return
        raise
    recovery_failed = False
    try:
        await recover_spool(db, attempt)
    except Exception:
        # Journal content and exception text may contain native output. Record
        # only the fact of failure, and recheck ownership after the rollback.
        recovery_failed = True
        await db.rollback()
    try:
        await mark_attempt_lost(db, attempt_id, reason)
        attempt = await locked_attempt(db, attempt_id)
        details = dict(attempt.completeness)
        if recovery_failed:
            details["recovery"] = "failed"
        if not cleanup_confirmed:
            details["cleanup"] = "unconfirmed"
        attempt.completeness = details
        await settle_task(db, attempt_id)
    except AttemptError as error:
        await db.rollback()
        if error.code not in FENCED_ERRORS:
            raise


async def reconcile_attempts(db):
    runtime = get_process_runtime()
    attempts = list(
        await db.execute(
            select(
                ExecutionAttempt.id,
                ExecutionAttempt.workspace_id,
                ExecutionAttempt.container_id,
                ExecutionAttempt.last_heartbeat_at,
            )
            .join(Task, Task.current_attempt_id == ExecutionAttempt.id)
            .where(
                or_(
                    ExecutionAttempt.status.in_(["created", "running", "stopping"]),
                    ExecutionAttempt.status.in_(TERMINAL)
                    & (
                        func.coalesce(ExecutionAttempt.completeness["task_projection"].astext, "")
                        != "committed"
                    ),
                )
            )
        )
    )
    for attempt in attempts:
        if attempt.id in _jobs and not _jobs[attempt.id].done():
            continue
        if attempt.last_heartbeat_at > _utcnow_naive() - timedelta(seconds=60):
            continue
        cleanup_confirmed = True
        if attempt.container_id:
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        runtime.stop,
                        attempt.container_id,
                        str(attempt.workspace_id),
                        grace_seconds=3,
                    ),
                    10,
                )
            except Exception:
                cleanup_confirmed = False
        await finish_interrupted_attempt(
            db, attempt.id, "Runner heartbeat was lost", cleanup_confirmed=cleanup_confirmed
        )
    # A task can be deleted while Docker startup is in flight, or a completed
    # runner can leave a child container behind. Only the runtime's own role is
    # listed; environment helpers and infrastructure are never candidates.
    try:
        containers = await asyncio.to_thread(runtime.list_active)
    except Exception:
        return
    for container in containers:
        try:
            attempt_id = uuid.UUID(container["attempt_id"])
        except (ValueError, TypeError, KeyError):
            continue
        record = await db.get(ExecutionAttempt, attempt_id)
        if record is not None and record.status not in TERMINAL:
            continue
        with suppress(Exception):
            await asyncio.wait_for(
                asyncio.to_thread(
                    runtime.stop,
                    container["container_id"],
                    container["workspace_id"],
                    grace_seconds=3,
                ),
                10,
            )
