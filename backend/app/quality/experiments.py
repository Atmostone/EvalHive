"""Experiment Runner / A/B Matrix Harness (SPA-40).

A first-class **Experiment** runs a frozen dataset of cases against a matrix of
agent configurations, ``n_runs_per_cell`` times each, over the benchmark
execution path (direct spawn with ``run_config.benchmark_mode`` — no
orchestrator decision-making for ``orchestrator: off`` cells, no approval
flow, no retries) with evaluation always on.

This module holds the pure helpers: configuration-matrix expansion (explicit
list + cartesian ``axes``, deduped by canonical fingerprint) and dataset
freezing (benchmark suite / existing tasks / custom upload → the uniform
``dataset_cases`` shape stored on the experiment, immune to later edits of
suite files or source tasks). The DB-bound service (create / start / tick /
report) builds on top of these.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.experiment import (
    Experiment,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.registry_entry import RegistryEntry
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.quality.benchmark import _capability_spec_for, load_cases
from app.quality.runs_common import (
    SUCCESS_TASK as _SUCCESS_TASK,
    TERMINAL_TASK as _TERMINAL_TASK,
    inflight_target,
)

logger = logging.getLogger(__name__)

TERMINAL_EXPERIMENT = {
    ExperimentStatus.COMPLETED.value,
    ExperimentStatus.CAPPED.value,
    ExperimentStatus.FAILED.value,
    ExperimentStatus.CANCELLED.value,
}

# Fallbacks for the preview estimate when no historical runs exist yet.
DEFAULT_RUN_COST_USD = 0.05
DEFAULT_RUN_DURATION_S = 120

# Every key a configuration may vary on. ``orchestrator`` toggles the
# execution path; the rest map 1:1 onto run_config overrides the engine
# already honors (template_id pins the engine fast path).
CONFIG_AXES = (
    "orchestrator",
    "template_id",
    "model_id",
    "temperature",
    "seed",
    "soul_md",
    "tools_override",
    "memory_mode",
)
MEMORY_MODES = ("off", "flat", "structured")

MAX_CONFIGS = 24
MAX_CASES = 300
MAX_N_RUNS = 20
MAX_TOTAL_RUNS = 1000


# --- configuration matrix ---------------------------------------------------


def _config_fingerprint(cfg: dict) -> str:
    """Canonical-JSON fingerprint over the variation axes (dedup identity)."""
    canon = {k: cfg.get(k) for k in CONFIG_AXES if cfg.get(k) is not None}
    canon["orchestrator"] = bool(cfg.get("orchestrator"))
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _config_label(cfg: dict) -> str:
    """Compact human label for an unlabeled configuration."""
    parts = []
    if cfg.get("model_id"):
        parts.append(f"model={str(cfg['model_id'])[:8]}")
    if cfg.get("template_id"):
        parts.append(f"tpl={str(cfg['template_id'])[:8]}")
    if cfg.get("temperature") is not None:
        parts.append(f"temp={cfg['temperature']}")
    if cfg.get("seed") is not None:
        parts.append(f"seed={cfg['seed']}")
    if cfg.get("soul_md"):
        parts.append("soul=custom")
    if cfg.get("tools_override"):
        parts.append("tools=override")
    if cfg.get("memory_mode"):
        parts.append(f"mem={cfg['memory_mode']}")
    parts.append("orch=on" if cfg.get("orchestrator") else "orch=off")
    return " ".join(parts)


def _config_errors(cfg: dict) -> list[str]:
    errors = []
    if cfg["orchestrator"]:
        if cfg.get("template_id"):
            errors.append("orchestrator:on configuration must not pin template_id")
        if cfg.get("tools_override"):
            errors.append(
                "orchestrator:on configuration cannot use tools_override "
                "(it is template-relative and the orchestrator selects templates)"
            )
    elif not cfg.get("template_id"):
        errors.append("orchestrator:off configuration requires template_id")
    mode = cfg.get("memory_mode")
    if mode is not None and mode not in MEMORY_MODES:
        errors.append(f"invalid memory_mode '{mode}' (expected one of {MEMORY_MODES})")
    temp = cfg.get("temperature")
    if temp is not None:
        try:
            ok = 0.0 <= float(temp) <= 2.0
        except (TypeError, ValueError):
            ok = False
        if not ok:
            errors.append(f"temperature out of range [0, 2]: {temp!r}")
    return errors


def expand_matrix(
    configurations: list[dict] | None, axes: dict | None = None
) -> list[dict]:
    """Expand a matrix request into a validated, deduped, keyed config list.

    Both composition styles are supported and combinable: an explicit
    ``configurations`` list and a cartesian product over ``axes`` (each axis a
    list of values). Configurations with the same canonical fingerprint
    collapse to the first occurrence; keys ``cfg-01``… are assigned in order.
    Raises ``ValueError`` on an invalid spec.
    """
    raw = [dict(c) for c in (configurations or [])]
    if axes:
        unknown = sorted(set(axes) - set(CONFIG_AXES))
        if unknown:
            raise ValueError(f"unknown axes: {unknown}")
        keys = [k for k in CONFIG_AXES if axes.get(k)]
        if keys:
            for combo in itertools.product(*(axes[k] for k in keys)):
                raw.append(dict(zip(keys, combo)))
    if not raw:
        raise ValueError("experiment needs at least one configuration")

    expanded: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw, 1):
        label = item.get("label")
        cfg = {k: item.get(k) for k in CONFIG_AXES if item.get(k) is not None}
        cfg["orchestrator"] = bool(item.get("orchestrator"))
        fingerprint = _config_fingerprint(cfg)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        errors = _config_errors(cfg)
        if errors:
            raise ValueError(f"configuration {i}: " + "; ".join(errors))
        cfg["fingerprint"] = fingerprint
        cfg["label"] = label or _config_label(cfg)
        expanded.append(cfg)

    if len(expanded) > MAX_CONFIGS:
        raise ValueError(f"too many configurations: {len(expanded)} > {MAX_CONFIGS}")
    for i, cfg in enumerate(expanded, 1):
        cfg["config_key"] = f"cfg-{i:02d}"
    return expanded


# --- dataset freezing -------------------------------------------------------


class UploadCaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    description: Optional[str] = None


class UploadCase(BaseModel):
    """One custom-uploaded case (a parsed JSONL line)."""

    model_config = ConfigDict(extra="forbid")

    task_input: UploadCaseInput
    case_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reference_answer: Optional[str] = None
    rubric: Optional[Any] = None
    canonical_trajectory: Optional[Any] = None
    capability_spec: Optional[dict] = None


def _frozen_case(case_key: str, title: str, **optional) -> dict:
    case = {"case_key": case_key, "title": title}
    for key, value in optional.items():
        if value is not None:
            case[key] = value
    return case


def cases_from_upload(raw_cases: list[dict]) -> list[dict]:
    """Validate + freeze custom-uploaded cases.

    Raises ``ValueError`` with a per-case, per-field message on the first
    invalid entry (the AC requires a clear format error for uploads).
    """
    if not raw_cases:
        raise ValueError("upload contains no cases")
    frozen: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_cases, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"case {i}: expected a JSON object, got {type(raw).__name__}")
        try:
            case = UploadCase(**raw)
        except ValidationError as ve:
            first = ve.errors()[0]
            loc = ".".join(str(part) for part in first["loc"]) or "(root)"
            raise ValueError(f"case {i}: {loc}: {first['msg']}") from ve
        key = case.case_id or f"upload-{i:03d}"
        if key in seen:
            raise ValueError(f"case {i}: duplicate case_id '{key}'")
        seen.add(key)
        frozen.append(
            _frozen_case(
                key,
                case.task_input.title,
                description=case.task_input.description,
                reference_answer=case.reference_answer,
                canonical_trajectory=case.canonical_trajectory,
                capability_spec=case.capability_spec,
                rubric=case.rubric,
            )
        )
    return frozen


def cases_from_suite(suite: str, case_ids: list[str] | None = None) -> list[dict]:
    """Freeze benchmark-suite cases (all, or the listed ``case_ids``)."""
    cases = load_cases(suite)
    if case_ids:
        wanted = set(case_ids)
        cases = [c for c in cases if c.id in wanted]
        missing = sorted(wanted - {c.id for c in cases})
        if missing:
            raise ValueError(f"unknown case ids in suite '{suite}': {missing}")
    if not cases:
        raise ValueError(f"suite '{suite}' has no cases")
    return [
        _frozen_case(
            c.id,
            c.input.title,
            description=c.input.description,
            category=c.category,
            reference_answer=c.gold.reference_answer,
            canonical_trajectory=c.gold.canonical_trajectory,
            capability_spec=_capability_spec_for(c),
            rubric=c.gold.rubric,
        )
        for c in cases
    ]


def cases_from_tasks(tasks: list) -> list[dict]:
    """Snapshot existing tasks as frozen cases (input + gold fields only).

    Children are later built fresh from the frozen case — uniform with the
    other sources and immune to later edits of the source tasks.
    """
    if not tasks:
        raise ValueError("dataset.task_ids matched no tasks")
    frozen: list[dict] = []
    seen: set[str] = set()
    for t in tasks:
        key = f"task-{t.id.hex[:8]}"
        if key in seen:
            key = f"task-{t.id.hex[:16]}"
        seen.add(key)
        frozen.append(
            _frozen_case(
                key,
                t.title,
                description=t.description,
                reference_answer=t.reference_answer,
                canonical_trajectory=t.canonical_trajectory,
                capability_spec=t.capability_spec,
            )
        )
    return frozen


def normalize_dataset(spec: dict, *, tasks: list | None = None) -> list[dict]:
    """Freeze a dataset spec into the uniform ``dataset_cases`` list.

    ``tasks`` carries the pre-loaded Task rows for ``source: tasks`` (the
    DB lookup happens in the service; this stays pure).
    """
    source = (spec or {}).get("source")
    if source == "benchmark_suite":
        if not spec.get("suite"):
            raise ValueError("dataset.suite is required for the benchmark_suite source")
        cases = cases_from_suite(spec["suite"], spec.get("case_ids"))
    elif source == "tasks":
        cases = cases_from_tasks(tasks or [])
    elif source == "upload":
        cases = cases_from_upload(spec.get("cases") or [])
    else:
        raise ValueError(f"unknown dataset source: {source!r}")
    if len(cases) > MAX_CASES:
        raise ValueError(f"too many cases: {len(cases)} > {MAX_CASES}")
    return cases


# --- service (DB-bound) -----------------------------------------------------


async def _validate_config_refs(
    db: AsyncSession, workspace_id: uuid.UUID, configs: list[dict]
) -> None:
    """Check that every template/model/registry reference exists in the workspace."""
    errors: list[str] = []

    def _uuid(value, what: str) -> Optional[uuid.UUID]:
        try:
            return uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            errors.append(f"invalid {what} id: {value!r}")
            return None

    template_ids = {c["template_id"] for c in configs if c.get("template_id")}
    for tid in sorted(template_ids):
        parsed = _uuid(tid, "template")
        if parsed is None:
            continue
        tpl = await db.get(Template, parsed)
        if tpl is None or tpl.workspace_id != workspace_id:
            errors.append(f"template {tid} not found in workspace")

    model_ids = {c["model_id"] for c in configs if c.get("model_id")}
    for mid in sorted(model_ids):
        parsed = _uuid(mid, "model")
        if parsed is None:
            continue
        model = await db.get(LLMModel, parsed)
        provider = await db.get(Provider, model.provider_id) if model else None
        if model is None or provider is None or provider.workspace_id != workspace_id:
            errors.append(f"model {mid} not found in workspace")

    registry_ids: set[str] = set()
    for c in configs:
        override = c.get("tools_override") or {}
        for key in ("enable", "disable"):
            registry_ids.update(str(x) for x in override.get(key) or [])
    for rid in sorted(registry_ids):
        parsed = _uuid(rid, "registry entry")
        if parsed is None:
            continue
        entry = await db.get(RegistryEntry, parsed)
        if entry is None or entry.workspace_id != workspace_id:
            errors.append(f"registry entry {rid} not found in workspace")

    if errors:
        raise ValueError("; ".join(errors))


async def create_experiment(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    payload: dict,
    created_by: str = "user",
    frozen_cases: Optional[list[dict]] = None,
) -> Experiment:
    """Validate + freeze the experiment request into a draft Experiment.

    ``payload``: {name, description?, dataset, configurations?, axes?,
    n_runs_per_cell?, budget_limit_usd?, max_parallel?, eval_config?}.
    ``frozen_cases`` carries already-frozen dataset cases (the clone path),
    skipping re-normalization. Raises ValueError on any invalid part (the API
    maps it to 400).
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("experiment name is required")

    configs = expand_matrix(payload.get("configurations"), payload.get("axes"))
    await _validate_config_refs(db, workspace_id, configs)

    dataset_spec = payload.get("dataset") or {}
    tasks: list[Task] | None = None
    if frozen_cases is None and dataset_spec.get("source") == "tasks":
        ids = []
        for raw in dataset_spec.get("task_ids") or []:
            try:
                ids.append(uuid.UUID(str(raw)))
            except ValueError:
                raise ValueError(f"invalid task id in dataset: {raw!r}")
        rows = (
            await db.execute(
                select(Task).where(
                    Task.id.in_(ids), Task.workspace_id == workspace_id
                )
            )
        ).scalars().all() if ids else []
        if len(rows) != len(ids):
            found = {t.id for t in rows}
            missing = [str(i) for i in ids if i not in found]
            raise ValueError(f"tasks not found in workspace: {missing}")
        by_id = {t.id: t for t in rows}
        tasks = [by_id[i] for i in ids]
    cases = (
        frozen_cases
        if frozen_cases is not None
        else normalize_dataset(dataset_spec, tasks=tasks)
    )
    if not cases:
        raise ValueError("dataset is empty")

    n_runs = int(payload.get("n_runs_per_cell") or 1)
    if not (1 <= n_runs <= MAX_N_RUNS):
        raise ValueError(f"n_runs_per_cell must be between 1 and {MAX_N_RUNS}")
    total_runs = len(configs) * len(cases) * n_runs
    if total_runs > MAX_TOTAL_RUNS:
        raise ValueError(
            f"matrix too large: {len(configs)} configs × {len(cases)} cases × "
            f"{n_runs} runs = {total_runs} > {MAX_TOTAL_RUNS}"
        )

    max_parallel = payload.get("max_parallel")
    if max_parallel is not None and int(max_parallel) < 1:
        raise ValueError("max_parallel must be >= 1")
    budget = payload.get("budget_limit_usd")
    if budget is not None and Decimal(str(budget)) <= 0:
        raise ValueError("budget_limit_usd must be positive")

    # Upload cases are already frozen in dataset_cases — don't store them twice.
    stored_dataset = {k: v for k, v in dataset_spec.items() if k != "cases"}
    if dataset_spec.get("source") == "upload":
        stored_dataset["n_cases"] = len(cases)

    exp = Experiment(
        workspace_id=workspace_id,
        name=name,
        description=payload.get("description"),
        dataset=stored_dataset,
        dataset_cases=cases,
        matrix_spec={
            "configurations": payload.get("configurations") or [],
            "axes": payload.get("axes"),
        },
        configurations=configs,
        n_runs_per_cell=n_runs,
        budget_limit_usd=Decimal(str(budget)) if budget is not None else None,
        max_parallel=int(max_parallel) if max_parallel is not None else None,
        eval_config=payload.get("eval_config") or {},
        created_by=created_by,
    )
    db.add(exp)
    await db.commit()
    await db.refresh(exp)
    return exp


