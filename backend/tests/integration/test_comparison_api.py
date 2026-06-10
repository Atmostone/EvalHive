"""Integration tests for the Pairwise Comparison Framework endpoints (E-21).

Direct comparison + LLM judge (with position-bias mitigation), human-verdict,
generated-B advance (clone + auto-judge), the ELO leaderboard hand-off to E-19,
the judge↔human agreement stat, and the owner/admin gate. The LLM judge is mocked
with a deterministic fake provider (no real LLM calls): a content-keyed provider
that picks the answer marked ``QUALITY=high`` regardless of order (→ no position
bias), and a first-slot provider that always picks "a" (→ position bias on swap).
"""

import json
import uuid
from unittest.mock import MagicMock

from httpx import AsyncClient
from sqlalchemy import text

from app import database
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.task import Task, TaskStatus
from app.models.workspace import Workspace
from app.quality import comparison as comparison_mod


# --------------------------------------------------------------------------- #
# Fake judge providers
# --------------------------------------------------------------------------- #
def _winner_resp(winner, pt=10, ct=4):
    fn = MagicMock()
    fn.arguments = json.dumps({"winner": winner, "reasoning": "because"})
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _ContentProvider:
    """Picks whichever slot's answer is marked ``QUALITY=high`` — a stable,
    content-driven verdict that survives an order swap (no position bias)."""

    async def acompletion(self, **kwargs):
        user = kwargs["messages"][1]["content"]
        a_sec, _, b_sec = user.partition("=== Answer B ===")
        a_part = a_sec.partition("=== Answer A ===")[2]
        if "QUALITY=high" in a_part:
            return _winner_resp("a")
        if "QUALITY=high" in b_sec:
            return _winner_resp("b")
        return _winner_resp("tie")


class _FirstSlotProvider:
    """Always prefers the first answer shown → on the swapped order it flips, so the
    reconciliation must detect position bias and return a tie."""

    async def acompletion(self, **kwargs):
        return _winner_resp("a")


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
async def _seed_judge_model(ws, *, api_name="judge-m"):
    async with database.async_session() as s:
        prov = Provider(workspace_id=ws, name="p", api_key="k", endpoint="http://x/v1")
        s.add(prov)
        await s.flush()
        model = LLMModel(
            provider_id=prov.id, display_name="J", api_name=api_name,
            input_price_per_1m_usd=1, output_price_per_1m_usd=2,
        )
        s.add(model)
        await s.flush()
        wsrow = await s.get(Workspace, ws)
        wsrow.quality_judge_model_id = model.id
        await s.commit()


async def _seed_task(ws, *, model_used, summary, title="t", status=TaskStatus.DONE.value):
    async with database.async_session() as s:
        task = Task(
            title=title,
            status=status,
            workspace_id=ws,
            model_used=model_used,
            result_summary=summary,
        )
        s.add(task)
        await s.flush()
        s.add(
            QualityRecord(
                task_id=task.id, workspace_id=ws, model_used=model_used,
                final_status=status,
            )
        )
        await s.commit()
        return task.id


async def _make_member(ws):
    async with database.async_session() as s:
        await s.execute(
            text("UPDATE workspace_members SET role='member' WHERE workspace_id=:w"),
            {"w": str(ws)},
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# Direct comparison + LLM judge
# --------------------------------------------------------------------------- #
async def test_direct_llm_judge_picks_content_winner(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    monkeypatch.setattr(comparison_mod, "get_llm_provider", lambda: _ContentProvider())

    a = await _seed_task(ws, model_used="model-a", summary="QUALITY=low answer", title="A")
    b = await _seed_task(ws, model_used="model-b", summary="QUALITY=high answer", title="B")

    r = await auth_client.post(
        "/api/quality/comparison",
        json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b)},
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["status"] == "judged"
    assert c["judge_verdict"] == "b"  # B is QUALITY=high in either order
    assert c["judge_detail"]["position_bias_detected"] is False
    assert c["player_a"] == "model-a" and c["player_b"] == "model-b"
    assert "ab" in c["judge_detail"]["orders"] and "ba" in c["judge_detail"]["orders"]


async def test_position_bias_detected_yields_tie(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    monkeypatch.setattr(comparison_mod, "get_llm_provider", lambda: _FirstSlotProvider())

    a = await _seed_task(ws, model_used="model-a", summary="answer a", title="A")
    b = await _seed_task(ws, model_used="model-b", summary="answer b", title="B")

    r = await auth_client.post(
        "/api/quality/comparison",
        json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b)},
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["judge_verdict"] == "tie"
    assert c["judge_detail"]["position_bias_detected"] is True


async def test_get_comparison_has_side_by_side(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    monkeypatch.setattr(comparison_mod, "get_llm_provider", lambda: _ContentProvider())
    a = await _seed_task(ws, model_used="model-a", summary="QUALITY=high", title="A")
    b = await _seed_task(ws, model_used="model-b", summary="QUALITY=low", title="B")
    created = (
        await auth_client.post(
            "/api/quality/comparison",
            json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b)},
        )
    ).json()

    r = await auth_client.get(f"/api/quality/comparison/{created['id']}")
    assert r.status_code == 200, r.text
    sides = r.json()["side_by_side"]
    assert sides["a"]["player"] == "model-a"
    assert sides["b"]["title"] == "B"


