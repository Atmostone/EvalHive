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
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.quality.benchmark import _capability_spec_for, load_cases

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