async def start_experiment(db: AsyncSession, exp: Experiment) -> None:
    """draft → running: materialize every matrix cell as a pending run row."""
    if exp.status != ExperimentStatus.DRAFT.value:
        raise ValueError(f"cannot run experiment in status '{exp.status}'")
    for cfg in exp.configurations:
        for case in exp.dataset_cases:
            for idx in range(exp.n_runs_per_cell):
                db.add(
                    ExperimentRun(
                        experiment_id=exp.id,
                        config_key=cfg["config_key"],
                        case_key=case["case_key"],
                        run_index=idx,
                    )
                )
    exp.status = ExperimentStatus.RUNNING.value
    exp.started_at = datetime.utcnow()
    await db.commit()


async def pause_experiment(db: AsyncSession, exp: Experiment) -> None:
    """running → paused: the tick stops claiming; in-flight runs finish."""
    if exp.status != ExperimentStatus.RUNNING.value:
        raise ValueError(f"cannot pause experiment in status '{exp.status}'")
    exp.status = ExperimentStatus.PAUSED.value
    await db.commit()


async def resume_experiment(db: AsyncSession, exp: Experiment) -> None:
    if exp.status != ExperimentStatus.PAUSED.value:
        raise ValueError(f"cannot resume experiment in status '{exp.status}'")
    exp.status = ExperimentStatus.RUNNING.value
    await db.commit()


