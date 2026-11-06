"""Benchmark Case Store read API (SPA-54).

Exposes the file-based benchmark suite catalogue — previously reachable only
through the CLI (``app.quality.benchmark.list_suites`` / ``load_cases``) — so the
experiment dataset picker can browse suites and inspect each case's gold signals
instead of blind-typing a suite name. Read-only; case authoring stays file-based
(full E-23 case-store CRUD is out of scope).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import get_current_workspace
from app.models.workspace import Workspace
from app.quality.benchmark import list_suites, load_cases

router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])


@router.get("/suites")
async def get_suites(_ws: Workspace = Depends(get_current_workspace)):
    """List benchmark suites in the file store with case counts."""
    out = []
    for name in list_suites():
        try:
            n = len(load_cases(name))
        except Exception:
            n = 0
        out.append({"name": name, "n_cases": n})
    return out


@router.get("/suites/{suite}")
async def get_suite(suite: str, _ws: Workspace = Depends(get_current_workspace)):
    """Read-only inspection of one suite: each case's title/family and which gold
    signals (eval engines) it carries — without exposing the gold values."""
    try:
        cases = load_cases(suite)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"suite '{suite}' not found") from None
    except Exception as exc:  # malformed case file
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    items = []
    for c in cases:
        g = c.gold
        items.append(
            {
                "id": c.id,
                "title": c.input.title,
                "category": c.category,
                "family": (c.meta or {}).get("family"),
                "required_services": c.environment.required_services if c.environment else [],
                "mcp_servers": c.environment.mcp_servers if c.environment else [],
                "gold": {
                    "reference_answer": g.reference_answer is not None,
                    "rubric": g.rubric is not None,
                    "canonical_trajectory": g.canonical_trajectory is not None,
                    "capability_spec": g.capability_spec is not None,
                    "external_eval": g.external_eval is not None,
                },
            }
        )
    return {"suite": suite, "n_cases": len(items), "cases": items}
