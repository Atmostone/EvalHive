"""Queue a separate evaluation invocation without reopening its source execution."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.benchmarks.jobs import enqueue, find_request
from app.benchmarks.launch_inputs import refresh_authorization
from app.benchmarks.regrade import retained_source
from app.benchmarks.trials import failure
from app.models.benchmark_trial import BenchmarkTrial


async def queue_regrade(
    db, store, evidence, workspace, trial_id, request, *, request_document=None
):
    document = {
        "kind": "regrade",
        "trial_id": str(trial_id),
        **(request if request_document is None else request_document),
    }
    document.pop("request_id", None)
    request_id = uuid.UUID(str(request["request_id"]))
    previous = await find_request(db, workspace.id, request_id, document)
    if previous is not None:
        return previous
    trial = await db.scalar(
        select(BenchmarkTrial).where(
            BenchmarkTrial.id == trial_id,
            BenchmarkTrial.workspace_id == workspace.id,
        )
    )
    if trial is None:
        raise failure("SELECTION_UNAVAILABLE", "Benchmark trial is unavailable")
    source_id = uuid.UUID(str(request["source_evaluation_id"]))
    source = await retained_source(
        store,
        evidence,
        workspace.id,
        trial_id,
        source_id,
        verify_bytes=False,
    )
    # Routes come from immutable evaluator inputs. Only evaluator role IDs are
    # resolved for authorization/key rotation; no agent connection is consulted.
    payload = {
        "format_version": "1.0",
        "auxiliary_models": {
            role: {**model, "api_key": "", "phases": ["evaluating"]}
            for role, model in source["models"].items()
        },
    }
    if not isinstance(trial.snapshot.get("benchmark", {}).get("model_bindings"), dict):
        raise failure("SOURCE_SPEC_UNAVAILABLE", "Product model references were not retained")
    payload = await refresh_authorization(
        db,
        workspace.id,
        trial.snapshot,
        payload,
        evaluation_roles=source["models"],
    )
    deadline = await db.scalar(select(func.clock_timestamp())) + timedelta(
        seconds=request["wall_time_seconds"],
    )
    return await enqueue(
        db,
        store,
        workspace.id,
        trial_id,
        request_id,
        document,
        payload,
        deadline_at=deadline,
        kind="regrade",
        source_evaluation_id=source_id,
    )