async def cancel_experiment(db: AsyncSession, exp: Experiment) -> None:
    """Stop the experiment, keeping partial results.

    Settled cells keep their results; pending AND in-flight cells become
    ``skipped`` (the tick no longer advances a cancelled experiment, so
    leaving them ``running`` would strand them). In-flight agent containers
    are killed best-effort.
    """
    if exp.status in TERMINAL_EXPERIMENT:
        raise ValueError(f"experiment already terminal ('{exp.status}')")
    rows = (
        await db.execute(
            select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id)
        )
    ).scalars().all()
    now = datetime.utcnow()
    inflight_ids = [
        r.task_id
        for r in rows
        if r.status == ExperimentRunStatus.RUNNING.value and r.task_id
    ]
    for r in rows:
        if r.status in (
            ExperimentRunStatus.PENDING.value,
            ExperimentRunStatus.RUNNING.value,
        ):
            r.status = ExperimentRunStatus.SKIPPED.value
            r.completed_at = now
    exp.status = ExperimentStatus.CANCELLED.value
    exp.completed_at = now
    await db.commit()

    if inflight_ids:
        from app.plugins.runtime import get_agent_runtime

        tasks = (
            await db.execute(select(Task).where(Task.id.in_(inflight_ids)))
        ).scalars().all()
        for t in tasks:
            if t.agent_container_id and t.status not in _TERMINAL_TASK:
                try:
                    get_agent_runtime().kill(t.agent_container_id)
                    t.status = TaskStatus.FAILED.value
                    t.completed_at = now
                except Exception as e:
                    logger.warning(f"experiment cancel: kill failed for {t.id}: {e}")
        await db.commit()


