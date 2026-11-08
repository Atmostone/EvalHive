"""Record native aggregate receipts without interpreting benchmark mathematics."""

import json
import uuid
from copy import deepcopy

from evalhive_benchmark import ProtocolError, command, encode_line, validate
from evalhive_benchmark.peer import identity
from evalhive_benchmark.protocol import MAX_LINE_BYTES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.benchmarks.driver import FENCED
from app.models.benchmark_evaluation import BenchmarkEvaluationAttempt
from app.models.benchmark_job import BenchmarkJob
from app.models.benchmark_trial import BenchmarkTrial
from app.models.experiment import Experiment, ExperimentRun
from app.models.task import Task


def population_conditions(snapshot):
    """Remove only case-specific identity; retain dataset, models and settings."""
    value = deepcopy(snapshot)
    value["selection"].pop("case_id", None)
    for key in ("case", "case_source_digest"):
        value["dataset"].pop(key, None)
    return value


def behavior_digest(payload):
    return identity(
        {
            "benchmark_manifest": payload["benchmark_manifest"],
            "harness_manifest": payload["harness_manifest"],
            "agent_model": payload["harness_spec"]["model"],
            "native_models": payload["benchmark_spec"]["models"],
            "model_routes": {
                role: {key: value[key] for key in ("name", "endpoint", "phases")}
                for role, value in payload["auxiliary_models"].items()
            },
        }
    )


