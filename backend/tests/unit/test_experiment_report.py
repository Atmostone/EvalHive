"""Report assembly for SPA-40 experiments (pure build_report + helpers)."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.models.experiment import Experiment, ExperimentRun
from app.quality.experiment_report import (
    build_report,
    pareto_frontier,
    significance_matrix,
)


class TestParetoFrontier:
    def test_dominated_point_excluded(self):
        points = [
            {"config_key": "a", "quality": 8.0, "cost": 0.1, "time": 100},
            {"config_key": "b", "quality": 7.0, "cost": 0.2, "time": 200},  # dominated by a
            {"config_key": "c", "quality": 9.0, "cost": 0.5, "time": 300},  # better quality
        ]
        assert pareto_frontier(points) == ["a", "c"]

    def test_identical_points_both_on_frontier(self):
        points = [
            {"config_key": "a", "quality": 5.0, "cost": 0.1, "time": 10},
            {"config_key": "b", "quality": 5.0, "cost": 0.1, "time": 10},
        ]
        assert pareto_frontier(points) == ["a", "b"]

    def test_missing_quality_excluded(self):
        points = [
            {"config_key": "a", "quality": None, "cost": 0.0, "time": 0},
            {"config_key": "b", "quality": 1.0, "cost": 9.9, "time": 999},
        ]
        assert pareto_frontier(points) == ["b"]

    def test_empty(self):
        assert pareto_frontier([]) == []


class TestSignificanceMatrix:
    def test_separated_groups_significant(self):
        samples = {
            "cfg-01": {"weighted_score": [8.0, 8.2, 8.1, 7.9, 8.3]},
            "cfg-02": {"weighted_score": [5.0, 5.2, 5.1, 4.9, 5.3]},
        }
        entries = significance_matrix(samples)
        assert len(entries) == 1
        entry = entries[0]
        assert (entry["a"], entry["b"]) == ("cfg-01", "cfg-02")
        assert entry["significant"] is True
        assert entry["welch"]["p"] < 0.05
        assert entry["mann_whitney"]["approx"] is True

    def test_identical_distributions_not_significant(self):
        same = [6.0, 7.0, 8.0, 7.5, 6.5]
        entries = significance_matrix(
            {"cfg-01": {"weighted_score": same}, "cfg-02": {"weighted_score": list(same)}}
        )
        assert entries[0]["significant"] is False

    def test_insufficient_data_omitted(self):
        entries = significance_matrix(
            {"cfg-01": {"weighted_score": [1.0]}, "cfg-02": {"weighted_score": [2.0]}}
        )
        assert entries == []


def _exp(configs):
    return Experiment(
        configurations=configs,
        accumulated_cost_usd=Decimal("0.5"),
        budget_limit_usd=None,
    )


def _run(config_key, case_key, idx, *, status="success", score=None, traj=None,
         cost="0.01", duration=60, task_id=None):
    return ExperimentRun(
        config_key=config_key,
        case_key=case_key,
        run_index=idx,
        status=status,
        weighted_score=score,
        trajectory_score=traj,
        cost_usd=Decimal(cost),
        duration_seconds=duration,
        task_id=task_id or uuid.uuid4(),
    )


def _record(dimensions=None, failures=None):
    return SimpleNamespace(
        quality_profile={"dimensions": dimensions or []} if dimensions else None,
        failure_profile={"failures": failures} if failures else None,
    )


CONFIGS = [
    {"config_key": "cfg-01", "label": "fast", "orchestrator": False},
    {"config_key": "cfg-02", "label": "orch", "orchestrator": True},
]


def test_build_report_full_shape():
    runs, records = [], {}
    # cfg-01: strong scores; cfg-02: weaker + one failure.
    for case in ("case-a", "case-b"):
        for idx in range(3):
            r1 = _run("cfg-01", case, idx, score=8.0 + idx * 0.1, traj=7.5, cost="0.01")
            runs.append(r1)
            records[r1.task_id] = _record(
                dimensions=[
                    {"key": "correctness", "score": 8.0 + idx * 0.1},
                    {"key": "completeness", "score": 7.0},
                ]
            )
            r2 = _run("cfg-02", case, idx, score=5.0 + idx * 0.1, traj=5.5,
                      cost="0.05", duration=240)
            runs.append(r2)
            records[r2.task_id] = _record(
                dimensions=[{"key": "correctness", "score": 5.0 + idx * 0.1}]
            )
    failed = _run("cfg-02", "case-a", 3, status="failed", cost="0.02")
    runs.append(failed)
    records[failed.task_id] = _record(failures=[{"class": "tool_misuse"}])

    report = build_report(_exp(CONFIGS), runs, records, partial=False)

    assert report["schema_version"] == 1
    assert report["partial"] is False
    assert report["n_terminal_runs"] == 13

    summary = report["summary"]
    assert summary["total_runs"] == 13
    assert summary["success"] == 12
    assert summary["failed"] == 1
    per_config = {c["config_key"]: c for c in summary["per_config"]}
    assert per_config["cfg-01"]["success_rate"] == 1.0
    assert per_config["cfg-01"]["quality_mean"] > per_config["cfg-02"]["quality_mean"]

    heatmap = report["heatmap"]
    assert heatmap["dimensions"] == ["correctness", "completeness"]
    row1 = next(r for r in heatmap["rows"] if r["config_key"] == "cfg-01")
    assert row1["cells"]["correctness"]["n"] == 6
    assert row1["cells"]["correctness"]["mean"] == 8.1
    row2 = next(r for r in heatmap["rows"] if r["config_key"] == "cfg-02")
    assert row2["cells"]["completeness"]["n"] == 0

    pareto = report["pareto"]
    assert pareto["frontier"] == ["cfg-01"]  # better quality AND cheaper AND faster
    assert all("on_frontier" in p for p in pareto["points"])

    assert len(report["scatter"]) == 12
    assert {p["config_key"] for p in report["scatter"]} == {"cfg-01", "cfg-02"}

    leaderboard = report["leaderboard"]
    assert leaderboard["source"] == "derived_pointwise"
    assert leaderboard["status"] == "ok"
    assert leaderboard["players"][0]["player"] == "cfg-01"
    assert leaderboard["players"][0]["label"] == "fast"
    assert leaderboard["players"][0]["rank"] == 1

    sig = {(e["a"], e["b"], e["metric"]): e for e in report["significance"]}
    weighted = sig[("cfg-01", "cfg-02", "weighted_score")]
    assert weighted["significant"] is True

    failure = report["failure_modes"]["per_config"]
    cfg2 = next(f for f in failure if f["config_key"] == "cfg-02")
    assert cfg2["classes"] == {"tool_misuse": 1}
    assert cfg2["statuses"]["failed"] == 1

    orch = report["orchestrator"]
    assert orch["on"]["configs"] == ["cfg-02"]
    assert orch["off"]["configs"] == ["cfg-01"]
    assert orch["delta"]["quality_mean"] < 0  # orchestrator side scored lower
    assert orch["delta"]["cost_mean"] > 0


def test_build_report_empty_runs():
    report = build_report(_exp(CONFIGS), [], {}, partial=True)
    assert report["partial"] is True
    assert report["n_terminal_runs"] == 0
    assert report["summary"]["total_runs"] == 0
    assert report["pareto"]["frontier"] == []
    assert report["leaderboard"]["status"] == "empty"
    assert report["significance"] == []
    assert report["orchestrator"]["delta"] is None