def child_run_config(
    exp: Experiment, cfg: dict, *, case_key: str, run_index: int
) -> dict:
    """The run_config a matrix-cell child carries (benchmark path + overrides)."""
    rc: dict = {
        "benchmark_mode": True,
        "experiment": {
            "id": str(exp.id),
            "config_key": cfg["config_key"],
            "case_key": case_key,
            "run_index": run_index,
        },
    }
    if not cfg.get("orchestrator") and cfg.get("template_id"):
        rc["template_id"] = str(cfg["template_id"])
    for key in ("model_id", "temperature", "seed", "soul_md", "tools_override", "memory_mode"):
        if cfg.get(key) is not None:
            rc[key] = cfg[key]
    return rc


async def _make_child(
    db: AsyncSession, exp: Experiment, run_row: ExperimentRun, cfg: dict, case: dict
) -> Task:
    """Create the READY child task for one matrix cell run.

    The task input is EXACTLY the frozen case (no suffixes — a per-config
    marker in the prompt would confound the A/B); identification lives in
    run_config.experiment and the benchmark_* tags.
    """
    pinned = (
        uuid.UUID(str(cfg["template_id"]))
        if (not cfg.get("orchestrator") and cfg.get("template_id"))
        else None
    )
    child = Task(
        title=case["title"][:500],
        description=case.get("description"),
        status=TaskStatus.READY.value,
        workspace_id=exp.workspace_id,
        origin="experiment",
        template_id=pinned,
        run_config=child_run_config(
            exp, cfg, case_key=run_row.case_key, run_index=run_row.run_index
        ),
        max_retries=0,
        reference_answer=case.get("reference_answer"),
        canonical_trajectory=case.get("canonical_trajectory"),
        capability_spec=case.get("capability_spec"),
        benchmark_case_id=run_row.case_key,
        benchmark_suite=f"exp:{exp.id}",
    )
    db.add(child)
    await db.flush()
    return child


