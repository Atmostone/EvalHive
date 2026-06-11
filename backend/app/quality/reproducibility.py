"""Reproducibility Snapshot (E-20).

Captures the exact state that produced an eval run — model, temperature, seed,
soul_md, memory state, RAG context, tools, task input — into an
``experiment_snapshot`` stored in the Quality Data Lake (the
``quality_records.reproducibility`` slot reserved by E-01). The snapshot is built
from data already gathered at record-build time (``blob["execution"]``, sourced
from the ``agent_spawned`` event), so capture is automatic and free — no LLM calls.

Three capabilities:
- **assemble** (pure): turn a task + its execution section into a snapshot with a
  deterministic ``fingerprint`` over the run-defining fields, plus an honest
  ``manifest`` of what was captured vs. what the runtime doesn't expose
  (``temperature``, tool versions, point-in-time RAG vectors, ``seed`` outside
  benchmark runs). Large text is hashed into the fingerprinted block and kept
  raw-capped under ``content`` — so reproducibility is never overstated.
- **diff** (pure): structural comparison of two snapshots' determinism blocks.
- **replay**: derive a ``run_config`` from a stored snapshot and clone the task via
  the existing re-run primitive (``clone_task_for_rerun``) — the U-03 seam.

Per-record by nature (one snapshot per task), so — unlike the E-17/18/19 reports —
it needs no new table: it lives in the reserved JSONB slot, like ``quality_profile``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.utils.events import log_event

logger = logging.getLogger(__name__)

SNAPSHOT_SCHEMA_VERSION = 1
_CAP = 20000  # mirror engine._FLAT_MEMORY_CAP — bound large captured text
_FLAT_KEYS = ("rules_md", "memory_md")
_RAG_COLLECTION = "spawnhive_docs"


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _cap(text):
    """Bound captured text so a huge prompt/memory can't bloat the slot."""
    return text[:_CAP] if isinstance(text, str) else text


def _opt_sha256(value):
    """Stable hex digest of a value, or ``None`` when absent — so "no reference
    answer" stays distinguishable from "reference answer is the empty string".
    Non-strings are canonicalized to JSON first."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mark(captured: list, missing: list, name: str, present) -> None:
    (captured if present else missing).append(name)


def _build_manifest(
    *, model_api_name, soul_md, tools, memory_context, flat_memory, temperature, seed, template_id
) -> dict:
    """Honest captured/missing accounting so reproducibility isn't overstated."""
    captured: list[str] = []
    missing: list[str] = []
    _mark(captured, missing, "model_api_name", bool(model_api_name))
    _mark(captured, missing, "soul_md", bool(soul_md))
    _mark(captured, missing, "tools", bool(tools))
    _mark(captured, missing, "memory_context", bool(memory_context))
    _mark(captured, missing, "flat_memory", any((flat_memory or {}).values()))
    _mark(captured, missing, "template", bool(template_id))
    _mark(captured, missing, "temperature", temperature is not None)
    _mark(captured, missing, "seed", seed is not None)
    captured.append("task_input")  # title is always present
    # Never available in the runtime today, regardless of this run:
    missing.append("tool_versions")
    missing.append("rag_vectors")

    all_notes = {
        "temperature": "set only when run_config carries a per-run override (SPA-40)",
        "tool_versions": "only tool names are tracked; versions unknown",
        "seed": "present only for benchmark-materialized runs",
        "rag_vectors": "point-in-time Qdrant capture out of scope; memory_context captured instead",
    }
    notes = {k: v for k, v in all_notes.items() if k in missing}
    return {"captured": captured, "missing": missing, "notes": notes}