class NativeAggregation:
    def __init__(self, store, owner, manifest, *, behavior=None):
        self.store, self.owner, self.manifest = store, owner, deepcopy(manifest)
        self.behavior = behavior

    async def population(self, evaluation_id):
        async with AsyncSession(self.store.engine) as db:
            await self.store.current(db, self.owner)
            trial = await db.get(BenchmarkTrial, self.owner.trial_id)
            if trial is None or trial.workspace_id != self.owner.workspace_id:
                raise ProtocolError("INVALID_SCOPE", "Aggregate source is unavailable")
            conditions = population_conditions(trial.snapshot)
            if self.behavior is not None:
                conditions["behavior_digest"] = self.behavior
            task = await db.get(Task, trial.task_id) if trial.task_id else None
            matrix = (task.run_config or {}).get("experiment") if task else None
            selected, expected, origin = [(trial, evaluation_id)], 1, {"kind": "trial"}
            if self.owner.control_id is not None:
                evaluator = await db.get(BenchmarkEvaluationAttempt, evaluation_id)
                if (
                    evaluator is None
                    or evaluator.trial_id != trial.id
                    or evaluator.workspace_id != trial.workspace_id
                ):
                    raise ProtocolError("INVALID_SCOPE", "Native evaluator is unavailable")
                conditions["evaluation_input_digest"] = evaluator.input_digest
                origin = {
                    "kind": "regrade",
                    "source_evaluation_id": str(evaluator.source_evaluation_id),
                }
            if matrix and self.owner.control_id is None:
                experiment = await db.get(Experiment, uuid.UUID(matrix["id"]))
                if experiment is None or experiment.workspace_id != self.owner.workspace_id:
                    raise ProtocolError("POPULATION_UNAVAILABLE", "Native matrix is unavailable")
                config = next(
                    (
                        item
                        for item in experiment.configurations
                        if item["config_key"] == matrix["config_key"]
                    ),
                    None,
                )
                if config is None or config.get("retired_at"):
                    raise ProtocolError("POPULATION_UNAVAILABLE", "Native configuration is retired")
                rows = list(
                    await db.scalars(
                        select(ExperimentRun)
                        .where(
                            ExperimentRun.experiment_id == experiment.id,
                            ExperimentRun.config_key == matrix["config_key"],
                            ExperimentRun.run_index == matrix["run_index"],
                            ExperimentRun.retired_at.is_(None),
                        )
                        .order_by(ExperimentRun.case_key)
                    )
                )
                expected, selected = len(experiment.dataset_cases), []
                origin = {
                    "kind": "matrix",
                    "experiment_id": str(experiment.id),
                    "revision": experiment.revision,
                    "config_key": matrix["config_key"],
                    "run_index": matrix["run_index"],
                    "configuration_fingerprint": config["fingerprint"],
                }
                for row in rows:
                    if row.task_id is None:
                        continue
                    member = await db.scalar(
                        select(BenchmarkTrial).where(
                            BenchmarkTrial.task_id == row.task_id,
                            BenchmarkTrial.workspace_id == self.owner.workspace_id,
                        )
                    )
                    if member is not None:
                        selected.append((member, evaluation_id if member.id == trial.id else None))
            members, results, input_bytes = [], [], 0
            seen = set()
            for member, explicit in selected:
                member_conditions = population_conditions(member.snapshot)
                if self.owner.control_id is not None:
                    member_conditions["evaluation_input_digest"] = conditions[
                        "evaluation_input_digest"
                    ]
                if self.behavior is not None:
                    job = await db.scalar(
                        select(BenchmarkJob)
                        .where(
                            BenchmarkJob.trial_id == member.id,
                            BenchmarkJob.workspace_id == self.owner.workspace_id,
                            BenchmarkJob.kind == "execution",
                        )
                        .options(undefer(BenchmarkJob.private_launch))
                    )
                    if job is None:
                        raise ProtocolError(
                            "POPULATION_UNAVAILABLE", "Frozen native model routes are unavailable"
                        )
                    payload = self.store.private.open(
                        "benchmark-job:" + str(job.id), job.private_launch
                    )
                    member_conditions["behavior_digest"] = behavior_digest(payload)
                if member_conditions != conditions:
                    raise ProtocolError("POPULATION_CONFLICT", "Native aggregate conditions differ")
                statement = select(BenchmarkEvaluationAttempt).where(
                    BenchmarkEvaluationAttempt.trial_id == member.id,
                    BenchmarkEvaluationAttempt.workspace_id == self.owner.workspace_id,
                    BenchmarkEvaluationAttempt.status != "running",
                )
                statement = (
                    statement.where(BenchmarkEvaluationAttempt.id == explicit)
                    if explicit
                    else statement.where(BenchmarkEvaluationAttempt.kind == "initial")
                )
                evaluation = await db.scalar(
                    statement.options(undefer(BenchmarkEvaluationAttempt.private_result))
                )
                if evaluation is None:
                    continue
                key = identity(member.snapshot["dataset"]["case"])
                if key in seen:
                    raise ProtocolError(
                        "POPULATION_CONFLICT", "Native aggregate contains duplicate cases"
                    )
                seen.add(key)
                result = self.store.private.open(
                    "evaluation-result:" + str(evaluation.id), evaluation.private_result
                )
                validate("evaluation_result", result)
                input_bytes += len(json.dumps(result, ensure_ascii=False).encode())
                if input_bytes > MAX_LINE_BYTES - 65536:
                    raise ProtocolError(
                        "AGGREGATE_INPUTS_TOO_LARGE",
                        "Native aggregate exceeds its bounded transport budget",
                        "unsupported",
                    )
                if identity(result) != evaluation.result_digest:
                    raise ProtocolError("EVALUATION_CONFLICT", "Native evaluation digest differs")
                results.append(result)
                members.append(
                    {
                        "trial_id": str(member.id),
                        "evaluation_id": str(evaluation.id),
                        "kind": evaluation.kind,
                        "result_digest": evaluation.result_digest,
                        "input_digest": evaluation.input_digest,
                        "case": deepcopy(member.snapshot["dataset"]["case"]),
                    }
                )
            if not any(member["evaluation_id"] == str(evaluation_id) for member in members):
                raise ProtocolError(
                    "EVALUATION_UNAVAILABLE", "Current evaluator result is unavailable"
                )
            return {
                "format_version": "1.0",
                "origin": origin,
                "conditions": conditions,
                "conditions_digest": identity(conditions),
                "members": members,
                "payload": {"results": results, "expected": expected},
            }

    async def __call__(self, channel, evaluation_id):
        document = None
        try:
            document = await self.population(evaluation_id)
            document["population_digest"] = identity(document)
            if self.manifest["capabilities"].get("aggregate") not in {"native", "emulated"}:
                raise ProtocolError(
                    "UNSUPPORTED_CAPABILITY", "Native aggregation is unavailable", "unsupported"
                )
            identifier = "aggregate." + str(evaluation_id)
            params = {"payload": document["payload"]}
            encode_line(command(identifier, "aggregate", params))
            result = await channel.invoke("aggregate", params, request_id=identifier, timeout=30)
            validate("aggregate_result", result)
            if (
                result["observed"] != len(document["members"])
                or result["expected"] < result["observed"]
                or result["expected"] < document["payload"]["expected"]
            ):
                raise ProtocolError(
                    "AGGREGATE_CONFLICT", "Native aggregate population differs", "grading"
                )
            await self.store.note(
                self.owner,
                "aggregate.completed",
                private={
                    **document,
                    "status": "completed",
                    "evaluation_id": str(evaluation_id),
                    "request_id": identifier,
                    "worker_actor_id": str(channel.actor_id),
                    "result": result,
                    "result_digest": identity(result),
                },
            )
        except ProtocolError as error:
            if error.code in FENCED:
                raise
            await self.store.note(
                self.owner,
                "aggregate.unavailable",
                code=error.code,
                private={
                    **(document or {}),
                    "status": "unavailable",
                    "evaluation_id": str(evaluation_id),
                    "reason": error.code,
                },
            )