async def _evaluate_child(db: AsyncSession, task: Task, eval_config: dict) -> None:
    """Best-effort record + evals for a terminal child, honoring eval_config.

    E-02 outcome scoring always runs (eval is the point of a benchmark run);
    E-07 trajectory defaults on, E-14 failure modes defaults off. E-20 is
    captured inside build_quality_record. Never raises.
    """
    from app.quality.data_lake import build_quality_record
    from app.quality.judge import evaluate_task_quality
    from app.quality.trajectory import evaluate_task_trajectory

    try:
        await build_quality_record(db, task, commit=True)
    except Exception as e:
        await db.rollback()
        logger.warning(f"experiment: record build failed for {task.id}: {e}")
        return
    rec = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if rec is None:
        return
    if task.status in _SUCCESS_TASK and rec.quality_profile is None:
        try:
            await evaluate_task_quality(db, task, commit=True)
        except Exception as e:
            await db.rollback()
            logger.warning(f"experiment: outcome eval failed for {task.id}: {e}")
    if (
        task.status in _SUCCESS_TASK
        and (eval_config or {}).get("trajectory", True)
        and rec.trajectory_profile is None
    ):
        try:
            await evaluate_task_trajectory(db, task, commit=True)
        except Exception as e:
            await db.rollback()
            logger.warning(f"experiment: trajectory eval failed for {task.id}: {e}")
    if (eval_config or {}).get("failure_modes") and rec.failure_profile is None:
        from app.quality.failure_modes import evaluate_task_failure_modes

        try:
            await evaluate_task_failure_modes(db, task, commit=True)
        except Exception as e:
            await db.rollback()
            logger.warning(f"experiment: failure-mode eval failed for {task.id}: {e}")