# --------------------------------------------------------------------------- #
# Human verdict + agreement
# --------------------------------------------------------------------------- #
async def test_human_verdict_and_agreement(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    monkeypatch.setattr(comparison_mod, "get_llm_provider", lambda: _ContentProvider())
    a = await _seed_task(ws, model_used="model-a", summary="QUALITY=high", title="A")
    b = await _seed_task(ws, model_used="model-b", summary="QUALITY=low", title="B")
    created = (
        await auth_client.post(
            "/api/quality/comparison",
            json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b)},
        )
    ).json()
    assert created["judge_verdict"] == "a"

    # Human agrees with the judge.
    r = await auth_client.put(
        f"/api/quality/comparison/{created['id']}/human-verdict",
        json={"verdict": "a", "reasoning": "A is clearly better"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["human_verdict"] == "a"

    r = await auth_client.get("/api/quality/comparison")
    body = r.json()
    assert body["agreement"]["n"] == 1
    assert body["agreement"]["agreement"] == 1.0


async def test_human_mode_no_auto_judge(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    a = await _seed_task(ws, model_used="model-a", summary="x", title="A")
    b = await _seed_task(ws, model_used="model-b", summary="y", title="B")
    r = await auth_client.post(
        "/api/quality/comparison",
        json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b), "judge_mode": "human"},
    )
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["status"] == "ready"
    assert c["judge_verdict"] is None


# --------------------------------------------------------------------------- #
# Generated B (clone + auto-judge on the tick)
# --------------------------------------------------------------------------- #
async def test_generated_advance_clones_b_and_judges(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    monkeypatch.setattr(comparison_mod, "get_llm_provider", lambda: _ContentProvider())

    src = await _seed_task(ws, model_used="model-a", summary="QUALITY=low source", title="src")

    r = await auth_client.post(
        "/api/quality/comparison",
        json={
            "subject": "model",
            "task_a_id": str(src),
            "source_task_id": str(src),
            "b_run_config": {"model_id": "model-b"},
        },
    )
    assert r.status_code == 200, r.text
    comp = r.json()
    assert comp["status"] == "generating"
    assert comp["task_b_id"] is not None  # first advance (in create) already cloned B

    # Simulate B finishing successfully with a winning answer.
    async with database.async_session() as s:
        bid = uuid.UUID(comp["task_b_id"])
        bt = await s.get(Task, bid)
        bt.status = TaskStatus.DONE.value
        bt.model_used = "model-b"
        bt.result_summary = "QUALITY=high generated answer"
        await s.commit()

    # The scheduler tick advances generating comparisons → ready → auto-judge.
    from app.quality.comparison import advance_active_comparisons

    async with database.async_session() as s:
        await advance_active_comparisons(s)

    r = await auth_client.get(f"/api/quality/comparison/{comp['id']}")
    out = r.json()
    assert out["status"] == "judged"
    assert out["player_b"] == "model-b"
    assert out["judge_verdict"] == "b"


# --------------------------------------------------------------------------- #
# Leaderboard hand-off to E-19
# --------------------------------------------------------------------------- #
async def test_leaderboard_persists_explicit_ranking(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_judge_model(ws)
    monkeypatch.setattr(comparison_mod, "get_llm_provider", lambda: _ContentProvider())
    a = await _seed_task(ws, model_used="model-a", summary="QUALITY=low", title="A")
    b = await _seed_task(ws, model_used="model-b", summary="QUALITY=high", title="B")
    await auth_client.post(
        "/api/quality/comparison",
        json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b)},
    )

    r = await auth_client.post(
        "/api/quality/comparison/leaderboard",
        json={"subject": "model", "method": "elo", "source": "judge"},
    )
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["metrics"]["source"] == "explicit"
    assert report["n_players"] == 2
    assert report["n_matches"] == 1
    assert report["pairwise"]["n_judged_comparisons"] == 1
    # The winner (model-b) should outrank model-a.
    players = {p["player"]: p["rank"] for p in report["metrics"]["players"]}
    assert players["model-b"] < players["model-a"]


# --------------------------------------------------------------------------- #
# Validation + auth
# --------------------------------------------------------------------------- #
async def test_create_requires_b_or_run_config(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    a = await _seed_task(ws, model_used="model-a", summary="x")
    r = await auth_client.post(
        "/api/quality/comparison", json={"subject": "model", "task_a_id": str(a)}
    )
    assert r.status_code == 422  # neither task_b_id nor b_run_config


async def test_create_requires_admin(auth_client: AsyncClient):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    a = await _seed_task(ws, model_used="model-a", summary="x")
    b = await _seed_task(ws, model_used="model-b", summary="y")
    await _make_member(ws)
    r = await auth_client.post(
        "/api/quality/comparison",
        json={"subject": "model", "task_a_id": str(a), "task_b_id": str(b)},
    )
    assert r.status_code == 403
