"""Integration tests for the Bias Mitigation Toolkit endpoints (E-18).

POST /api/quality/bias-report/run re-judges the calibration set with the
prompt-level mitigations OFF then ON (a controlled A/B that DOES call the judge)
and persists a versioned before/after report. GET /api/quality/bias-report reads
the latest (and, with history=true, the version history). The run endpoint is
owner/admin-only.

The fake judge returns a clustered, inflated score (8) with no mitigation and the
"true" per-dimension score when the mitigation instructions are present — so the
ON pass agrees with the humans and the OFF pass does not, deterministically.
"""

import json
import re
import uuid
from unittest.mock import MagicMock

from httpx import AsyncClient
from sqlalchemy import text

from app import database
from app.models.provider import LLMModel, Provider
from app.models.quality_record import QualityRecord
from app.models.rubric import Rubric
from app.models.task import Task, TaskStatus
from app.models.workspace import Workspace
from app.quality import judge as judge_mod
from app.quality.stats import score_to_band

# Six records. Humans rate correctness high (good) and tool_selection low (bad),
# and reject every task (because tool use is poor).
_CORRECTNESS = [9, 8, 9, 8, 9, 8]
_TOOL = [2, 3, 2, 3, 2, 3]
_SUBMITTERS = ["alice@x.com", "bob@x.com"]


def _resp(score, pt=10, ct=4):
    fn = MagicMock()
    fn.arguments = json.dumps({"score": score, "reasoning": "ok"})
    tc = MagicMock()
    tc.function = fn
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(tool_calls=[tc]))]
    resp.usage = {"prompt_tokens": pt, "completion_tokens": ct}
    return resp


class _ABProvider:
    """OFF (no mitigation) → clustered 8 for every dimension. ON (mitigation
    instructions present) → the true score encoded in the task's result_summary."""

    async def acompletion(self, **kwargs):
        system = kwargs["messages"][0]["content"]
        user = kwargs["messages"][1]["content"]
        dim_name = user.split("\n", 1)[0].split("Dimension:", 1)[1].strip()
        mitigated = "full 0-10 range" in system
        if mitigated:
            targets = {m.group(1): int(m.group(2)) for m in re.finditer(r"([A-Za-z ]+)=(\d+)", user)}
            score = targets.get(dim_name, 8)
        else:
            score = 8
        return _resp(score)


async def _seed_rubric(ws):
    async with database.async_session() as s:
        await s.execute(
            text("UPDATE rubrics SET is_default=false WHERE workspace_id=:w"), {"w": str(ws)}
        )
        s.add(
            Rubric(
                workspace_id=ws,
                name="Bias Test",
                is_default=True,
                dimensions=[
                    {"key": "correctness", "name": "Correctness", "evaluator": "judge",
                     "weight": 1.0, "threshold": 6, "critical": True},
                    {"key": "tool_selection", "name": "Tool Selection", "evaluator": "judge",
                     "weight": 1.0, "threshold": 6, "critical": True},
                ],
            )
        )
        await s.commit()


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


async def _seed_records(ws, *, agent_model="doer-m"):
    async with database.async_session() as s:
        for i in range(len(_CORRECTNESS)):
            c, t = _CORRECTNESS[i], _TOOL[i]
            # The result encodes the true per-dimension scores for the ON pass.
            summary = f"Correctness={c};Tool Selection={t}"
            task = Task(title=f"t{i}", status=TaskStatus.DONE.value, workspace_id=ws,
                        result_summary=summary, model_used=agent_model)
            s.add(task)
            await s.flush()
            s.add(QualityRecord(
                task_id=task.id, workspace_id=ws, model_used=agent_model,
                final_status=TaskStatus.DONE.value,
                quality_profile=None,
                human_feedback={
                    "schema_version": 1,
                    "verdict": "reject",
                    "dimensions": [
                        {"key": "correctness", "name": "Correctness", "score": c,
                         "band": score_to_band(c)},
                        {"key": "tool_selection", "name": "Tool Selection", "score": t,
                         "band": score_to_band(t)},
                    ],
                    "submitted_by": _SUBMITTERS[i % 2],
                    "submitted_at": "2026-05-30T00:00:00",
                },
            ))
        await s.commit()


async def test_run_history_and_before_after(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_rubric(ws)
    await _seed_judge_model(ws)
    await _seed_records(ws)
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _ABProvider())

    # run #1
    r = await auth_client.post("/api/quality/bias-report/run")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["version"] == 1
    assert report["judge_config_key"] == "judge-m"
    assert report["sample_size"] == 12  # 6 records × 2 dims
    assert report["passed"] is True

    m = report["metrics"]
    assert m["status"] == "ok"
    assert m["n_records"] == 6
    # Mitigation improved overall agreement with humans.
    assert m["overall_delta"]["cohen_kappa_after"] > m["overall_delta"]["cohen_kappa_before"]
    assert m["overall_delta"]["improved"] is True
    assert m["before"] is not None and m["after"] is not None

    delta = {d["key"]: d for d in m["dimensions_delta"]}
    assert delta["tool_selection"]["improved"] is True
    assert delta["tool_selection"]["cohen_kappa_after"] > delta["tool_selection"]["cohen_kappa_before"]

    diags = m["diagnostics"]
    assert diags["position_bias"]["status"] == "n/a"
    assert "E-21" in diags["position_bias"]["reason"]
    assert diags["self_preference"]["flagged"] is False  # judge-m != doer-m
    assert diags["score_clustering"]["clustered_off"] is True
    assert diags["score_clustering"]["improved"] is True

    # run #2 → next version
    r = await auth_client.post("/api/quality/bias-report/run")
    assert r.status_code == 200
    assert r.json()["version"] == 2

    # latest + history (newest first)
    r = await auth_client.get("/api/quality/bias-report")
    assert r.json()["version"] == 2
    r = await auth_client.get("/api/quality/bias-report?history=true")
    body = r.json()
    assert body["latest"]["version"] == 2
    assert [h["version"] for h in body["history"]] == [2, 1]


async def test_self_preference_flagged_when_judge_is_agent(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_rubric(ws)
    await _seed_judge_model(ws, api_name="judge-m")
    await _seed_records(ws, agent_model="judge-m")  # judge == agent
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _ABProvider())

    r = await auth_client.post("/api/quality/bias-report/run")
    assert r.status_code == 200, r.text
    sp = r.json()["metrics"]["diagnostics"]["self_preference"]
    assert sp["flagged"] is True
    assert sp["n_self_judged"] == 6
    assert "inflated" in sp["warning"]


async def test_run_empty_when_no_feedback(auth_client: AsyncClient, monkeypatch):
    ws = uuid.UUID(auth_client.headers["X-Workspace-Id"])
    await _seed_rubric(ws)
    await _seed_judge_model(ws)
    monkeypatch.setattr(judge_mod, "get_llm_provider", lambda: _ABProvider())

    r = await auth_client.post("/api/quality/bias-report/run")
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["metrics"]["status"] == "empty"
    assert report["passed"] is False
    assert report["sample_size"] == 0


async def test_get_none_when_never_run(auth_client: AsyncClient):
    r = await auth_client.get("/api/quality/bias-report")
    assert r.status_code == 200
    assert r.json() is None


async def test_run_requires_admin(auth_client: AsyncClient):
    ws = auth_client.headers["X-Workspace-Id"]
    async with database.async_session() as s:
        await s.execute(text(
            "UPDATE workspace_members SET role='member' WHERE workspace_id=:w"
        ), {"w": ws})
        await s.commit()

    r = await auth_client.post("/api/quality/bias-report/run")
    assert r.status_code == 403