def _run_cost(task: Task, rec: QualityRecord | None) -> Decimal:
    total = Decimal(task.cost_usd or 0)
    if rec is not None:
        for prof in (rec.quality_profile, rec.trajectory_profile, rec.failure_profile):
            if prof:
                total += Decimal(str(prof.get("judge_cost_usd") or 0))
    return total


def _run_duration(task: Task, rec: QualityRecord | None) -> Optional[int]:
    if rec is not None and rec.duration_seconds is not None:
        return rec.duration_seconds
    if task.started_at and task.completed_at:
        return int((task.completed_at - task.started_at).total_seconds())
    return None


async def advance_experiment(db: AsyncSession, exp: Experiment) -> None:
    """One idempotent tick step: settle finished runs, claim pending cells
    under the parallelism/budget limits, finalize when everything settled."""
    if exp.status != ExperimentStatus.RUNNING.value:
        return

    rows = (
        await db.execute(
            select(ExperimentRun)
            .where(ExperimentRun.experiment_id == exp.id)
            .order_by(
                ExperimentRun.config_key,
                ExperimentRun.case_key,
                ExperimentRun.run_index,
            )
        )
    ).scalars().all()
    configs = {c["config_key"]: c for c in exp.configurations}
    cases = {c["case_key"]: c for c in exp.dataset_cases}

    running = [r for r in rows if r.status == ExperimentRunStatus.RUNNING.value]
    task_ids = [r.task_id for r in running if r.task_id]
    tasks: dict[uuid.UUID, Task] = {}
    if task_ids:
        loaded = (
            await db.execute(select(Task).where(Task.id.in_(task_ids)))
        ).scalars().all()
        tasks = {t.id: t for t in loaded}

    # 1) Settle runs whose task reached a terminal state (evaluate + denormalize).
    now = datetime.utcnow()
    for r in running:
        task = tasks.get(r.task_id)
        if task is None:
            r.status = ExperimentRunStatus.FAILED.value
            r.completed_at = now
            continue
        if task.status not in _TERMINAL_TASK:
            continue
        await _evaluate_child(db, task, exp.eval_config or {})
        rec = (
            await db.execute(
                select(QualityRecord).where(QualityRecord.task_id == task.id)
            )
        ).scalar_one_or_none()
        r.status = (
            ExperimentRunStatus.SUCCESS.value
            if task.status in _SUCCESS_TASK
            else ExperimentRunStatus.FAILED.value
        )
        r.cost_usd = _run_cost(task, rec)
        r.duration_seconds = _run_duration(task, rec)
        if rec is not None:
            r.weighted_score = (rec.quality_profile or {}).get("weighted_score")
            r.trajectory_score = (rec.trajectory_profile or {}).get("overall_score")
        r.completed_at = datetime.utcnow()

    # 2) Accumulated cost: settled rows (denormalized) + in-flight agent spend.
    total = Decimal("0")
    for r in rows:
        total += Decimal(r.cost_usd or 0)
    for r in running:
        task = tasks.get(r.task_id)
        if task is not None and r.status == ExperimentRunStatus.RUNNING.value:
            total += Decimal(task.cost_usd or 0)
    exp.accumulated_cost_usd = total
    budget_hit = (
        exp.budget_limit_usd is not None and total >= exp.budget_limit_usd
    )

    pending = [r for r in rows if r.status == ExperimentRunStatus.PENDING.value]
    still_running = [
        r for r in rows if r.status == ExperimentRunStatus.RUNNING.value
    ]

    # 3) Claim the next pending cells while under the limits.
    if pending and not budget_hit:
        target = await inflight_target(db, parallel=True)
        if exp.max_parallel:
            target = min(target, exp.max_parallel)
        slots = max(0, target - len(still_running))
        claimed = 0
        for r in pending[:slots]:
            cfg = configs.get(r.config_key)
            case = cases.get(r.case_key)
            if cfg is None or case is None:  # defensive; cells are pre-validated
                r.status = ExperimentRunStatus.SKIPPED.value
                r.completed_at = datetime.utcnow()
                continue
            child = await _make_child(db, exp, r, cfg, case)
            r.task_id = child.id
            r.status = ExperimentRunStatus.RUNNING.value
            claimed += 1
        await db.commit()
        if claimed:
            return  # let them run; settle/finalize on a later tick

    # 4) Budget reached → skip everything not yet claimed (partial results kept).
    if pending and budget_hit:
        now = datetime.utcnow()
        for r in pending:
            r.status = ExperimentRunStatus.SKIPPED.value
            r.completed_at = now

    # 5) Finalize once nothing is pending or in flight.
    if not still_running and all(
        r.status != ExperimentRunStatus.PENDING.value for r in rows
    ):
        skipped = any(
            r.status == ExperimentRunStatus.SKIPPED.value for r in rows
        )
        succeeded = any(
            r.status == ExperimentRunStatus.SUCCESS.value for r in rows
        )
        if skipped:
            exp.status = ExperimentStatus.CAPPED.value
        elif succeeded:
            exp.status = ExperimentStatus.COMPLETED.value
        else:
            exp.status = ExperimentStatus.FAILED.value
            exp.error = "no run succeeded"
        exp.completed_at = datetime.utcnow()
    await db.commit()


