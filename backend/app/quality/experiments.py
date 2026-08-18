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
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.experiment import (
    Experiment,
    ExperimentAttempt,
    ExperimentRun,
    ExperimentRunStatus,
    ExperimentStatus,
)
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.registry_entry import RegistryEntry
from app.models.task import Task, TaskStatus
from app.models.template import Template
from app.quality import external_eval as ext_eval
from app.quality.benchmark import _capability_spec_for, load_cases
from app.quality.runs_common import (
    SUCCESS_TASK as _SUCCESS_TASK,
    TERMINAL_TASK as _TERMINAL_TASK,
    inflight_target,
)
from app.utils.events import log_event
from app.utils.failures import FAILURE_INFRA, merge_failure_type

logger = logging.getLogger(__name__)

# Toolathlon executable-eval cases (gold.external_eval) run on a dedicated image
# with the case's MCP servers force-enabled, and a higher iteration ceiling.
TOOLATHLON_AGENT_IMAGE = "spawnhive-agent-toolathlon:latest"
# The image a plain (non-executable-eval) case runs on; mirrors
# app/orchestrator/docker_manager.AGENT_IMAGE, imported lazily there to keep the
# docker dependency out of this module's import path.
DEFAULT_AGENT_IMAGE = "spawnhive-agent:latest"

# The two halves of "what is still part of this experiment" (SPA-84). Retiring a
# configuration keeps its lineage but takes it out of the matrix, and every
# reader has to agree on that — or the report and the aggregates end up
# describing different populations again, which is the bug this whole change
# exists to end. Defined once and used by the runner, the report, the API,
# analytics and the CLI, so nobody has to half-remember the rule.
LIVE_CELL = ExperimentRun.retired_at.is_(None)


def live_configs(exp: Experiment) -> list[dict]:
    """Configurations still in the matrix — retired entries kept but excluded."""
    return [c for c in (exp.configurations or []) if not c.get("retired_at")]
TOOLATHLON_MAX_ITERATIONS = 150
# A preprocess still running after this many seconds is a kept-alive mock server
# (the agent runs against it); we proceed and remove it at the eval settle.
PREPROCESS_MOCK_GRACE_S = 180

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
# SPA-69: upper bound on parallel Toolathlon PG lanes — matches the number of
# ``toolathlon_pg_lane_<i>`` containers provisioned in docker-compose (profile
# "toolathlon-lanes"). Asking for more lanes than containers would pin a run to a
# non-existent host, so reject it at create time.
MAX_TOOLATHLON_LANES = 4


# --- configuration matrix ---------------------------------------------------


