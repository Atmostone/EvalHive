"""Validate native matrix selections and freeze conditions without running models."""

import itertools
import uuid
from dataclasses import dataclass
from typing import Literal

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from app.benchmarks.launch_inputs import resolve_inputs, scoped
from app.benchmarks.launch_schema import TrialLimits
from app.models.benchmark import BenchmarkDatasetCase, BenchmarkDatasetRevision, BenchmarkProfile
from app.models.workspace import Workspace

MAX_CONFIGS, MAX_CASES, MAX_REPEATS, MAX_RUNS = 24, 300, 20, 1000
AXES = {
    "harness_profile_id",
    "model_id",
    "harness_config",
    "benchmark_config",
    "limits",
    "agent_max_turns",
}


class NativeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["benchmark_sdk"]
    profile_id: uuid.UUID
    dataset_revision_id: uuid.UUID
    case_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=MAX_CASES)


class NativeConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=255)
    harness_profile_id: uuid.UUID
    model_id: uuid.UUID | None = None
    harness_config: dict = Field(default_factory=dict, max_length=128)
    benchmark_config: dict = Field(default_factory=dict, max_length=128)
    limits: TrialLimits = Field(default_factory=TrialLimits)
    agent_max_turns: int = Field(default=100, ge=1, le=1000000)


@dataclass(frozen=True)
class PreparedMatrix:
    workspace: object
    source: dict
    cases: list
    configurations: list
    plans: list
    repeats: int


def expand(configurations, axes=None):
    if not isinstance(configurations, list) or (axes is not None and not isinstance(axes, dict)):
        raise ValueError("Native matrix configurations and axes are invalid")
    raw = list(configurations)
    if axes:
        if set(axes) - AXES or any(
            not isinstance(values, list) or not values for values in axes.values()
        ):
            raise ValueError("Native matrix axes require supported nonempty value lists")
        count = 1
        for values in axes.values():
            count *= len(values)
            if count > MAX_CONFIGS:
                raise ValueError("Native matrix exceeds the configuration limit")
        keys = sorted(axes)
        raw.extend(
            dict(zip(keys, values, strict=True))
            for values in itertools.product(*(axes[key] for key in keys))
        )
    if not raw or len(raw) > MAX_CONFIGS:
        raise ValueError("Native matrix requires 1 to 24 configurations")
    output, seen = [], set()
    for item in raw:
        try:
            config = NativeConfiguration.model_validate(item).model_dump(
                mode="json", exclude_none=True
            )
        except (ValidationError, ValueError):
            raise ValueError(
                "Native configuration requires a harness profile and supported native settings"
            ) from None
        digest = identity({key: value for key, value in config.items() if key != "label"})
        if digest in seen:
            continue
        seen.add(digest)
        output.append(
            {
                **config,
                "config_key": f"cfg-{len(output) + 1:02d}",
                "fingerprint": digest,
                "label": config.get("label") or f"Native configuration {len(output) + 1}",
            }
        )
    return output


async def prepare(db, workspace_id, payload):
    try:
        source = NativeSource.model_validate(payload.get("dataset"))
    except ValidationError:
        raise ValueError(
            "Native matrix requires a workspace benchmark profile and registered dataset revision"
        ) from None
    configs = expand(payload.get("configurations") or [], payload.get("axes"))
    repeats = payload.get("n_runs_per_cell", 1)
    if type(repeats) is not int or not 1 <= repeats <= MAX_REPEATS:
        raise ValueError("Native matrix repetitions must be between 1 and 20")
    if (
        payload.get("max_parallel") not in (None, 1)
        or payload.get("n_toolathlon_lanes") is not None
    ):
        raise ValueError("Native matrices use the common serial environment admission")
    if payload.get("eval_config"):
        raise ValueError(
            "Native benchmark packages own evaluation; external judge overrides are unavailable"
        )
    if payload.get("budget_limit_usd") is not None:
        raise ValueError(
            "Native matrices require per-trial request limits; an unverifiable dollar cap is unsupported"
        )
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("Native matrix workspace is unavailable")
    try:
        profile = await scoped(db, BenchmarkProfile, workspace_id, source.profile_id)
        dataset = await scoped(
            db, BenchmarkDatasetRevision, workspace_id, source.dataset_revision_id
        )
        statement = select(BenchmarkDatasetCase).where(
            BenchmarkDatasetCase.dataset_revision_id == dataset.id
        )
        if source.case_ids is not None:
            if len(set(source.case_ids)) != len(source.case_ids):
                raise ValueError("Native matrix case selection contains duplicates")
            statement = statement.where(BenchmarkDatasetCase.id.in_(source.case_ids))
        cases = list(
            await db.scalars(statement.order_by(BenchmarkDatasetCase.position).limit(MAX_CASES + 1))
        )
        if (
            not cases
            or len(cases) > MAX_CASES
            or (source.case_ids is not None and len(cases) != len(source.case_ids))
        ):
            raise ValueError("Select 1 to 300 cases from the registered workspace dataset")
        if len(configs) * len(cases) * repeats > MAX_RUNS:
            raise ValueError("Native matrix exceeds the 1000 run limit")
        plans, public_cases = [], []
        for case in cases:
            case_key = "sdk:" + case.id.hex
            public_cases.append(
                {
                    "case_key": case_key,
                    "title": case.descriptor["title"],
                    "dataset_case_id": str(case.id),
                    "source_digest": case.descriptor["source_digest"],
                }
            )
            for config in configs:
                frozen = await resolve_inputs(
                    db,
                    workspace,
                    {
                        "profile_id": str(profile.id),
                        "dataset_revision_id": str(dataset.id),
                        "case_id": str(case.id),
                        "harness_profile_id": config["harness_profile_id"],
                    },
                    trial_id=uuid.uuid4(),
                    task_id=uuid.uuid4(),
                    limits=config["limits"],
                    agent_max_turns=config["agent_max_turns"],
                    benchmark_config=config["benchmark_config"],
                    harness_config=config["harness_config"],
                    model_id=config.get("model_id"),
                )
                plans.append((config["config_key"], case_key, frozen))
    except ProtocolError as error:
        raise ValueError(str(error)) from None
    return PreparedMatrix(
        workspace,
        source.model_dump(mode="json", exclude_none=True),
        public_cases,
        configs,
        plans,
        repeats,
    )


async def preview(db, workspace_id, payload):
    prepared = await prepare(db, workspace_id, payload)
    return {
        "n_configs": len(prepared.configurations),
        "n_cases": len(prepared.cases),
        "total_runs": len(prepared.configurations) * len(prepared.cases) * prepared.repeats,
        "n_runs_per_cell": prepared.repeats,
        "est_cost_usd": None,
        "est_duration_minutes": None,
        "warnings": [
            "Native model usage and cost are unknown until recorded. Runs use serial environment admission."
        ],
    }
