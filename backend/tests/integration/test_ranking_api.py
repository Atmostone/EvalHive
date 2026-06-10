"""Integration tests for the Aggregation Engine endpoints (E-19).

POST /api/quality/ranking/run derives head-to-head matches from the stored
pointwise quality scores (or ranks an explicit match list), aggregates them with
Bradley-Terry / Elo, and persists a versioned leaderboard. GET /api/quality/ranking
reads the latest (and, with history=true, the version history). The run endpoint is
owner/admin-only and makes no LLM calls.

Two models compete across several shared benchmark cases; model-a scores higher
everywhere, so it must top the leaderboard deterministically.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy import text

from app import database
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import Workspace  # noqa: F401  (kept for parity / fixtures)

_CASES = ["case1", "case2", "case3", "case4", "case5"]
_A = [9, 9, 8, 9, 8]  # model-a / tpl-a — the clear winner
_B = [4, 5, 4, 3, 5]  # model-b / tpl-b


async def _seed_records(ws):
    async with database.async_session() as s:
        for i, case in enumerate(_CASES):
            for model, tpl, scores in (
                ("model-a", "tpl-a", _A),
                ("model-b", "tpl-b", _B),
            ):
                task = Task(
                    title=f"{model}-{case}",
                    status=TaskStatus.DONE.value,
                    workspace_id=ws,
                    model_used=model,
                )
                s.add(task)
                await s.flush()
                s.add(
                    QualityRecord(
                        task_id=task.id,
                        workspace_id=ws,
                        model_used=model,
                        template_name=tpl,
                        final_status=TaskStatus.DONE.value,
                        benchmark_case_id=case,
                        quality_profile={"weighted_score": scores[i]},
                    )
                )
        await s.commit()


async def test_run_history_and_leaderboard(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_records(ws)

    # run #1 — default subject=model, method=bt
    r = await auth_client.post("/api/quality/ranking/run")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["version"] == 1
    assert report["ranking_key"] == "model:bt"
    assert report["subject"] == "model"
    assert report["method"] == "bt"
    assert report["passed"] is True

    m = report["metrics"]
    assert m["status"] == "ok"
    assert m["source"] == "derived"
    assert m["n_players"] == 2
    assert m["n_matches"] == 5  # one match per shared case
    leaders = m["players"]
    assert leaders[0]["player"] == "model-a"
    assert leaders[0]["rank"] == 1
    assert leaders[0]["wins"] == 5 and leaders[0]["losses"] == 0
    assert leaders[0]["rating"] > leaders[1]["rating"]
    assert leaders[0]["ci_low"] <= leaders[0]["rating"] <= leaders[0]["ci_high"]
    assert m["derivation"]["n_cases"] == 5

    # run #2 → next version
    r = await auth_client.post("/api/quality/ranking/run")
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # latest + history (newest first), scoped by ranking_key
    r = await auth_client.get("/api/quality/ranking")
    assert r.json()["version"] == 2
    r = await auth_client.get("/api/quality/ranking?ranking_key=model:bt&history=true")
    body = r.json()
    assert body["latest"]["version"] == 2
    assert [h["version"] for h in body["history"]] == [2, 1]


async def test_template_subject_and_elo_method(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_records(ws)

    r = await auth_client.post(
        "/api/quality/ranking/run", json={"subject": "template", "method": "elo"}
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["ranking_key"] == "template:elo"
    m = report["metrics"]
    assert m["status"] == "ok"
    assert m["method"] == "elo"
    assert {p["player"] for p in m["players"]} == {"tpl-a", "tpl-b"}
    assert m["players"][0]["player"] == "tpl-a"


async def test_explicit_matches_bypass_derivation(auth_client: AsyncClient):
    # No seeded records — rank an explicit match list instead.
    body = {
        "method": "bt",
        "matches": [
            {"player_a": "x", "player_b": "y", "outcome": "a"},
            {"player_a": "x", "player_b": "y", "outcome": "a"},
            {"player_a": "x", "player_b": "y", "outcome": "a"},
        ],
    }
    r = await auth_client.post("/api/quality/ranking/run", json=body)
    assert r.status_code == 200, r.text
    m = r.json()["metrics"]
    assert m["status"] == "ok"
    assert m["source"] == "explicit"
    assert m["players"][0]["player"] == "x"
    assert "derivation" not in m


async def test_run_empty_when_no_records(auth_client: AsyncClient):
    r = await auth_client.post("/api/quality/ranking/run")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["metrics"]["status"] == "empty"
    assert report["passed"] is False
    assert report["n_matches"] == 0


async def test_get_none_when_never_run(auth_client: AsyncClient):
    r = await auth_client.get("/api/quality/ranking")
    assert r.status_code == 200
    assert r.json() is None


async def test_run_requires_admin(auth_client: AsyncClient):
    ws = auth_client.headers["X-Workspace-Id"]
    async with database.async_session() as s:
        await s.execute(
            text("UPDATE workspace_members SET role='member' WHERE workspace_id=:w"),
            {"w": ws},
        )
        await s.commit()

    r = await auth_client.post("/api/quality/ranking/run")
    assert r.status_code == 403