def _config_fingerprint(cfg: dict) -> str:
    """Canonical-JSON fingerprint over the variation axes (dedup identity)."""
    canon = {k: cfg.get(k) for k in CONFIG_AXES if cfg.get(k) is not None}
    canon["orchestrator"] = bool(cfg.get("orchestrator"))
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def experiment_input_fingerprint(exp: Experiment) -> str:
    """Canonical-JSON fingerprint over everything a report is computed from (SPA-84).

    Deliberately covers the *inputs* only — configurations, the frozen case
    keys, repetitions and evaluation settings — not the results. Results change
    as an experiment runs, and a report of a running experiment is never cached;
    what must never change silently is the shape of the thing being measured.

    Retired configurations are included with their ``retired_at`` stamp, so
    retiring one changes the fingerprint even though the entry is kept.
    """
    configs = [
        {
            "config_key": c.get("config_key"),
            "fingerprint": c.get("fingerprint"),
            "retired_at": c.get("retired_at"),
        }
        for c in sorted(
            exp.configurations or [], key=lambda c: str(c.get("config_key") or "")
        )
    ]
    canon = {
        "configurations": configs,
        "case_keys": sorted(
            str(c.get("case_key")) for c in (exp.dataset_cases or []) if c.get("case_key")
        ),
        "n_runs_per_cell": exp.n_runs_per_cell,
        "eval_config": exp.eval_config or {},
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


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


class UploadRubricDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    name: Optional[str] = Field(default=None, max_length=255)
    description: Optional[str] = None
    evaluator: str = Field(default="judge", pattern="^(judge|reference|objective|human)$")
    weight: float = Field(gt=0)
    threshold: Optional[int] = Field(default=None, ge=0, le=10)
    critical: bool = False
    reference_mode: Optional[str] = None
    probe: Optional[str] = None


class UploadRubric(BaseModel):
    """Inline per-case rubric: overrides the template/workspace rubric when the
    case is judged (mixed datasets get the right dimensions per case)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, max_length=255)
    dimensions: list[UploadRubricDimension] = Field(min_length=1, max_length=20)


class UploadCase(BaseModel):
    """One custom-uploaded case (a parsed JSONL line)."""

    model_config = ConfigDict(extra="forbid")

    task_input: UploadCaseInput
    case_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reference_answer: Optional[str] = None
    rubric: Optional[UploadRubric] = None
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
                rubric=case.rubric.model_dump(exclude_none=True) if case.rubric else None,
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
            # Toolathlon executable eval: the runner needs the commands, the
            # required services (drives sequential execution + the eval image)
            # and meta.task_path (the gym dir for preprocess/eval). Plain suites
            # leave these None, so _frozen_case drops them.
            external_eval=c.gold.external_eval.model_dump() if c.gold.external_eval else None,
            environment=c.environment.model_dump() if c.environment else None,
            meta=dict(c.meta) if c.meta else None,
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
    exclude_fingerprints: Optional[set[str]] = None,
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

    _validate_eval_config(payload.get("eval_config"))
    eval_config = _with_judge_threshold(payload.get("eval_config"))

    configs = expand_matrix(payload.get("configurations"), payload.get("axes"))
    if exclude_fingerprints:
        # Clone path: a configuration retired in the source must not come back.
        # Retiring drops it from matrix_spec.configurations, but an axes-defined
        # matrix regenerates it from the cartesian product, so the exclusion has
        # to happen after expansion (SPA-84).
        configs = [c for c in configs if c.get("fingerprint") not in exclude_fingerprints]
        if not configs:
            raise ValueError("every configuration of the source experiment is retired")
        for i, c in enumerate(configs, start=1):
            c["config_key"] = f"cfg-{i:02d}"
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
    n_lanes = payload.get("n_toolathlon_lanes")
    if n_lanes is not None and int(n_lanes) < 1:
        raise ValueError("n_toolathlon_lanes must be >= 1")
    if n_lanes is not None and int(n_lanes) > MAX_TOOLATHLON_LANES:
        raise ValueError(
            f"n_toolathlon_lanes must be <= {MAX_TOOLATHLON_LANES} "
            f"(only {MAX_TOOLATHLON_LANES} lane containers are provisioned)"
        )
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
        n_toolathlon_lanes=int(n_lanes) if n_lanes is not None else None,
        eval_config=eval_config,
        created_by=created_by,
    )
    # Record the inputs from the start, so the column always means "the inputs as
    # of the last write" rather than being empty until the first mutation.
    exp.input_fingerprint = experiment_input_fingerprint(exp)
    db.add(exp)
    await db.commit()
    await db.refresh(exp)
    return exp


async def start_experiment(db: AsyncSession, exp: Experiment) -> None:
    """draft → running: materialize every matrix cell as a pending run row."""
    if exp.status != ExperimentStatus.DRAFT.value:
        raise ValueError(f"cannot run experiment in status '{exp.status}'")
    images = _agent_image_ids()
    resolved_configs = []
    for cfg in exp.configurations:
        # A config retired while the experiment was still a draft has no cells
        # and no lineage; materializing it here would silently run a condition
        # the author had already taken out of the matrix.
        if cfg.get("retired_at"):
            resolved_configs.append(cfg)
            continue
        resolved_configs.append(
            {**cfg, "resolved": await _resolve_config_state(db, cfg, images)}
        )
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
    # Reassign: the JSONB column is not mutation-tracked, so an in-place edit
    # of a dict inside the list is silently dropped.
    exp.configurations = resolved_configs
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
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == exp.id,
                # A retired config's cells were already settled and archived when
                # it was retired; rewriting them now would overwrite the very
                # lineage retirement froze.
                LIVE_CELL,
            )
        )
    ).scalars().all()
    now = datetime.utcnow()
    inflight_ids = [
        r.task_id
        for r in rows
        if r.status in (
            ExperimentRunStatus.RUNNING.value,
            ExperimentRunStatus.EVALUATING.value,
        )
        and r.task_id
    ]
    for r in rows:
        if r.status in (
            ExperimentRunStatus.PENDING.value,
            ExperimentRunStatus.PREPROCESSING.value,
            ExperimentRunStatus.RUNNING.value,
            ExperimentRunStatus.EVALUATING.value,
        ):
            # Best-effort cleanup of any Toolathlon preprocess/eval containers.
            ext_eval.remove(r.preprocess_container_id)
            ext_eval.remove(r.eval_container_id)
            r.preprocess_container_id = None
            r.eval_container_id = None
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


# Suffix moving a superseded child task out of the experiment's plain
# ``exp:<id>`` population (SPA-84). Every reader filters the suite by exact
# equality, so the suffix quietly narrows ``exp:<id>`` to what the report counts
# while leaving the retired lineage addressable under its own tag.
RETIRED_SUITE_SUFFIX = ":retired"

# Template fields that change what a configuration MEANS when edited. Mirrors
# _full_template_snapshot in app/api/templates.py; hashed rather than versioned
# because a TemplateVersion row is a snapshot of the state BEFORE the edit that
# created it, and a template that was never edited has no version rows at all.
_TEMPLATE_IDENTITY_FIELDS = (
    "soul_md",
    "model_id",
    "rubric_id",
    "tool_ids",
    "max_ram",
    "max_cpu",
    "timeout_minutes",
)


def _template_content_hash(tpl) -> str:
    canon = {f: getattr(tpl, f, None) for f in _TEMPLATE_IDENTITY_FIELDS}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# Image ids change only when somebody rebuilds an image, but they are now read on
# every report fetch (drift is recomputed per read so it cannot go stale in the
# cache). Memoize briefly so a polled report page does not hit the docker socket
# once per poll — docker-py is synchronous and this runs on the API event loop.
_IMAGE_ID_TTL_SECONDS = 60.0
_image_id_cache: tuple[float, dict] | None = None


def _agent_image_ids() -> dict:
    """Image ids of both agent images, best effort.

    Which image a run uses is a property of the CASE, not the configuration — a
    mixed dataset uses both within one config — so both are recorded rather than
    one being pinned. Rebuilding these images has moved measured pass rates
    before, and nothing in the database recorded which build produced which run.
    """
    global _image_id_cache
    now = time.monotonic()
    if _image_id_cache and now - _image_id_cache[0] < _IMAGE_ID_TTL_SECONDS:
        return _image_id_cache[1]
    out: dict = {}
    try:
        import docker

        client = docker.from_env()
        for name in (DEFAULT_AGENT_IMAGE, TOOLATHLON_AGENT_IMAGE):
            try:
                out[name] = client.images.get(name).id
            except Exception:
                out[name] = None
    except Exception as e:  # docker socket unavailable — pinning is best effort
        logger.warning(f"experiment: agent image ids unavailable ({e})")
    _image_id_cache = (now, out)
    return out


async def _resolve_config_state(db: AsyncSession, cfg: dict, images: dict) -> dict:
    """Freeze what a configuration actually resolves to right now (SPA-84).

    The fingerprint covers ids and overrides; it cannot see that the template
    behind ``template_id`` was edited, or that the model behind ``model_id`` was
    repointed at a different vendor. Both have happened in this project's own
    history — a template's model was swapped mid-experiment — and were invisible
    afterwards. This records the resolution so the drift becomes visible instead.

    Never store secrets here: ``GET /api/experiments/{id}`` returns
    ``configurations`` verbatim and is not role-gated.
    """
    resolved: dict = {"resolved_at": datetime.utcnow().isoformat(), "agent_images": images}
    tpl = None
    if cfg.get("template_id"):
        try:
            tpl = await db.get(Template, uuid.UUID(str(cfg["template_id"])))
        except (ValueError, TypeError):
            tpl = None
    if tpl is not None:
        resolved["template_name"] = tpl.name
        resolved["template_content_sha256"] = _template_content_hash(tpl)
    # The effective model is the config's, else the template's — a config that
    # omits model_id inherits it, which is exactly the case that drifted here.
    model_id = cfg.get("model_id") or (getattr(tpl, "model_id", None) if tpl else None)
    if model_id:
        try:
            model = await db.get(LLMModel, uuid.UUID(str(model_id)))
        except (ValueError, TypeError):
            model = None
        if model is not None:
            resolved["model_api_name"] = model.api_name
            resolved["model_display_name"] = model.display_name
            provider = await db.get(Provider, model.provider_id)
            if provider is not None:
                resolved["provider_name"] = provider.name
    return resolved


async def _retire_task_tag(db: AsyncSession, task_id: Optional[uuid.UUID]) -> None:
    """Move a superseded run's task and quality record to the retired suite tag.

    Retrying a cell used to leave its old task tagged as part of the experiment,
    which is how an experiment ended up with three times more tagged tasks than
    run rows — the report counted run rows, the suite aggregators counted tags,
    and the two described different populations. The record carries its own copy
    of the tag (denormalized at insert and never re-synced), so both must move.
    """
    if task_id is None:
        return
    task = await db.get(Task, task_id)
    if task is None or not task.benchmark_suite:
        return
    if task.benchmark_suite.endswith(RETIRED_SUITE_SUFFIX):
        return
    task.benchmark_suite = f"{task.benchmark_suite}{RETIRED_SUITE_SUFFIX}"
    for rec in (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task_id))
    ).scalars().all():
        if rec.benchmark_suite and not rec.benchmark_suite.endswith(RETIRED_SUITE_SUFFIX):
            rec.benchmark_suite = f"{rec.benchmark_suite}{RETIRED_SUITE_SUFFIX}"


async def _lock_experiment(db: AsyncSession, exp: Experiment) -> None:
    """Serialize mutations of one experiment (SPA-84).

    All three mutations read ``configurations``, derive the next state from it
    and bump ``revision``. Without a lock two concurrent calls both read the old
    list, both append, and the second write wins — losing a configuration while
    its cells are already materialized, or advancing the revision once for two
    changes. Taken before anything is read, released by the caller's commit.
    """
    await db.execute(select(Experiment.id).where(Experiment.id == exp.id).with_for_update())
    await db.refresh(exp)


async def _bump_revision(
    db: AsyncSession, exp: Experiment, action: str, detail: dict
) -> None:
    """Record that the experiment's inputs changed (SPA-84).

    Increments the revision, refreshes the input fingerprint, drops the cached
    report and writes an audit event. Every mutation goes through here, which is
    what lets a cached report be matched against the input it was built from
    instead of being trusted because it merely exists.

    Does not commit — the caller owns the transaction, so the bump and the
    mutation it describes land together or not at all.
    """
    exp.revision = (exp.revision or 1) + 1
    exp.input_fingerprint = experiment_input_fingerprint(exp)
    exp.report = None
    await log_event(
        db,
        "experiment_mutated",
        "system",
        {
            "experiment_id": str(exp.id),
            "action": action,
            "revision": exp.revision,
            "input_fingerprint": exp.input_fingerprint,
            **detail,
        },
        workspace_id=exp.workspace_id,
        commit=False,
    )


async def _snapshot_attempt(db: AsyncSession, run: ExperimentRun, reason: str) -> None:
    """Copy a cell's current state into the attempt ledger before it is cleared.

    Called for cells that actually ran — a never-claimed ``pending`` cell has
    nothing worth preserving. The scores are denormalized rather than referenced
    because the task they came from may be deleted later, and because a capped
    run reports ``failed`` while still carrying a real evaluation: overwriting it
    used to destroy the observation.

    Idempotent per execution. ``attempt_count`` only advances when the tick
    claims the cell, so two archiving events can land on the same execution — a
    retry followed by a retirement, say — and the ledger is the authority on
    which indices are taken. Without this the second one violates
    ``uq_experiment_attempts_cell`` and takes the whole transaction down.
    """
    # 0 means the cell was never claimed. Rows that predate the counter are
    # covered by the migration's backfill, so the counter alone is enough here.
    if not run.attempt_count:
        return
    index = run.attempt_count
    already = await db.scalar(
        select(ExperimentAttempt.id).where(
            ExperimentAttempt.experiment_run_id == run.id,
            ExperimentAttempt.attempt_index == index,
        )
    )
    if already:
        return
    db.add(
        ExperimentAttempt(
            experiment_run_id=run.id,
            attempt_index=index,
            task_id=run.task_id,
            status=run.status,
            cost_usd=run.cost_usd or Decimal(0),
            weighted_score=run.weighted_score,
            trajectory_score=run.trajectory_score,
            duration_seconds=run.duration_seconds,
            failure_type=run.failure_type,
            external_verdict=run.external_verdict,
            launch_time=run.launch_time,
            lane_index=run.lane_index,
            condition_fingerprint=run.condition_fingerprint,
            core_condition_fingerprint=run.core_condition_fingerprint,
            retired_reason=reason,
            completed_at=run.completed_at,
        )
    )


async def retry_failed_experiment(db: AsyncSession, exp: Experiment) -> int:
    """Reset failed cells back to ``pending`` and re-open the experiment so the
    tick re-runs them in place (no clone). Only cells that ERRORED OUT
    (``status=failed`` — provider rate-limit / transient API / preprocess or eval
    infra failure) are retried; genuine results stay put, since a model that
    finished but flunked the checker is ``status=success`` with
    ``external_verdict=False``. Idempotent and repeatable: press again to re-run
    whatever is still failed after a provider quota window resets.

    The superseded state of every retried cell is written to the attempt ledger
    first, so "re-run in place" no longer means the previous result is lost.
    """
    await _lock_experiment(db, exp)
    if exp.status not in TERMINAL_EXPERIMENT and exp.status != ExperimentStatus.PAUSED.value:
        raise ValueError(
            f"cannot retry a {exp.status} experiment; pause or let it settle first"
        )
    rows = (
        await db.execute(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == exp.id,
                # A retired config's cells are out of the matrix and are never
                # re-claimed by the tick, so re-running them would resurrect a
                # condition the author deliberately retired.
                LIVE_CELL,
            )
        )
    ).scalars().all()
    retried = 0
    for r in rows:
        if r.status != ExperimentRunStatus.FAILED.value:
            continue
        # Best-effort cleanup of any lingering Toolathlon preprocess/eval containers.
        ext_eval.remove(r.preprocess_container_id)
        ext_eval.remove(r.eval_container_id)
        await _snapshot_attempt(db, r, "retry")
        # The ledger keeps the pointer; the task itself leaves the live
        # population so tag-based aggregation matches the report.
        await _retire_task_tag(db, r.task_id)
        r.task_id = None
        r.status = ExperimentRunStatus.PENDING.value
        r.cost_usd = Decimal(0)
        r.weighted_score = None
        r.trajectory_score = None
        r.duration_seconds = None
        r.failure_type = None
        r.external_verdict = None
        r.launch_time = None
        r.preprocess_container_id = None
        r.eval_container_id = None
        r.preprocess_retried = None
        r.preprocess_started_at = None
        r.preprocess_log = None
        r.eval_log = None
        r.completed_at = None
        # Stale pin: the row claimed a lane it no longer holds. _first_free_lane
        # reassigns at claim time, but until then the row misreports occupancy.
        r.lane_index = None
        retried += 1
    if retried:
        exp.status = ExperimentStatus.RUNNING.value
        exp.completed_at = None
        exp.error = None
        await _bump_revision(db, exp, "retry_failed", {"cells_retried": retried})
    await db.commit()
    return retried


async def add_config_to_experiment(db: AsyncSession, exp: Experiment, cfg_input: dict) -> dict:
    """Append a new configuration (e.g. another model) to an existing experiment
    and materialize its cells (config × all cases × n_runs_per_cell) as pending,
    re-opening the experiment so the tick runs them. Lets you add a model in
    place instead of starting a fresh experiment. Frozen dataset is reused as-is.
    """
    await _lock_experiment(db, exp)
    if exp.status == ExperimentStatus.DRAFT.value:
        raise ValueError("add the configuration at creation, or run the experiment first")
    canon = {k: cfg_input.get(k) for k in CONFIG_AXES if cfg_input.get(k) is not None}
    canon["orchestrator"] = bool(cfg_input.get("orchestrator"))
    errs = _config_errors(canon)
    if errs:
        raise ValueError("; ".join(errs))
    # create_experiment validates these; the add path used to skip it, so a
    # config could name a template or model that does not exist in the workspace.
    await _validate_config_refs(db, exp.workspace_id, [canon])
    fp = _config_fingerprint(canon)
    existing = list(exp.configurations or [])
    live = live_configs(exp)
    if any(c.get("fingerprint") == fp for c in live):
        raise ValueError("a configuration with these settings already exists in this experiment")
    if len(live) >= MAX_CONFIGS:
        raise ValueError(f"too many configurations: {len(live)} >= {MAX_CONFIGS}")
    # Count retired configs too: a key must never be reused, or the attempt
    # ledger and the audit trail would silently merge two different conditions.
    nums = [
        int(str(c.get("config_key", "")).split("-")[1])
        for c in existing
        if str(c.get("config_key", "")).startswith("cfg-")
    ]
    cfg = dict(canon)
    cfg["fingerprint"] = fp
    cfg["label"] = cfg_input.get("label") or _config_label(canon)
    cfg["config_key"] = f"cfg-{(max(nums) + 1) if nums else 1:02d}"
    # A config added after the start still gets its resolution frozen, or it
    # would be the one condition in the matrix with no record of what it meant.
    cfg["resolved"] = await _resolve_config_state(db, cfg, _agent_image_ids())
    exp.configurations = existing + [cfg]  # reassign so the JSONB column is marked dirty
    ms = dict(exp.matrix_spec or {})
    ms["configurations"] = list(ms.get("configurations") or []) + [cfg_input]
    exp.matrix_spec = ms
    created = 0
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
            created += 1
    if exp.status in TERMINAL_EXPERIMENT:
        exp.status = ExperimentStatus.RUNNING.value
        exp.completed_at = None
        exp.error = None
    await _bump_revision(
        db, exp, "add_config", {"config_key": cfg["config_key"], "runs_created": created}
    )
    await db.commit()
    return {"config_key": cfg["config_key"], "label": cfg["label"], "runs_created": created}


async def _resettle_after_retirement(db: AsyncSession, exp: Experiment) -> None:
    """Bring the experiment's own totals back onto the live population (SPA-84).

    Cost and terminal status are rolled up by the tick, which returns early
    unless the experiment is running — so retiring a configuration on a settled
    experiment left ``accumulated_cost_usd`` carrying the retired config's spend
    and the status reflecting runs that are no longer counted. Every other view
    had already moved to the live population, which is exactly the split this
    change exists to close.
    """
    rows = (
        await db.execute(
            select(ExperimentRun).where(ExperimentRun.experiment_id == exp.id, LIVE_CELL)
        )
    ).scalars().all()
    # Mirror the tick's roll-up: settled cells carry their cost denormalized,
    # in-flight ones still hold it on the task. Counting only the settled half
    # would make a paused experiment's spend dip until the next tick.
    total = sum((Decimal(r.cost_usd or 0) for r in rows), Decimal("0"))
    inflight_task_ids = [r.task_id for r in rows if r.status in _INFLIGHT_RUN and r.task_id]
    if inflight_task_ids:
        for task in (
            await db.execute(select(Task).where(Task.id.in_(inflight_task_ids)))
        ).scalars().all():
            total += Decimal(task.cost_usd or 0)
    exp.accumulated_cost_usd = total

    # Only re-derive a terminal verdict; a paused experiment keeps its status,
    # and the tick owns the running one.
    if exp.status not in TERMINAL_EXPERIMENT or any(
        r.status in _INFLIGHT_RUN or r.status == ExperimentRunStatus.PENDING.value
        for r in rows
    ):
        return
    if any(r.status == ExperimentRunStatus.SKIPPED.value for r in rows):
        exp.status = ExperimentStatus.CAPPED.value
    elif any(r.status == ExperimentRunStatus.SUCCESS.value for r in rows):
        exp.status = ExperimentStatus.COMPLETED.value
        exp.error = None
    elif rows:
        exp.status = ExperimentStatus.FAILED.value
        exp.error = "no run succeeded"


async def remove_config_from_experiment(
    db: AsyncSession, exp: Experiment, config_key: str
) -> dict:
    """Retire a configuration — the inverse of :func:`add_config_to_experiment`.

    Stamps ``retired_at`` on the config entry and on its ExperimentRun rows,
    tears down any in-flight preprocess/eval/agent containers, and bumps the
    revision so the report re-assembles without it. The lineage is deliberately
    **kept**: this used to hard-delete the rows, which is how an experiment ended
    up with more ``exp:<id>``-tagged tasks than it had runs, and left no way to
    tell what had been measured before. Refuses to retire the last live
    configuration — delete the experiment instead.
    """
    await _lock_experiment(db, exp)
    if exp.status == ExperimentStatus.RUNNING.value:
        raise ValueError("pause or cancel the experiment before retiring a configuration")
    existing = list(exp.configurations or [])
    target = next((c for c in existing if c.get("config_key") == config_key), None)
    if target is None:
        raise ValueError(f"no configuration '{config_key}' in this experiment")
    if target.get("retired_at"):
        raise ValueError(f"configuration '{config_key}' is already retired")
    live = live_configs(exp)
    if len(live) <= 1:
        raise ValueError("cannot retire the only configuration; delete the experiment instead")

    rows = (
        await db.execute(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == exp.id,
                ExperimentRun.config_key == config_key,
            )
        )
    ).scalars().all()
    inflight_task_ids = [
        r.task_id
        for r in rows
        if r.status in (
            ExperimentRunStatus.RUNNING.value,
            ExperimentRunStatus.EVALUATING.value,
        )
        and r.task_id
    ]
    retired_at = datetime.utcnow()
    for r in rows:
        # Best-effort teardown of any Toolathlon preprocess/eval containers.
        ext_eval.remove(r.preprocess_container_id)
        ext_eval.remove(r.eval_container_id)
        # Settle anything still in flight BEFORE archiving, so the ledger records
        # how the execution actually ended. The tick skips retired rows, so a
        # cell left mid-flight here would stay that way forever.
        if r.status in _INFLIGHT_RUN:
            r.status = ExperimentRunStatus.FAILED.value
            r.completed_at = retired_at
        await _snapshot_attempt(db, r, "config_retired")
        await _retire_task_tag(db, r.task_id)
        r.retired_at = retired_at
    removed = len(rows)

    # Stamp the expanded entry rather than dropping it (reassign so the JSONB
    # column is marked dirty). The key stays taken, so it can never be reused.
    exp.configurations = [
        {**c, "retired_at": retired_at.isoformat()}
        if c.get("config_key") == config_key
        else c
        for c in existing
    ]
    # Drop the matching raw spec entry. matrix_spec carries the un-keyed user
    # inputs and drives clone fidelity, so a retired config should not come back
    # on clone; match by re-canonicalized fingerprint, axes / other configs stay.
    ms = dict(exp.matrix_spec or {})
    fp = target.get("fingerprint")
    kept, dropped_spec = [], False
    for raw in list(ms.get("configurations") or []):
        canon = {k: raw.get(k) for k in CONFIG_AXES if raw.get(k) is not None}
        canon["orchestrator"] = bool(raw.get("orchestrator"))
        if not dropped_spec and fp and _config_fingerprint(canon) == fp:
            dropped_spec = True
            continue
        kept.append(raw)
    ms["configurations"] = kept
    exp.matrix_spec = ms
    await _resettle_after_retirement(db, exp)
    await _bump_revision(
        db, exp, "retire_config", {"config_key": config_key, "runs_retired": removed}
    )
    await db.commit()

    if inflight_task_ids:
        from app.plugins.runtime import get_agent_runtime

        now = datetime.utcnow()
        tasks = (
            await db.execute(select(Task).where(Task.id.in_(inflight_task_ids)))
        ).scalars().all()
        for t in tasks:
            if t.agent_container_id and t.status not in _TERMINAL_TASK:
                try:
                    get_agent_runtime().kill(t.agent_container_id)
                    t.status = TaskStatus.FAILED.value
                    t.completed_at = now
                except Exception as e:
                    logger.warning(f"remove config: kill failed for {t.id}: {e}")
        await db.commit()

    return {"config_key": config_key, "label": target.get("label"), "runs_retired": removed}


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


def _external_eval(case: dict | None) -> dict | None:
    """The case's executable-eval block (Toolathlon), when present + complete."""
    if not case:
        return None
    ext = case.get("external_eval")
    if isinstance(ext, dict) and ext.get("preprocess_command") and ext.get("eval_command"):
        return ext
    return None


def _judge_mode(eval_config: dict | None) -> bool:
    """Judge mode (``eval_config.eval_mode == "judge"``): even on cases that carry
    an executable checker, do NOT run the checker — settle the agent and let the
    E-02 outcome judge be the evaluator (no ground-truth verdict). The case's
    preprocess still seeds the workspace; only the eval (checker) phase is skipped.
    This turns a verifiable bench into an open-result one so the judge — and the
    outcome×trajectory 2-D view — can be exercised where there is no oracle."""
    return (eval_config or {}).get("eval_mode") == "judge"


def _run_checker(case: dict | None, eval_config: dict | None) -> bool:
    """Run the executable checker for this case? Yes iff it has one AND we are not
    in judge mode."""
    return _external_eval(case) is not None and not _judge_mode(eval_config)


_TRACE_CONFIG_KEYS = frozenset(
    {"tool_output_token_cap", "tool_args_token_cap", "keep_tail_on_error", "max_input_tokens"}
)

# Every top-level key the evaluation path actually reads. A key not on this list
# does nothing, so accepting it is accepting a lie about how the experiment was
# judged — the whole point of `judge_threshold` being a field is defeated if
# `judge_threshld: 6` is stored, fingerprinted and ignored. Adding a flag means
# adding it here; that is the intended friction.
_EVAL_CONFIG_KEYS = frozenset(
    {
        "eval_mode",
        "trajectory",
        "failure_modes",
        "judge_incomplete_runs",
        "outcome_files_only",
        "audit_outcome_judge_on_verifiable",
        "judge_threshold",
        "trace",
    }
)
_EVAL_CONFIG_BOOL_KEYS = frozenset(
    {
        "trajectory",
        "failure_modes",
        "judge_incomplete_runs",
        "outcome_files_only",
        "audit_outcome_judge_on_verifiable",
    }
)
_EVAL_MODES = frozenset({"checker", "judge"})
# The outcome judge scores 0–10 (see app/quality/judge.py), so a threshold outside
# that range would silently make one quadrant of the 2×2 unreachable.
JUDGE_THRESHOLD_MIN = 0.0
JUDGE_THRESHOLD_MAX = 10.0


def _validate_eval_config(eval_config: dict | None) -> None:
    """Reject a malformed ``eval_config`` at create time.

    A typo here fails silently and quietly changes what every run in the
    experiment was judged on — the exact class of error this block exists to
    prevent. Better a 400 than a corpus whose conditions differ from its label.

    ``eval_config`` is write-once (there is no update endpoint) and hashed into
    the revision fingerprint, so what passes here is what the experiment is
    committed to. That is what makes ``judge_threshold`` a pre-registration rather
    than a setting: it is fixed before any result exists, and changing it means
    cloning into a new experiment, which is a new record and not a rewritten one.
    """
    if eval_config is None:
        return
    if not isinstance(eval_config, dict):
        raise ValueError("eval_config must be an object")

    unknown = sorted(set(eval_config) - _EVAL_CONFIG_KEYS)
    if unknown:
        raise ValueError(
            f"unknown eval_config key(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(_EVAL_CONFIG_KEYS))})"
        )
    for key in sorted(_EVAL_CONFIG_BOOL_KEYS & set(eval_config)):
        if not isinstance(eval_config[key], bool):
            raise ValueError(f"eval_config.{key} must be a boolean")
    if "eval_mode" in eval_config and eval_config["eval_mode"] not in _EVAL_MODES:
        raise ValueError(
            f"eval_config.eval_mode must be one of: {', '.join(sorted(_EVAL_MODES))}"
        )
    if "judge_threshold" in eval_config:
        value = eval_config["judge_threshold"]
        if isinstance(value, bool):
            raise ValueError("eval_config.judge_threshold must be a number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("eval_config.judge_threshold must be a number")
        if not JUDGE_THRESHOLD_MIN <= value <= JUDGE_THRESHOLD_MAX:
            raise ValueError(
                "eval_config.judge_threshold must be between "
                f"{JUDGE_THRESHOLD_MIN:g} and {JUDGE_THRESHOLD_MAX:g} "
                "(the outcome judge's scale)"
            )

    _validate_trace_config(eval_config)


def _with_judge_threshold(eval_config: dict | None) -> dict:
    """Stamp the project default threshold when the author named none (SPA-87).

    Leaving the key absent worked — the report falls back to the same constant —
    but it left nothing on the record, which is the one thing this field exists
    for. Accepting a default IS a pre-registration as long as it is fixed before
    any result exists, and writing it at creation is what makes that checkable:
    the value goes into the frozen revision fingerprint like any other, and the
    report can stop saying «default» for every experiment ever made.

    Only new experiments get this. `threshold_source: default` in a report now
    means precisely «created before the threshold was recorded», rather than
    «nobody chose».
    """
    from app.quality.experiment_report import RQ2_JUDGE_THRESHOLD

    out = dict(eval_config or {})
    out.setdefault("judge_threshold", RQ2_JUDGE_THRESHOLD)
    return out


def _validate_trace_config(eval_config: dict | None) -> None:
    """Reject a malformed ``eval_config.trace`` (SPA-86)."""
    block = (eval_config or {}).get("trace")
    if block is None:
        return
    if not isinstance(block, dict):
        raise ValueError("eval_config.trace must be an object")
    unknown = sorted(set(block) - _TRACE_CONFIG_KEYS)
    if unknown:
        raise ValueError(
            f"unknown eval_config.trace key(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(_TRACE_CONFIG_KEYS))})"
        )
    for key in ("tool_output_token_cap", "tool_args_token_cap", "max_input_tokens"):
        if key not in block:
            continue
        try:
            value = int(block[key])
        except (TypeError, ValueError):
            raise ValueError(f"eval_config.trace.{key} must be an integer")
        if value < 0:
            raise ValueError(f"eval_config.trace.{key} must be >= 0 (0 disables trimming)")


def _trace_settings(eval_config: dict | None) -> tuple[object, int | None]:
    """Resolve ``eval_config.trace`` into (TraceCleanerConfig, max_input_tokens).

    An untrimmed trace is a real bill at 200+ runs, so it is opt-in per experiment
    rather than a default: absent the block, this returns (None, None) and the
    judge keeps reading the workspace settings. Any cap set to 0 means «do not
    truncate» (SPA-86)."""
    block = (eval_config or {}).get("trace")
    if not isinstance(block, dict) or not block:
        return None, None

    from app.quality.trace_cleaner import (
        DEFAULT_TOOL_ARGS_TOKEN_CAP,
        DEFAULT_TOOL_OUTPUT_TOKEN_CAP,
        TraceCleanerConfig,
    )

    def _cap(key: str, default: int) -> int:
        try:
            return max(0, int(block[key])) if key in block else default
        except (TypeError, ValueError):
            return default

    config = TraceCleanerConfig(
        tool_output_token_cap=_cap("tool_output_token_cap", DEFAULT_TOOL_OUTPUT_TOKEN_CAP),
        tool_args_token_cap=_cap("tool_args_token_cap", DEFAULT_TOOL_ARGS_TOKEN_CAP),
        keep_tail_on_error=bool(block.get("keep_tail_on_error", False)),
    )
    max_input_tokens = None
    if "max_input_tokens" in block:
        try:
            max_input_tokens = max(0, int(block["max_input_tokens"]))
        except (TypeError, ValueError):
            max_input_tokens = None
    return config, max_input_tokens


def _requires_toolathlon_pg(case: dict | None) -> bool:
    env = (case or {}).get("environment") or {}
    return "toolathlon_pg" in (env.get("required_services") or [])


def _lanes_enabled(exp: Experiment) -> int | None:
    """SPA-69: the number of isolated Toolathlon PG lanes this experiment runs on,
    or None for the legacy serial path (shared ``toolathlon_pg``). Lanes are opt-in:
    only an explicit ``n_toolathlon_lanes >= 1`` switches a run onto a per-lane
    ``toolathlon_pg_lane_<i>`` instance — so existing experiments (NULL) keep using
    the single shared container untouched."""
    n = exp.n_toolathlon_lanes
    return n if (n and n >= 1) else None


def _pg_host_for_lane(lane_index: int | None) -> str | None:
    """Per-lane PG hostname override, or None to use the default ``toolathlon_pg``
    (legacy serial). Only the host varies per lane — the database name, user and
    password stay identical, so the gym preprocess/eval scripts need no changes."""
    if lane_index is None:
        return None
    return f"toolathlon_pg_lane_{lane_index}"


def _first_free_lane(used: set[int], n_lanes: int) -> int | None:
    """Smallest lane index in ``[0, n_lanes)`` not currently occupied, or None."""
    for i in range(n_lanes):
        if i not in used:
            return i
    return None


_PORTAL_RE = re.compile(r"localhost:\d+|127\.0\.0\.1:\d+")


def _requires_portal(case: dict | None) -> bool:
    """A case whose task references a mock service at ``localhost:PORT`` — the
    preprocess container serves it, so the agent must share that container's
    network namespace to reach it."""
    return bool(case and _PORTAL_RE.search(case.get("description") or ""))


def _case_task_path(case: dict) -> str:
    path = (case.get("meta") or {}).get("task_path")
    if not path:
        raise ValueError(f"toolathlon case {case.get('case_key')!r} is missing meta.task_path")
    return path


async def _resolve_toolathlon_tools(
    db: AsyncSession, workspace_id, case: dict
) -> list[str]:
    """Registry ids of the ``toolathlon-<server>`` MCP entries the case needs
    (force-enabled on the agent via tools_override). Raises if any are missing —
    the import (``app.cli.toolathlon_import``) must run first."""
    servers = list(((case.get("environment") or {}).get("mcp_servers")) or [])
    if not servers:
        return []
    names = [f"toolathlon-{s}" for s in servers]
    rows = (
        await db.execute(
            select(RegistryEntry).where(
                RegistryEntry.workspace_id == workspace_id,
                RegistryEntry.name.in_(names),
            )
        )
    ).scalars().all()
    found = {r.name: r for r in rows}
    missing = [n for n in names if n not in found]
    if missing:
        raise ValueError(
            f"toolathlon registry entries missing (run toolathlon_import first): {missing}"
        )
    return [str(found[n].id) for n in names]


async def _apply_toolathlon_run_config(
    db: AsyncSession, exp: Experiment, case: dict, rc: dict, run_row: ExperimentRun
) -> None:
    """Patch a child run_config for a Toolathlon case: the dedicated agent image,
    the case's MCP servers force-enabled, and the higher iteration ceiling
    (mirrors app.cli.toolathlon_pilot.create). SPA-69: when the run is pinned to a
    PG lane, carry the lane host in ``rc["pg_host"]`` so the orchestrator points the
    agent's MCP servers at ``toolathlon_pg_lane_<i>`` (otherwise the shared default)."""
    tool_ids = await _resolve_toolathlon_tools(db, exp.workspace_id, case)
    rc["agent_image"] = TOOLATHLON_AGENT_IMAGE
    rc["max_iterations"] = TOOLATHLON_MAX_ITERATIONS
    pg_host = _pg_host_for_lane(run_row.lane_index)
    if pg_host:
        rc["pg_host"] = pg_host
    override = rc.get("tools_override") or {}
    enable = list(dict.fromkeys(list(override.get("enable") or []) + tool_ids))
    rc["tools_override"] = {"enable": enable, "disable": list(override.get("disable") or [])}


async def _make_child(
    db: AsyncSession,
    exp: Experiment,
    run_row: ExperimentRun,
    cfg: dict,
    case: dict,
    *,
    initial_status: str = TaskStatus.READY.value,
) -> Task:
    """Create the child task for one matrix cell run.

    The task input is EXACTLY the frozen case (no suffixes — a per-config
    marker in the prompt would confound the A/B); identification lives in
    run_config.experiment and the benchmark_* tags. Toolathlon cases are created
    in BACKLOG (``initial_status``) so the orchestrator cannot spawn the agent
    before preprocess seeds the workspace — the runner flips them READY itself.
    """
    pinned = (
        uuid.UUID(str(cfg["template_id"]))
        if (not cfg.get("orchestrator") and cfg.get("template_id"))
        else None
    )
    rc = child_run_config(
        exp, cfg, case_key=run_row.case_key, run_index=run_row.run_index
    )
    if _external_eval(case):
        await _apply_toolathlon_run_config(db, exp, case, rc, run_row)
    child = Task(
        title=case["title"][:500],
        description=case.get("description"),
        status=initial_status,
        workspace_id=exp.workspace_id,
        origin="experiment",
        template_id=pinned,
        run_config=rc,
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


def _case_rubric(case: dict | None) -> dict | None:
    """The case's inline rubric, when it is usable (a dict with a non-empty
    ``dimensions`` list). Benchmark-suite gold rubrics are unvalidated ``Any``,
    so malformed ones are silently ignored and the template rubric applies."""
    if not case:
        return None
    rubric = case.get("rubric")
    if isinstance(rubric, dict):
        dims = rubric.get("dimensions")
        if isinstance(dims, list) and dims and all(isinstance(d, dict) for d in dims):
            return rubric
    return None


async def _evaluate_child(
    db: AsyncSession, task: Task, eval_config: dict, *, case: dict | None = None
) -> None:
    """Best-effort record + evals for a terminal child, honoring eval_config.

    E-02 outcome scoring runs unless the case carries an executable checker —
    then the checker is the outcome ground truth (SPA-68); opt back in for an
    audit via ``eval_config.audit_outcome_judge_on_verifiable``.
    E-07 trajectory defaults on, E-14 failure modes defaults off. E-20 is
    captured inside build_quality_record. A case-level rubric (``case.rubric``)
    overrides the template/workspace rubric for outcome scoring. Never raises.
    """
    from app.quality.data_lake import build_quality_record
    from app.quality.judge import evaluate_task_quality
    from app.quality.trajectory import evaluate_task_trajectory

    # Settle-time harvest backstop: a benchmark run can go terminal WITHOUT a
    # completed/failed webhook — the orchestrator timeout reaper kills a long-running
    # agent and sets FAILED directly in the DB (engine.py), so the webhook harvest
    # never fires and the deliverables sitting at the workspace root would be judged
    # as an empty list → 0. This is the single choke point every settling run passes
    # through, so harvest here when result_files is still empty (no-op if the webhook
    # already harvested or nothing is on disk). Mirrors the benchmark branch in
    # webhooks.py and covers all three terminal paths (completed / failed / reaped).
    if not (task.result_files or []):
        try:
            import os

            from app.config import get_settings
            from app.storage.minio_client import upload_task_results_root

            ws_dir = os.path.join(get_settings().data_dir, "workspaces", str(task.id))
            paths = upload_task_results_root(str(task.id), ws_dir)
            if paths:
                task.result_files = paths
                await db.flush()
        except Exception as e:
            logger.warning(f"experiment: settle-time harvest failed for {task.id}: {e}")

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
    # Verifiable benches: the executable checker IS the outcome ground truth, so
    # the E-02 outcome judge is redundant here (and over-credits — SPA-68). Skip
    # it unless explicitly auditing the judge against the checker. E-07 trajectory
    # still runs below (no ground truth for the process).
    verifiable = _run_checker(case, eval_config)
    audit_outcome = bool((eval_config or {}).get("audit_outcome_judge_on_verifiable"))
    # Judge-mode benchmarks (no executable checker) still produce real deliverables
    # when the agent run ends non-cleanly — most often a max-iteration cap-hit that
    # reports ``failed``. Grading only _SUCCESS_TASK runs zeroes out that work for a
    # harness reason, not a capability one, and biases the cross-model comparison
    # (cap-hits land hardest on slow/verbose models). With ``judge_incomplete_runs``
    # set, score non-verifiable cells regardless of the agent's terminal status; the
    # run still records status=FAILED (it didn't finish cleanly) but carries an
    # outcome/trajectory score — exactly the outcome-vs-trajectory split RQ2 studies.
    judge_incomplete = (
        bool((eval_config or {}).get("judge_incomplete_runs")) and not verifiable
    )
    should_eval = task.status in _SUCCESS_TASK or judge_incomplete
    if (
        should_eval
        and rec.quality_profile is None
        and (not verifiable or audit_outcome)
    ):
        try:
            await evaluate_task_quality(
                db, task, commit=True, rubric_override=_case_rubric(case),
                files_only=bool((eval_config or {}).get("outcome_files_only")),
            )
        except Exception as e:
            await db.rollback()
            logger.warning(f"experiment: outcome eval failed for {task.id}: {e}")
    if (
        should_eval
        and (eval_config or {}).get("trajectory", True)
        and rec.trajectory_profile is None
    ):
        trace_config, trace_max_input = _trace_settings(eval_config)
        try:
            await evaluate_task_trajectory(
                db,
                task,
                commit=True,
                trace_config=trace_config,
                max_input_tokens=trace_max_input,
            )
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


# --- Toolathlon executable-eval lifecycle (gold.external_eval) --------------
# A Toolathlon run threads through two states the plain path never enters:
#   PENDING → (claim: BACKLOG task + seed/preprocess) → PREPROCESSING
#   PREPROCESSING → (preprocess done: flip task READY) → RUNNING
#   RUNNING → (agent terminal: start eval)             → EVALUATING
#   EVALUATING → (eval done: verdict + E-02/E-07)      → SUCCESS/FAILED
# Each transition is at most one step per tick; preprocess/eval containers are
# detached and polled, so the tick never blocks.

_INFLIGHT_RUN = {
    ExperimentRunStatus.PREPROCESSING.value,
    ExperimentRunStatus.RUNNING.value,
    ExperimentRunStatus.EVALUATING.value,
}


async def _start_toolathlon_run(
    db: AsyncSession, exp: Experiment, run: ExperimentRun, cfg: dict, case: dict
) -> None:
    """Claim a Toolathlon cell: create the BACKLOG task, capture launch_time,
    seed + start preprocess detached. Any setup error fails the run."""
    try:
        child = await _make_child(
            db, exp, run, cfg, case, initial_status=TaskStatus.BACKLOG.value
        )
        run.task_id = child.id
        portal = _requires_portal(case)
        long_lt, _short = ext_eval.launch_time_pair()
        run.launch_time = long_lt
        run.preprocess_started_at = datetime.utcnow()
        run.preprocess_retried = False
        run.preprocess_container_id = ext_eval.start_preprocess(
            child.id,
            _case_task_path(case),
            case["external_eval"]["preprocess_command"],
            long_lt,
            keep_alive=portal,
            pg_host=_pg_host_for_lane(run.lane_index),
        )
        # Portal case: the agent shares the (kept-alive) preprocess container's
        # netns so it can reach the mock localhost:PORT server.
        if portal:
            rc = dict(child.run_config or {})
            rc["network_mode"] = f"container:{ext_eval.preprocess_container_name(child.id)}"
            child.run_config = rc
        run.status = ExperimentRunStatus.PREPROCESSING.value
    except Exception as e:
        logger.warning(f"experiment: preprocess start failed for {run.case_key}: {e}")
        run.preprocess_log = f"preprocess start failed: {e}"[:4000]
        run.status = ExperimentRunStatus.FAILED.value
        run.failure_type = merge_failure_type(run.failure_type, FAILURE_INFRA)
        run.completed_at = datetime.utcnow()
        await _fail_orphan_task(db, run)


async def _flip_preprocessed_ready(db: AsyncSession, run: ExperimentRun) -> None:
    """Preprocess done → flip the BACKLOG task READY (the orchestrator then
    spawns the agent); the run becomes RUNNING."""
    task = (
        await db.execute(select(Task).where(Task.id == run.task_id))
    ).scalar_one_or_none()
    if task is None:
        run.status = ExperimentRunStatus.FAILED.value
        run.completed_at = datetime.utcnow()
        return
    if task.status == TaskStatus.BACKLOG.value:
        task.status = TaskStatus.READY.value
    run.status = ExperimentRunStatus.RUNNING.value


async def _fail_orphan_task(db: AsyncSession, run: ExperimentRun) -> None:
    """Mark a still-BACKLOG task FAILED when its preprocess failed, so it does
    not linger un-spawnable."""
    if not run.task_id:
        return
    task = (
        await db.execute(select(Task).where(Task.id == run.task_id))
    ).scalar_one_or_none()
    if task is not None and task.status == TaskStatus.BACKLOG.value:
        task.status = TaskStatus.FAILED.value
        task.failure_type = merge_failure_type(task.failure_type, FAILURE_INFRA)
        task.completed_at = datetime.utcnow()


async def _advance_preprocessing(
    db: AsyncSession, exp: Experiment, run: ExperimentRun, case: dict | None
) -> None:
    """Poll a PREPROCESSING run: flip its task READY on success, retry once on
    the gym ``%A`` quirk, fail on a terminal non-zero exit, proceed on a
    long-running mock server."""
    if case is None:
        run.status = ExperimentRunStatus.FAILED.value
        run.failure_type = merge_failure_type(run.failure_type, FAILURE_INFRA)
        run.completed_at = datetime.utcnow()
        return
    try:
        code, logs = ext_eval.poll_exit(run.preprocess_container_id)
    except Exception as e:
        logger.warning(f"experiment: preprocess lost for {run.case_key}: {e}")
        run.preprocess_log = f"preprocess container lost: {e}"[:4000]
        run.status = ExperimentRunStatus.FAILED.value
        run.failure_type = merge_failure_type(run.failure_type, FAILURE_INFRA)
        run.completed_at = datetime.utcnow()
        await _fail_orphan_task(db, run)
        return

    if code is None:  # still running
        started = run.preprocess_started_at or datetime.utcnow()
        if (datetime.utcnow() - started).total_seconds() >= PREPROCESS_MOCK_GRACE_S:
            # kept-alive mock server: proceed, leave it running (removed at settle)
            await _flip_preprocessed_ready(db, run)
        return

    if code == 0:
        ext_eval.remove(run.preprocess_container_id)
        run.preprocess_container_id = None
        await _flip_preprocessed_ready(db, run)
        return

    # non-zero exit: one retry with the short launch_time on the gym date quirk
    if ext_eval.has_unconverted_data_error(logs) and not run.preprocess_retried:
        ext_eval.remove(run.preprocess_container_id)
        _long, short_lt = ext_eval.launch_time_pair()
        run.launch_time = short_lt
        run.preprocess_retried = True
        run.preprocess_started_at = datetime.utcnow()
        try:
            run.preprocess_container_id = ext_eval.start_preprocess(
                run.task_id,
                _case_task_path(case),
                case["external_eval"]["preprocess_command"],
                short_lt,
                keep_alive=_requires_portal(case),
                pg_host=_pg_host_for_lane(run.lane_index),
            )
        except Exception as e:
            run.preprocess_log = f"preprocess retry failed: {e}"[:4000]
            run.status = ExperimentRunStatus.FAILED.value
            run.failure_type = merge_failure_type(run.failure_type, FAILURE_INFRA)
            run.completed_at = datetime.utcnow()
            await _fail_orphan_task(db, run)
        return

    ext_eval.remove(run.preprocess_container_id)
    run.preprocess_container_id = None
    run.preprocess_log = (logs or "")[-4000:]
    run.status = ExperimentRunStatus.FAILED.value
    run.failure_type = merge_failure_type(run.failure_type, FAILURE_INFRA)
    run.completed_at = datetime.utcnow()
    await _fail_orphan_task(db, run)


async def _start_eval(
    db: AsyncSession, exp: Experiment, run: ExperimentRun, case: dict
) -> None:
    """Agent settled → start the eval container detached (run → EVALUATING). If
    the eval cannot even launch, settle now with verdict=None."""
    try:
        run.eval_container_id = ext_eval.start_eval(
            run.task_id,
            _case_task_path(case),
            case["external_eval"]["eval_command"],
            case["external_eval"].get("groundtruth_path"),
            run.launch_time,
            pg_host=_pg_host_for_lane(run.lane_index),
        )
        run.status = ExperimentRunStatus.EVALUATING.value
    except Exception as e:
        logger.warning(f"experiment: eval start failed for {run.case_key}: {e}")
        await _settle_toolathlon(
            db, exp, run, case, verdict=None, eval_log=f"eval start failed: {e}"
        )


async def _advance_evaluating(
    db: AsyncSession, exp: Experiment, run: ExperimentRun, case: dict | None
) -> None:
    """Poll an EVALUATING run: on exit, verdict = (exit==0); on infra error,
    verdict=None. Either way settle (E-02/E-07 still run)."""
    try:
        code, logs = ext_eval.poll_exit(run.eval_container_id)
    except Exception as e:
        await _settle_toolathlon(
            db, exp, run, case, verdict=None, eval_log=f"eval container lost: {e}"
        )
        return
    if code is None:
        return  # still running
    await _settle_toolathlon(db, exp, run, case, verdict=(code == 0), eval_log=logs)


async def _settle_toolathlon(
    db: AsyncSession,
    exp: Experiment,
    run: ExperimentRun,
    case: dict | None,
    *,
    verdict: bool | None,
    eval_log: str,
) -> None:
    """Terminal settle for a Toolathlon run: record the verdict (column + event),
    clean up containers, run E-02/E-07 + denormalize. ``status`` reflects the
    AGENT task (SUCCESS/FAILED); ``external_verdict`` is the checker's pass/fail
    (None = could not evaluate) — kept separate (RQ2)."""
    task = (
        await db.execute(select(Task).where(Task.id == run.task_id))
    ).scalar_one_or_none()
    # Container cleanup first (no DB state); the verdict + event are set AFTER
    # _evaluate_child, which commits/rolls back internally — so a failing E-02
    # can never discard the verdict (mirrors the plain settle order).
    ext_eval.remove(run.preprocess_container_id)
    ext_eval.remove(run.eval_container_id)
    run.preprocess_container_id = None
    run.eval_container_id = None
    if task is None:
        run.external_verdict = verdict
        run.eval_log = (eval_log or "")[-4000:]
        run.status = ExperimentRunStatus.FAILED.value
        run.failure_type = merge_failure_type(run.failure_type, FAILURE_INFRA)
        run.completed_at = datetime.utcnow()
        return
    await _evaluate_child(db, task, exp.eval_config or {}, case=case)
    rec = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    run.external_verdict = verdict
    run.eval_log = (eval_log or "")[-4000:]
    run.status = (
        ExperimentRunStatus.SUCCESS.value
        if task.status in _SUCCESS_TASK
        else ExperimentRunStatus.FAILED.value
    )
    run.failure_type = merge_failure_type(run.failure_type, task.failure_type)
    run.cost_usd = _run_cost(task, rec)
    run.duration_seconds = _run_duration(task, rec)
    if rec is not None:
        run.weighted_score = (rec.quality_profile or {}).get("weighted_score")
        run.trajectory_score = (rec.trajectory_profile or {}).get("overall_score")
    run.completed_at = datetime.utcnow()
    # Durable verdict event (host-script parity + /results), only when the
    # checker actually produced one.
    if verdict is not None:
        await log_event(
            db,
            "external_eval_verdict",
            "system",
            {
                "passed": bool(verdict),
                "benchmark_case_id": run.case_key,
                "benchmark_suite": f"exp:{exp.id}",
                "launch_time": run.launch_time,
                "log_tail": (eval_log or "")[-2000:],
            },
            task_id=task.id,
            workspace_id=exp.workspace_id,
            commit=False,
        )


async def advance_experiment(db: AsyncSession, exp: Experiment) -> None:
    """One idempotent tick step: advance Toolathlon preprocess/eval phases,
    settle finished runs, claim pending cells under the parallelism/budget
    limits, finalize when everything settled."""
    if exp.status != ExperimentStatus.RUNNING.value:
        return

    rows = (
        await db.execute(
            select(ExperimentRun)
            .where(
                ExperimentRun.experiment_id == exp.id,
                # Cells of a retired configuration keep their lineage but must
                # not be claimed, settled or counted towards finalization.
                LIVE_CELL,
            )
            .order_by(
                ExperimentRun.config_key,
                ExperimentRun.case_key,
                ExperimentRun.run_index,
            )
        )
    ).scalars().all()
    configs = {c["config_key"]: c for c in exp.configurations}
    cases = {c["case_key"]: c for c in exp.dataset_cases}
    # Toolathlon shares one Postgres → at most one cell in flight at a time.
    requires_pg = any(_requires_toolathlon_pg(c) for c in exp.dataset_cases)
    lanes = _lanes_enabled(exp)
    # Toolathlon cases share a mutable mock postgres → run serially UNLESS the
    # experiment provisions isolated lanes (SPA-69). serial == legacy single-container.
    serial = requires_pg and lanes is None

    # Snapshot the states at tick start so a transition this tick (e.g.
    # PREPROCESSING → RUNNING) is not also processed by a later phase.
    preprocessing_ids = {
        r.id for r in rows if r.status == ExperimentRunStatus.PREPROCESSING.value
    }
    running_ids = {r.id for r in rows if r.status == ExperimentRunStatus.RUNNING.value}
    evaluating_ids = {
        r.id for r in rows if r.status == ExperimentRunStatus.EVALUATING.value
    }
    task_ids = [
        r.task_id for r in rows if r.id in (running_ids | evaluating_ids) and r.task_id
    ]
    tasks: dict[uuid.UUID, Task] = {}
    if task_ids:
        loaded = (
            await db.execute(select(Task).where(Task.id.in_(task_ids)))
        ).scalars().all()
        tasks = {t.id: t for t in loaded}

    now = datetime.utcnow()

    # 1a) Advance PREPROCESSING runs (Toolathlon).
    for r in rows:
        if r.id in preprocessing_ids:
            await _advance_preprocessing(db, exp, r, cases.get(r.case_key))

    # 1b) Settle RUNNING runs whose agent task is terminal. A Toolathlon run
    # starts its eval here (→ EVALUATING) instead of settling.
    for r in rows:
        if r.id not in running_ids:
            continue
        task = tasks.get(r.task_id)
        if task is None:
            r.status = ExperimentRunStatus.FAILED.value
            r.failure_type = merge_failure_type(r.failure_type, FAILURE_INFRA)
            r.completed_at = now
            continue
        if task.status not in _TERMINAL_TASK:
            continue
        case = cases.get(r.case_key)
        if _run_checker(case, exp.eval_config):
            await _start_eval(db, exp, r, case)
            continue
        await _evaluate_child(db, task, exp.eval_config or {}, case=case)
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
        r.failure_type = merge_failure_type(r.failure_type, task.failure_type)
        r.cost_usd = _run_cost(task, rec)
        r.duration_seconds = _run_duration(task, rec)
        if rec is not None:
            r.weighted_score = (rec.quality_profile or {}).get("weighted_score")
            r.trajectory_score = (rec.trajectory_profile or {}).get("overall_score")
        r.completed_at = datetime.utcnow()

    # 1c) Advance EVALUATING runs (Toolathlon).
    for r in rows:
        if r.id in evaluating_ids:
            await _advance_evaluating(db, exp, r, cases.get(r.case_key))

    # 2) Accumulated cost: settled rows (denormalized) + in-flight agent spend.
    total = Decimal("0")
    for r in rows:
        total += Decimal(r.cost_usd or 0)
    for r in rows:
        if r.status in (
            ExperimentRunStatus.RUNNING.value,
            ExperimentRunStatus.EVALUATING.value,
        ):
            task = tasks.get(r.task_id)
            if task is not None:
                total += Decimal(task.cost_usd or 0)
    exp.accumulated_cost_usd = total
    budget_hit = exp.budget_limit_usd is not None and total >= exp.budget_limit_usd

    pending = [r for r in rows if r.status == ExperimentRunStatus.PENDING.value]
    in_flight = [r for r in rows if r.status in _INFLIGHT_RUN]

    # 3) Claim the next pending cells while under the limits.
    if pending and not budget_hit:
        target = await inflight_target(db, parallel=True)
        if exp.max_parallel:
            target = min(target, exp.max_parallel)
        if serial:
            target = 1
        elif lanes:
            target = min(target, lanes)
        slots = max(0, target - len(in_flight))
        # SPA-69: lanes already busy with in-flight Toolathlon runs; a newly claimed
        # Toolathlon cell takes the first free one (its preprocess/eval/agent are then
        # pinned to toolathlon_pg_lane_<lane_index>).
        used_lanes = {r.lane_index for r in in_flight if r.lane_index is not None}
        # Spread the claimed slots across configs (models) instead of taking
        # pending in raw order — cells are materialized config-by-config, so the
        # naive prefix would pile N runs of ONE model onto a single provider while
        # the others idle. Pick, for each slot, a pending cell from the config with
        # the fewest in-flight+already-picked runs → ~1 task per model, balanced
        # progress and provider load (SPA-69).
        load: dict[str, int] = {}
        for r in in_flight:
            load[r.config_key] = load.get(r.config_key, 0) + 1
        # Total progress per config (settled + in-flight, i.e. everything not still
        # pending) — used to break ties so the LEAST-advanced model is preferred. A
        # plain alphabetical tie-break starves the last config: with N lanes and N+1
        # configs, the highest-sorting one never wins a tie and only runs once the
        # others are exhausted. Progress-based ties keep all models advancing evenly.
        done_by_cfg: dict[str, int] = {}
        for r in rows:
            if r.status != ExperimentRunStatus.PENDING.value:
                done_by_cfg[r.config_key] = done_by_cfg.get(r.config_key, 0) + 1
        by_cfg: dict[str, list] = {}
        for r in pending:
            by_cfg.setdefault(r.config_key, []).append(r)
        claim_list: list = []
        while len(claim_list) < slots:
            avail = [k for k, v in by_cfg.items() if v]
            if not avail:
                break
            k = min(avail, key=lambda c: (load.get(c, 0), done_by_cfg.get(c, 0), c))
            claim_list.append(by_cfg[k].pop(0))
            load[k] = load.get(k, 0) + 1
            done_by_cfg[k] = done_by_cfg.get(k, 0) + 1
        claimed = 0
        for r in claim_list:
            cfg = configs.get(r.config_key)
            case = cases.get(r.case_key)
            if cfg is None or case is None:  # defensive; cells are pre-validated
                r.status = ExperimentRunStatus.SKIPPED.value
                r.completed_at = datetime.utcnow()
                continue
            if _external_eval(case):
                if lanes:
                    lane = _first_free_lane(used_lanes, lanes)
                    if lane is None:
                        continue  # all lanes busy this tick; claim on a later tick
                    r.lane_index = lane
                    used_lanes.add(lane)
                await _start_toolathlon_run(db, exp, r, cfg, case)
            else:
                child = await _make_child(db, exp, r, cfg, case)
                r.task_id = child.id
                r.status = ExperimentRunStatus.RUNNING.value
            # This execution is attempt N of the cell; a retry snapshots the
            # state under this number before clearing it (SPA-84).
            r.attempt_count = (r.attempt_count or 0) + 1
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
    if not in_flight and all(
        r.status != ExperimentRunStatus.PENDING.value for r in rows
    ):
        skipped = any(r.status == ExperimentRunStatus.SKIPPED.value for r in rows)
        succeeded = any(r.status == ExperimentRunStatus.SUCCESS.value for r in rows)
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
    # Must be read before the pops below empty the dict.
    caller_respecified_matrix = "configurations" in changes or "axes" in changes
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
        "n_toolathlon_lanes": changes.pop(
            "n_toolathlon_lanes", exp.n_toolathlon_lanes
        ),
        "eval_config": changes.pop("eval_config", exp.eval_config or {}),
    }
    new_dataset = changes.pop("dataset", None)
    if changes:
        raise ValueError(f"unknown clone changes: {sorted(changes)}")

    # Unless the caller respecifies the matrix, a configuration retired in the
    # source stays retired in the copy — otherwise "re-run this experiment"
    # quietly reinstates the condition the author took out.
    retired_fps = (
        None
        if caller_respecified_matrix
        else {
            c.get("fingerprint")
            for c in (exp.configurations or [])
            if c.get("retired_at") and c.get("fingerprint")
        }
    )

    if new_dataset is not None:
        payload["dataset"] = new_dataset
        return await create_experiment(
            db,
            workspace_id=exp.workspace_id,
            payload=payload,
            created_by=created_by,
            exclude_fingerprints=retired_fps,
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
        exclude_fingerprints=retired_fps,
    )
