"""Trajectory Matching (E-09): deterministic, LLM-free trajectory comparison.

For the narrow class of tasks with a *canonical* trajectory — a single valid
tool-call path (typically a benchmark task, §3.2 T3) — we can compare the
agent's actual tool-trace against the reference one without an LLM. This is the
cheapest, most objective trajectory signal, but it only applies when a canonical
trajectory exists (`tasks.canonical_trajectory` is set); most real tasks have
many valid paths and must not use it.

The actual sequence is the ordered tool names of the `kind == "tool"` steps in
the E-06 cleaned trace. The reference is parsed from one of three forms:

- a list of tool names ``["search", "write_file", "run_tests"]`` — a linear chain;
- ``{"sequence": [...], "match_mode": "edit", "match_threshold": 0.9}``;
- ``{"nodes": [{"id": "n1", "tool": "search"}, ...], "edges": [["n1", "n2"], ...]}``
  — a DAG of tool calls.

Three similarity metrics are computed (all of them, every time — they are cheap):

- **exact** — 1.0 iff the actual sequence equals the reference linearization, else 0.0.
- **edit** — ``difflib.SequenceMatcher`` ratio over the tool-name lists, in [0, 1]
  (same stdlib approach as the fuzzy reference judge in :mod:`app.quality.reference`).
- **dag** — 1.0 iff the actual tool multiset equals the canonical one and every
  precedence edge is respected (the actual order is a valid topological order),
  else 0.0.

The headline ``score``/``matched`` follow the configured ``match_mode``
(default ``edit``). Consistent with the rest of ``app.quality``, the matcher
never raises: a bad reference or any failure becomes ``status: "error"``.
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quality_record import QualityRecord
from app.models.task import Task
from app.quality.trace_cleaner import build_cleaned_trace
from app.utils.events import log_event

logger = logging.getLogger(__name__)

TRAJECTORY_MATCH_SCHEMA_VERSION = 1
MATCH_MODES = ("exact", "edit", "dag")
DEFAULT_MATCH_MODE = "edit"
# For the (graded) edit mode, how close counts as a pass. exact/dag are binary.
DEFAULT_EDIT_THRESHOLD = 0.9
_DETAIL_CAP = 500


def _normalize(name: str | None) -> str:
    return (name or "").strip().lower()


# --- inputs ---------------------------------------------------------------


def extract_tool_sequence(cleaned_trace: dict) -> list[str]:
    """The ordered tool names of the trace's tool steps (E-06 ``kind == 'tool'``).

    Steps without a ``tool_name`` (e.g. tool outputs recovered from the MinIO
    log archive, which loses the name) are skipped — they cannot be matched.
    """
    seq: list[str] = []
    for s in cleaned_trace.get("steps") or []:
        if s.get("kind") == "tool" and s.get("tool_name"):
            seq.append(str(s["tool_name"]))
    return seq


def _topological_order(node_ids: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    """Kahn's algorithm, breaking ties by the original node order (deterministic).

    Returns ``None`` if the graph has a cycle (not a DAG)."""
    indeg = {n: 0 for n in node_ids}
    adj: dict[str, list[str]] = {n: [] for n in node_ids}
    for u, v in edges:
        if u in indeg and v in indeg:
            adj[u].append(v)
            indeg[v] += 1
    order: list[str] = []
    # Stable: consider nodes in their declared order.
    ready = [n for n in node_ids if indeg[n] == 0]
    while ready:
        n = ready.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                # Re-insert keeping declared order among the ready set.
                ready.append(m)
                ready.sort(key=node_ids.index)
    return order if len(order) == len(node_ids) else None


def _sequence_ref(seq, match_mode, match_threshold) -> dict:
    """A linear chain: node i → node i+1. Edges are node-index pairs so that a
    repeated tool stays a distinct node (tool-level edges would be ambiguous)."""
    tools = [str(t) for t in seq]
    edges = [(i, i + 1) for i in range(len(tools) - 1)]
    return {
        "tools": tools, "edges": edges, "linear": tools,
        "form": "sequence", "match_mode": match_mode,
        "match_threshold": match_threshold,
    }


def parse_reference(canonical) -> dict:
    """Normalize the three accepted reference forms into a common shape.

    Returns ``{tools, edges, linear, form, match_mode, match_threshold}`` where
    ``tools`` is the per-node tool (node index = position), ``edges`` is the list
    of ``(from_idx, to_idx)`` node-index precedence pairs, and ``linear`` is a
    canonical linearization (for the exact/edit metrics). Raises ``ValueError``
    on an unusable reference.
    """
    match_mode = DEFAULT_MATCH_MODE
    match_threshold = DEFAULT_EDIT_THRESHOLD

    # Form 1: a bare list of tool names — a linear chain.
    if isinstance(canonical, list):
        return _sequence_ref(canonical, match_mode, match_threshold)

    if not isinstance(canonical, dict):
        raise ValueError("canonical_trajectory must be a list or an object")

    if canonical.get("match_mode") in MATCH_MODES:
        match_mode = canonical["match_mode"]
    try:
        if canonical.get("match_threshold") is not None:
            match_threshold = float(canonical["match_threshold"])
    except (TypeError, ValueError):
        pass

    # Form 2: {"sequence": [...]} — a linear chain.
    if isinstance(canonical.get("sequence"), list):
        return _sequence_ref(canonical["sequence"], match_mode, match_threshold)

    # Form 3: {"nodes": [{id, tool}], "edges": [[from, to]]} — a DAG.
    nodes = canonical.get("nodes")
    if isinstance(nodes, list) and nodes:
        idx_of: dict[str, int] = {}
        tools: list[str] = []
        for i, n in enumerate(nodes):
            if not isinstance(n, dict) or "tool" not in n:
                raise ValueError("each DAG node needs a 'tool'")
            nid = str(n.get("id") or f"_n{i}")
            if nid in idx_of:
                raise ValueError(f"duplicate DAG node id '{nid}'")
            idx_of[nid] = i
            tools.append(str(n["tool"]))
        edges: list[tuple[int, int]] = []
        for e in canonical.get("edges") or []:
            if not (isinstance(e, (list, tuple)) and len(e) == 2):
                raise ValueError("each DAG edge must be a [from, to] pair")
            u, v = str(e[0]), str(e[1])
            if u not in idx_of or v not in idx_of:
                raise ValueError(f"DAG edge references unknown node: {e}")
            edges.append((idx_of[u], idx_of[v]))
        order = _topological_order(list(range(len(tools))), edges)
        if order is None:
            raise ValueError("canonical DAG has a cycle")
        linear = [tools[i] for i in order]
        return {
            "tools": tools, "edges": edges, "linear": linear,
            "form": "dag", "match_mode": match_mode,
            "match_threshold": match_threshold,
        }

    raise ValueError("canonical_trajectory has no 'sequence' or 'nodes'")


# --- metrics --------------------------------------------------------------


def exact_match(actual: list[str], linear: list[str]) -> float:
    """1.0 iff the normalized tool sequences are identical, else 0.0."""
    a = [_normalize(x) for x in actual]
    b = [_normalize(x) for x in linear]
    return 1.0 if a == b else 0.0


def edit_similarity(actual: list[str], linear: list[str]) -> float:
    """SequenceMatcher ratio over the normalized tool-name lists, in [0, 1]."""
    a = [_normalize(x) for x in actual]
    b = [_normalize(x) for x in linear]
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def dag_consistency(actual: list[str], tools: list[str], edges: list[tuple[int, int]]) -> tuple[float, str]:
    """1.0 iff the actual run is a valid topological order of the canonical DAG.

    ``tools`` is the per-node tool (index = node), ``edges`` are ``(from, to)``
    node-index precedence pairs. Requires the same tool multiset, then simulates
    Kahn's algorithm *driven by the actual order*: walk the actual calls and, for
    each, consume an available (in-degree 0) node with that tool, unlocking its
    successors. Exact for chains and DAGs with distinct tools per node; for a DAG
    where the same tool labels several parallel nodes the greedy instance pick is
    a close approximation. Returns ``(score, note)``.
    """
    a = [_normalize(x) for x in actual]
    expected = [_normalize(t) for t in tools]
    if Counter(a) != Counter(expected):
        return 0.0, "tool multiset differs from the canonical DAG"

    n = len(tools)
    indeg = [0] * n
    adj: list[list[int]] = [[] for _ in range(n)]
    for u, v in edges:
        adj[u].append(v)
        indeg[v] += 1

    # Available (unlocked) node instances, bucketed by normalized tool.
    available: dict[str, deque[int]] = {}
    for i in range(n):
        if indeg[i] == 0:
            available.setdefault(expected[i], deque()).append(i)

    for t in a:
        bucket = available.get(t)
        if not bucket:
            return 0.0, "actual order is not a valid topological order of the canonical DAG"
        node = bucket.popleft()
        for m in adj[node]:
            indeg[m] -= 1
            if indeg[m] == 0:
                available.setdefault(expected[m], deque()).append(m)
    return 1.0, "valid topological order of the canonical DAG"


def match_trajectory(cleaned_trace: dict, canonical, *, mode: str | None = None) -> dict:
    """Compare a cleaned trace against a canonical trajectory. Never raises."""
    try:
        ref = parse_reference(canonical)
    except Exception as e:  # noqa: BLE001 — a bad reference must not crash the request
        logger.warning(f"canonical trajectory parse failed: {e}")
        return {"status": "error", "error": str(e)[:_DETAIL_CAP]}

    actual = extract_tool_sequence(cleaned_trace)
    ex = exact_match(actual, ref["linear"])
    ed = round(edit_similarity(actual, ref["linear"]), 4)
    dag, dag_note = dag_consistency(actual, ref["tools"], ref["edges"])
    metrics = {"exact": ex, "edit": ed, "dag": dag}

    use_mode = mode if mode in MATCH_MODES else ref["match_mode"]
    score = metrics[use_mode]
    threshold = ref["match_threshold"] if use_mode == "edit" else 1.0
    matched = score >= threshold

    detail = (
        f"exact {ex:.0f}; edit ratio {ed:.2f}; dag: {dag_note}. "
        f"actual {len(actual)} tool call(s) vs reference {len(ref['linear'])}."
    )
    return {
        "status": "scored",
        "mode": use_mode,
        "score": round(score, 4),
        "matched": bool(matched),
        "threshold": threshold,
        "metrics": metrics,
        "actual_sequence": actual,
        "reference_sequence": ref["linear"],
        "reference_form": ref["form"],
        "detail": detail[:_DETAIL_CAP],
    }


async def evaluate_task_trajectory_match(
    db: AsyncSession, task: Task, *, commit: bool = True
) -> dict | None:
    """Match ``task``'s trajectory against its canonical one; write the profile.

    Returns ``None`` (skipped) when the task has no ``canonical_trajectory``.
    Otherwise builds the cleaned trace, computes the deterministic match, and
    writes it to ``quality_records.trajectory_match_profile`` (overwriting on
    re-run, for on-demand re-evaluation). A bad reference is persisted as a
    profile with ``status: "error"`` — not skipped.
    """
    if not task.canonical_trajectory:
        logger.info(f"trajectory match skipped — no canonical trajectory for task {task.id}")
        return None

    cleaned_trace = await build_cleaned_trace(db, task)
    result = match_trajectory(cleaned_trace, task.canonical_trajectory)

    stats = cleaned_trace.get("stats") or {}
    tool_steps = sum(
        1 for s in (cleaned_trace.get("steps") or [])
        if s.get("kind") == "tool" and s.get("tool_name")
    )
    profile = {
        "schema_version": TRAJECTORY_MATCH_SCHEMA_VERSION,
        "status": result.get("status"),
        "mode": result.get("mode"),
        "score": result.get("score"),
        "matched": result.get("matched", False),
        "threshold": result.get("threshold"),
        "metrics": result.get("metrics", {}),
        "actual_sequence": result.get("actual_sequence", []),
        "reference_sequence": result.get("reference_sequence", []),
        "reference_form": result.get("reference_form"),
        "detail": result.get("detail", ""),
        "trace_stats": {
            "steps_total": stats.get("steps_total"),
            "tool_steps": tool_steps,
        },
        "evaluated_at": datetime.utcnow().isoformat(),
        "errors": (
            [{"error": result.get("error")}] if result.get("status") == "error" else []
        ),
    }

    record = (
        await db.execute(select(QualityRecord).where(QualityRecord.task_id == task.id))
    ).scalar_one_or_none()
    if record is None:
        from app.quality.data_lake import build_quality_record

        record = await build_quality_record(db, task, commit=False)
    if record is not None:
        record.trajectory_match_profile = profile

    await log_event(
        db,
        "trajectory_match_evaluated",
        "system",
        {
            "mode": profile["mode"],
            "score": profile["score"],
            "matched": profile["matched"],
            "status": profile["status"],
        },
        task_id=task.id,
        workspace_id=task.workspace_id,
        commit=False,
    )

    if commit:
        await db.commit()
    return profile
