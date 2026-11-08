"""Historical YAML/JSON Case Store readers and result linkage.

New runs use installed Benchmark SDK datasets. Historical schemas remain readable
without converting their original gold, environment or reproduction fields.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task

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
    """Reject retired task materialization before touching the database."""
    from app.quality.experiments import LEGACY_BENCHMARK_ERROR

    raise ValueError(LEGACY_BENCHMARK_ERROR)