async def advance_active_experiments(db: AsyncSession) -> int:
    """Advance every running experiment; used by the scheduler tick."""
    rows = (
        await db.execute(
            select(Experiment).where(
                Experiment.status == ExperimentStatus.RUNNING.value
            )
        )
    ).scalars().all()
    advanced = 0
    for exp in rows:
        try:
            await advance_experiment(db, exp)
            advanced += 1
        except Exception as e:
            await db.rollback()
            logger.warning(f"experiment: advance failed for {exp.id}: {e}")
    return advanced


async def estimate_preview(
    db: AsyncSession, *, workspace_id: uuid.UUID, payload: dict
) -> dict:
    """Total runs + cost/time estimate for a (not yet created) experiment."""
    from sqlalchemy import func as sa_func

    configs = expand_matrix(payload.get("configurations"), payload.get("axes"))
    dataset_spec = payload.get("dataset") or {}
    if dataset_spec.get("source") == "tasks":
        n_cases = len(dataset_spec.get("task_ids") or [])
    elif dataset_spec.get("source") == "upload":
        n_cases = len(dataset_spec.get("cases") or [])
    else:
        n_cases = len(cases_from_suite(dataset_spec.get("suite", ""), dataset_spec.get("case_ids"))) if dataset_spec.get("suite") else 0
    n_runs = int(payload.get("n_runs_per_cell") or 1)
    total_runs = len(configs) * n_cases * n_runs

    warnings: list[str] = []
    est_cost = 0.0
    est_serial_seconds = 0.0
    used_fallback = False
    for cfg in configs:
        query = select(
            sa_func.avg(QualityRecord.cost_usd),
            sa_func.avg(QualityRecord.duration_seconds),
        ).where(QualityRecord.workspace_id == workspace_id)
        if cfg.get("template_id"):
            query = query.where(
                QualityRecord.template_id == uuid.UUID(str(cfg["template_id"]))
            )
        avg_cost, avg_duration = (await db.execute(query)).one()
        if avg_cost is None:
            row = (
                await db.execute(
                    select(
                        sa_func.avg(QualityRecord.cost_usd),
                        sa_func.avg(QualityRecord.duration_seconds),
                    ).where(QualityRecord.workspace_id == workspace_id)
                )
            ).one()
            avg_cost, avg_duration = row
        if avg_cost is None:
            used_fallback = True
            avg_cost, avg_duration = DEFAULT_RUN_COST_USD, DEFAULT_RUN_DURATION_S
        per_config_runs = n_cases * n_runs
        est_cost += float(avg_cost) * per_config_runs
        est_serial_seconds += float(avg_duration or DEFAULT_RUN_DURATION_S) * per_config_runs

    parallelism = await inflight_target(db, parallel=True)
    if payload.get("max_parallel"):
        parallelism = min(parallelism, int(payload["max_parallel"]))
    est_minutes = (est_serial_seconds / max(1, parallelism)) / 60.0

    if used_fallback:
        warnings.append(
            "no historical runs to estimate from — using default cost/duration"
        )
    budget = payload.get("budget_limit_usd")
    if budget is not None and est_cost > float(budget):
        warnings.append(
            f"estimated cost ${est_cost:.2f} exceeds budget ${float(budget):.2f} — "
            "the experiment will be capped with partial results"
        )
    if any(c.get("temperature") is not None for c in configs):
        warnings.append(
            "temperature axis requires an agent image built from this revision "
            "(LLM_TEMPERATURE support)"
        )

    return {
        "n_configs": len(configs),
        "n_cases": n_cases,
        "n_runs_per_cell": n_runs,
        "total_runs": total_runs,
        "est_cost_usd": round(est_cost, 4),
        "est_duration_minutes": round(est_minutes, 1),
        "warnings": warnings,
    }