def assemble_snapshot(task_like, execution: dict | None = None) -> dict:
    """Build the experiment_snapshot for a task (pure — no DB, no I/O).

    ``task_like`` duck-types ``run_config / model_used / title / description /
    reference_answer / canonical_trajectory`` (tests pass a ``SimpleNamespace``).
    ``execution`` is the Quality Data Lake ``blob["execution"]`` dict — the spawn
    snapshot: ``soul_md / tools / mcp_servers / model_api_name / memory_context /
    flat_memory / template_*``. Returns the snapshot with its ``fingerprint`` filled.
    """
    execution = execution or {}
    rc = getattr(task_like, "run_config", None) or {}

    soul_md = execution.get("soul_md", "") or ""
    memory_context = execution.get("memory_context", "") or ""
    flat_memory = execution.get("flat_memory", {}) or {}
    tools = sorted(execution.get("tools", []) or [])
    mcp_servers = sorted(execution.get("mcp_servers", []) or [])
    model_api_name = execution.get("model_api_name") or getattr(task_like, "model_used", None)
    template_id = execution.get("template_id")
    template_name = execution.get("template_name")

    temperature = rc.get("temperature")
    seed = rc.get("seed")

    title = getattr(task_like, "title", None)
    description = getattr(task_like, "description", None)
    reference_answer = getattr(task_like, "reference_answer", None)
    canonical_trajectory = getattr(task_like, "canonical_trajectory", None)

    determinism = {
        "model_api_name": model_api_name,
        "temperature": temperature,
        "seed": seed,
        "template_id": str(template_id) if template_id else None,
        "template_name": template_name,
        "tools": tools,
        "mcp_servers": mcp_servers,
        "soul_md_sha256": _opt_sha256(soul_md),
        "memory_context_sha256": _opt_sha256(memory_context),
        "flat_memory_sha256": {k: _opt_sha256(flat_memory.get(k)) for k in _FLAT_KEYS},
        "rag": {
            "collection": _RAG_COLLECTION,
            "memory_context_present": bool(memory_context),
            "vector_capture": "out_of_scope",
        },
        "tool_versions": {t: None for t in tools},
        "task_input": {
            "title": title,
            "description_sha256": _opt_sha256(description),
            "reference_answer_sha256": _opt_sha256(reference_answer),
            "canonical_trajectory_sha256": _opt_sha256(canonical_trajectory),
        },
    }

    content = {
        "soul_md": _cap(soul_md),
        "memory_context": _cap(memory_context),
        "flat_memory": {k: _cap(flat_memory.get(k, "") or "") for k in _FLAT_KEYS},
        "task_input": {
            "description": _cap(description),
            "reference_answer": _cap(reference_answer),
            "canonical_trajectory": canonical_trajectory,
        },
    }

    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": datetime.utcnow().isoformat(),
        "determinism": determinism,
        "content": content,
        "manifest": _build_manifest(
            model_api_name=model_api_name,
            soul_md=soul_md,
            tools=tools,
            memory_context=memory_context,
            flat_memory=flat_memory,
            temperature=temperature,
            seed=seed,
            template_id=template_id,
        ),
    }
    snapshot["fingerprint"] = snapshot_fingerprint(snapshot)
    return snapshot


def snapshot_fingerprint(snapshot: dict) -> str:
    """Stable SHA-256 over the run-defining ``determinism`` block only.

    Excludes ``captured_at`` (volatile), ``content`` (raw text already hashed into
    determinism) and ``manifest``. Canonical form — sorted keys + compact separators
    — so equal runs ⇒ equal fingerprint regardless of dict ordering."""
    core = snapshot.get("determinism", {})
    canonical = json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten a nested dict into dotted paths; lists stay as leaf values."""
    out: dict = {}
    for k, v in d.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{path}."))
        else:
            out[path] = v
    return out


def _diff_summary(added: dict, removed: dict, changed: dict, *, identical: bool) -> str:
    if identical and not (added or removed or changed):
        return "identical — same fingerprint"
    head = f"{len(changed)} changed, {len(added)} added, {len(removed)} removed"
    named = [f"{k} {changed[k]['from']}→{changed[k]['to']}" for k in list(changed)[:3]]
    return head + ("; " + ", ".join(named) if named else "")


def diff_snapshots(a: dict, b: dict) -> dict:
    """Structural diff of two snapshots' determinism blocks (pure).

    Returns added/removed/changed keyed by dotted path (deterministically ordered),
    the two fingerprints, an ``identical`` flag and a human-readable ``summary``."""
    fa = a.get("fingerprint") or snapshot_fingerprint(a)
    fb = b.get("fingerprint") or snapshot_fingerprint(b)
    flat_a = _flatten(a.get("determinism", {}))
    flat_b = _flatten(b.get("determinism", {}))

    added = {k: flat_b[k] for k in sorted(flat_b.keys() - flat_a.keys())}
    removed = {k: flat_a[k] for k in sorted(flat_a.keys() - flat_b.keys())}
    changed = {
        k: {"from": flat_a[k], "to": flat_b[k]}
        for k in sorted(flat_a.keys() & flat_b.keys())
        if flat_a[k] != flat_b[k]
    }
    return {
        "fingerprint_a": fa,
        "fingerprint_b": fb,
        "identical": fa == fb,
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": _diff_summary(added, removed, changed, identical=fa == fb),
    }


# --------------------------------------------------------------------------- #
# DB wrappers
# --------------------------------------------------------------------------- #
def _has_execution_context(execution: dict) -> bool:
    """True when there's any spawn-time state worth snapshotting."""
    return bool(
        execution.get("model_api_name")
        or execution.get("soul_md")
        or execution.get("tools")
        or execution.get("memory_context")
    )


