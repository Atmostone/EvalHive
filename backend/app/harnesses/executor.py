"""Launch and supervise a connector without importing its native implementation."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

import httpx
from evalhive_harness import validate_run
from evalhive_harness.client import ConnectorClient
from evalhive_runner import __version__ as RUNNER_VERSION
from evalhive_runner.api import HTTPAttemptAPI
from evalhive_runner.redaction import Redactor
from evalhive_runner.runner import AttemptRunner, RunnerPolicy
from evalhive_runner.spool import EventSpool
from sqlalchemy import func, select, text

from app import database
from app.auth.tokens import _utcnow_naive, issue_runner_token
from app.config import get_settings
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
from app.models.execution_attempt import ExecutionAttempt
from app.models.task import Task, TaskStatus
from app.runtime import get_process_runtime
from app.runtime.attach import DockerAttachedProcess
from app.runtime.spec import BindMount, NetworkSpec, ProcessSpec, ResourceLimits
from app.runtime.workspace import prepare_layout, write_private_json

_jobs: dict = {}


async def settle_task(db, attempt_id):
    attempt = await locked_attempt(db, attempt_id)
    if attempt.status not in TERMINAL:
        return
    task = await db.get(Task, attempt.task_id)
    task.status = TaskStatus.DONE.value if attempt.status == "success" else TaskStatus.FAILED.value
    task.result_summary = (attempt.result or {}).get("summary", "Execution ended")
    task.completed_at = attempt.finished_at
    if attempt.status == "lost":
        task.failure_type = "infra"
    await db.commit()


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
    image_id = await asyncio.to_thread(runtime.resolve_image, manifest["id"], manifest["version"])
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
    task.status, task.started_at = TaskStatus.IN_PROGRESS.value, _utcnow_naive()
    await db.commit()
    container = None
    layout = None
    try:
        layout = prepare_layout(str(task.id), str(attempt.id))
        credential_path = write_private_json(layout, "credentials.json", credentials)
        limits = spec.get("limits", {})
        process = ProcessSpec(
            task_id=str(task.id),
            workspace_id=str(task.workspace_id),
            attempt_id=str(attempt.id),
            connector_id=manifest["id"],
            connector_version=manifest["version"],
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
                    finish_hook=finish_hook,
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
            ).where(ExecutionAttempt.status.in_(["created", "running", "stopping"]))
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