async def clone_experiment(
    db: AsyncSession,
    exp: Experiment,
    *,
    name: Optional[str] = None,
    changes: Optional[dict] = None,
    created_by: str = "user",
) -> Experiment:
    """New draft from an existing experiment, with optional field overrides.

    ``changes`` is a partial create payload; untouched parts (including the
    frozen dataset) are copied from the source. Re-run = clone + run.
    """
    changes = dict(changes or {})
    payload: dict = {
        "name": name
        or changes.pop("name", None)
        or f"{exp.name} (copy-{uuid.uuid4().hex[:4]})",
        "description": changes.pop("description", exp.description),
        "configurations": changes.pop(
            "configurations", (exp.matrix_spec or {}).get("configurations") or []
        ),
        "axes": changes.pop("axes", (exp.matrix_spec or {}).get("axes")),
        "n_runs_per_cell": changes.pop("n_runs_per_cell", exp.n_runs_per_cell),
        "budget_limit_usd": changes.pop(
            "budget_limit_usd",
            float(exp.budget_limit_usd) if exp.budget_limit_usd is not None else None,
        ),
        "max_parallel": changes.pop("max_parallel", exp.max_parallel),
        "eval_config": changes.pop("eval_config", exp.eval_config or {}),
    }
    new_dataset = changes.pop("dataset", None)
    if changes:
        raise ValueError(f"unknown clone changes: {sorted(changes)}")

    if new_dataset is not None:
        payload["dataset"] = new_dataset
        return await create_experiment(
            db, workspace_id=exp.workspace_id, payload=payload, created_by=created_by
        )

    # Same dataset: copy the frozen cases verbatim instead of re-normalizing
    # (an upload source can't be re-normalized — raw cases aren't stored).
    payload["dataset"] = exp.dataset
    return await create_experiment(
        db,
        workspace_id=exp.workspace_id,
        payload=payload,
        created_by=created_by,
        frozen_cases=exp.dataset_cases,
    )