async def capture_snapshot(db: AsyncSession, task: Task, *, commit: bool = True) -> dict | None:
    """(Re)build the experiment_snapshot for ``task`` and store it in its quality
    record's ``reproducibility`` slot. Ensures the record exists first. Returns the
    snapshot, or ``None`` when there is no execution context to snapshot (the caller
    surfaces ``skipped``). Re-capture overwrites the slot (last-write-wins)."""
    from app.quality.data_lake import assemble_record, build_quality_record

    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        record = await build_quality_record(db, task, commit=False)
    if record is None:
        return None

    execution = (await assemble_record(db, task)).get("execution", {})
    if not _has_execution_context(execution):
        return None

    snapshot = assemble_snapshot(task, execution)
    record.reproducibility = snapshot
    await log_event(
        db,
        "reproducibility_captured",
        "system",
        {"fingerprint": snapshot["fingerprint"], "missing": snapshot["manifest"]["missing"]},
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )
    if commit:
        await db.commit()
    return snapshot


async def replay_from_snapshot(db: AsyncSession, task_id) -> dict:
    """Replay a run from its stored snapshot: derive a ``run_config`` from the
    captured state and clone the source task via the existing re-run primitive
    (``clone_task_for_rerun``), linked through ``replay_of_task_id``.

    The model is pinned via the source's ``template_id`` (a template carries its
    model); ``soul_md`` / ``seed`` / ``temperature`` are passed through when the
    snapshot captured them. ``model_api_name`` is *not* resolved back to a model id —
    so determinism is honestly bounded to "same template + prompt (+ seed/temp where
    available)", as the gap manifest states. Raises ``ValueError`` when the task or
    its snapshot is missing."""
    from app.orchestrator.rerun import clone_task_for_rerun

    tid = task_id if isinstance(task_id, uuid.UUID) else uuid.UUID(str(task_id))
    source = await db.get(Task, tid)
    if source is None:
        raise ValueError("task not found")
    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == source.id))
    ).scalar_one_or_none()
    snapshot = record.reproducibility if record else None
    if not snapshot:
        raise ValueError("no reproducibility snapshot for this task")

    det = snapshot.get("determinism", {})
    content = snapshot.get("content", {})
    run_config: dict = {}
    if det.get("template_id"):
        run_config["template_id"] = det["template_id"]
    if det.get("seed") is not None:
        run_config["seed"] = det["seed"]
    if det.get("temperature") is not None:
        run_config["temperature"] = det["temperature"]
    soul_md = content.get("soul_md")
    if soul_md:
        run_config["soul_md"] = soul_md

    clone = await clone_task_for_rerun(
        db, source, run_config=run_config or None, title_suffix=" (replay)", commit=True
    )
    return {
        "replay_task_id": str(clone.id),
        "source_task_id": str(source.id),
        "run_config": clone.run_config,
        "fingerprint": snapshot.get("fingerprint"),
    }
