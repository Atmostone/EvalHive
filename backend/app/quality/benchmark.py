"""Benchmark Case Store (pre-E-23): file-first store of reusable task definitions.

A **benchmark case** is a versioned task *definition* with optional gold signals —
the mirror image of the result slots on ``quality_records`` (E-01). Cases live as
YAML/JSON files under ``backend/benchmarks/<suite>/`` (git is the store and its
history; the same files are what E-23 will later index in a table and publish). A
case is *materialized* into one or more runnable task instances (via the normal
task pipeline + the engine's pinned-template fast path), each tagged with
``benchmark_case_id`` / ``benchmark_suite`` so results can be aggregated by
suite × case × model.

Layers 1 (format) + 2 (loader + linkage) only. The registry table, the API/UI and
public publication are E-23 — deliberately out of scope here (KISS).
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task, TaskStatus

logger = logging.getLogger(__name__)

# backend/app/quality/benchmark.py → parents[2] == backend/
BENCHMARKS_DIR = Path(__file__).resolve().parents[2] / "benchmarks"
_CASE_SUFFIXES = (".yaml", ".yml", ".json")


# --- case format (Layer 1) ------------------------------------------------


class CaseInput(BaseModel):
    title: str
    description: str | None = None
    context: list = Field(default_factory=list)  # future: RAG doc refs / attachments


class CaseExternalEval(BaseModel):
    """Gold as an *executable external checker* (Toolathlon-style, pre-E-23).

    Commands are templates resolved by the runner at execution time; the committed
    YAML stays machine-independent via placeholders: ``${TOOLATHLON_GYM_PATH}``
    (dataset clone root), ``${AGENT_WORKSPACE}``, ``${GROUNDTRUTH_WORKSPACE}``,
    ``${LAUNCH_TIME}``, ``${RES_LOG_FILE}``. ``eval_command`` MUST receive the same
    ``--launch_time`` value as ``preprocess_command`` (date-relative checks).
    ``groundtruth_path`` is relative to the dataset root (absent for DB-only evals).
    """

    preprocess_command: str
    eval_command: str
    groundtruth_path: str | None = None

    @field_validator("preprocess_command", "eval_command")
    @classmethod
    def _command_non_empty(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("external_eval command must be non-empty")
        return v


class CaseGold(BaseModel):
    """The pluggable gold envelope — each key feeds one eval engine."""

    capability_spec: dict | None = None       # E-13
    reference_answer: str | None = None        # E-03 / outcome correctness
    rubric: Any | None = None                  # E-02 (ref or inline)
    canonical_trajectory: Any | None = None    # E-09
    external_eval: CaseExternalEval | None = None  # executable checker (E-23)


class CaseEnvironment(BaseModel):
    """Externally provisioned context the case needs at run time.

    ``required_services``: infra the runner must provide (e.g. ``toolathlon_pg`` —
    the Toolathlon PostgreSQL; eval/preprocess and most MCP servers query it).
    ``mcp_servers``: MCP server names the agent needs (Registry entries, SPA-41).
    """

    required_services: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)


class CaseRepro(BaseModel):
    template_id: str | None = None
    model_id: str | None = None
    seed: int | None = None


class BenchmarkCase(BaseModel):
    id: str
    suite: str
    category: str | None = None
    input: CaseInput
    gold: CaseGold = Field(default_factory=CaseGold)
    environment: CaseEnvironment | None = None
    repro: CaseRepro = Field(default_factory=CaseRepro)
    meta: dict = Field(default_factory=dict)


def _parse_file(path: Path) -> BenchmarkCase:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw) if path.suffix == ".json" else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: case must be a mapping")
    return BenchmarkCase(**data)


def load_cases(suite: str) -> list[BenchmarkCase]:
    """Parse + validate every case file for ``suite`` (raises on a bad/duplicate case)."""
    suite_dir = BENCHMARKS_DIR / suite
    if not suite_dir.is_dir():
        raise FileNotFoundError(f"no benchmark suite directory: {suite_dir}")
    cases: list[BenchmarkCase] = []
    seen: set[str] = set()
    for path in sorted(suite_dir.iterdir()):
        if path.suffix not in _CASE_SUFFIXES:
            continue
        case = _parse_file(path)
        if case.suite != suite:
            raise ValueError(f"{path.name}: suite '{case.suite}' != directory '{suite}'")
        if case.id in seen:
            raise ValueError(f"duplicate case id '{case.id}' in suite '{suite}'")
        seen.add(case.id)
        cases.append(case)
    return cases


def list_suites() -> list[str]:
    if not BENCHMARKS_DIR.is_dir():
        return []
    return sorted(p.name for p in BENCHMARKS_DIR.iterdir() if p.is_dir())


# --- materialization + linkage (Layer 2) ----------------------------------


def _capability_spec_for(case: BenchmarkCase) -> dict | None:
    """Carry the case category into the capability_spec the harness reads (E-13)."""
    spec = case.gold.capability_spec
    if not spec:
        return None
    spec = dict(spec)
    if case.category and not spec.get("category"):
        spec["category"] = case.category
    return spec


async def materialize(
    db: AsyncSession,
    case: BenchmarkCase,
    *,
    workspace_id,
    repeat: int = 1,
    template_id: uuid.UUID | None = None,
    model_id: uuid.UUID | None = None,
    commit: bool = True,
) -> list[Task]:
    """Create ``repeat`` runnable READY task instances from ``case``.

    Gold signals become the task's `reference_answer` / `canonical_trajectory` /
    `capability_spec`; the case is recorded via `benchmark_case_id` / `benchmark_suite`.
    A pinned template (CLI arg or `repro.template_id`) sets `task.template_id` so the
    engine takes its fast path (no decomposition); an optional model override goes
    through `run_config.model_id`. The orchestrator loop drains the READY instances.
    """
    eff_template = template_id or (
        uuid.UUID(case.repro.template_id) if case.repro.template_id else None
    )
    eff_model = model_id or (
        uuid.UUID(case.repro.model_id) if case.repro.model_id else None
    )
    run_config = {}
    if eff_template is not None:
        run_config["template_id"] = str(eff_template)
    if eff_model is not None:
        run_config["model_id"] = str(eff_model)
    if case.repro.seed is not None:
        run_config["seed"] = case.repro.seed

    tasks: list[Task] = []
    for _ in range(max(1, repeat)):
        t = Task(
            title=case.input.title,
            description=case.input.description,
            status=TaskStatus.READY.value,
            workspace_id=workspace_id,
            template_id=eff_template,
            run_config=run_config or None,
            reference_answer=case.gold.reference_answer,
            canonical_trajectory=case.gold.canonical_trajectory,
            capability_spec=_capability_spec_for(case),
            benchmark_case_id=case.id,
            benchmark_suite=case.suite,
        )
        db.add(t)
        tasks.append(t)
    if commit:
        await db.commit()
        for t in tasks:
            await db.refresh(t)
    logger.info(f"materialized case {case.id} ({case.suite}) × {len(tasks)}")
    return tasks
