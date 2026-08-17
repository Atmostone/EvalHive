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
            {"config_key": "a", "quality": 8.0, "cost": 0.1, "effort": 100},
            {"config_key": "b", "quality": 7.0, "cost": 0.2, "effort": 200},  # dominated by a
            {"config_key": "c", "quality": 9.0, "cost": 0.5, "effort": 300},  # better quality
        ]
        assert pareto_frontier(points) == ["a", "c"]

    def test_identical_points_both_on_frontier(self):
        points = [
            {"config_key": "a", "quality": 5.0, "cost": 0.1, "effort": 10},
            {"config_key": "b", "quality": 5.0, "cost": 0.1, "effort": 10},
        ]
        assert pareto_frontier(points) == ["a", "b"]

    def test_missing_quality_excluded(self):
        points = [
            {"config_key": "a", "quality": None, "cost": 0.0, "effort": 0},
            {"config_key": "b", "quality": 1.0, "cost": 9.9, "effort": 999},
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
         cost="0.01", duration=60, task_id=None, external_verdict=None,
         failure_type=None):
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
        external_verdict=external_verdict,
        failure_type=failure_type,
    )


def _record(dimensions=None, failures=None, trajectory_axes=None, trajectory_match=None,
            human_feedback=None, cost_usd="0", quality_cost=0.0, trajectory_cost=0.0,
            gate=None, loop_detected=False, trace_stats=None, loop_analysis=None,
            input_tokens=None, output_tokens=None, tool_call_count=None):
    quality_profile = None
    if dimensions or quality_cost or gate:
        quality_profile = {"dimensions": dimensions or [], "judge_cost_usd": quality_cost}
        if gate is not None:
            quality_profile["gate"] = gate
    trajectory_profile = None
    if trajectory_axes is not None or loop_detected or trace_stats or loop_analysis:
        trajectory_profile = {
            "status": "scored",
            "axes": trajectory_axes or [],
            "judge_cost_usd": trajectory_cost,
            "loop_detected": loop_detected,
        }
        if trace_stats is not None:
            trajectory_profile["trace_stats"] = trace_stats
        if loop_analysis is not None:
            trajectory_profile["loop_analysis"] = loop_analysis
    return SimpleNamespace(
        cost_usd=Decimal(str(cost_usd)),
        quality_profile=quality_profile,
        failure_profile={"failures": failures} if failures else None,
        trajectory_profile=trajectory_profile,
        trajectory_match_profile=trajectory_match,
        trajectory_evidence_profile=None,
        hallucination_profile=None,
        human_feedback=human_feedback,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tool_call_count=tool_call_count,
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
                ],
                trajectory_axes=[
                    {"key": "efficiency", "name": "Efficiency", "score": 7.0},
                    {"key": "tool_selection", "name": "Tool selection", "score": 8.0},
                ],
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

    assert report["schema_version"] == 16
    assert report["partial"] is False
    assert report["n_terminal_runs"] == 13
    # No executable verdicts here → external/rq2 present but unavailable.
    assert report["external"]["available"] is False
    assert report["rq2"]["available"] is False
    # v2: trajectory heatmap (E-07 axes) + trajectory match (E-09) blocks present
    assert "axes" in report["trajectory_heatmap"]
    assert "per_config" in report["trajectory_match"]
    assert report["trajectory_match"]["available"] is False  # no canonical trajectories here

    summary = report["summary"]
    assert summary["total_runs"] == 13
    assert summary["success"] == 12
    assert summary["failed"] == 1
    per_config = {c["config_key"]: c for c in summary["per_config"]}
    assert per_config["cfg-01"]["success_rate"] == 1.0
    assert per_config["cfg-01"]["quality_mean"] > per_config["cfg-02"]["quality_mean"]

    heatmap = report["heatmap"]
    assert heatmap["dimensions"] == ["correctness", "completeness"]
    # dimension_labels falls back to the key when the profile carries no name
    assert heatmap["dimension_labels"]["correctness"] == "correctness"
    # No gate in these profiles; cfg-01 trajectory-scored so loop_detection is live
    assert report["quality_gate"]["available"] is False
    assert report["loop_detection"]["available"] is True
    # LLM loop signal present, but no deterministic loop_analysis on these records
    assert report["loop_detection"]["structural_available"] is False
    # No trace_stats in these profiles; longitudinal has >1 repetition (idx 0/1/2/3)
    assert report["trace_stats"]["available"] is False
    assert report["longitudinal"]["available"] is True
    assert [p["run_index"] for p in report["longitudinal"]["points"]] == [0, 1, 2, 3]
    row1 = next(r for r in heatmap["rows"] if r["config_key"] == "cfg-01")
    assert row1["cells"]["correctness"]["n"] == 6
    assert row1["cells"]["correctness"]["mean"] == 8.1
    row2 = next(r for r in heatmap["rows"] if r["config_key"] == "cfg-02")
    assert row2["cells"]["completeness"]["n"] == 0

    traj_hm = report["trajectory_heatmap"]
    assert "efficiency" in traj_hm["axes"] and "tool_selection" in traj_hm["axes"]
    row1t = next(r for r in traj_hm["rows"] if r["config_key"] == "cfg-01")
    assert row1t["cells"]["efficiency"]["n"] == 6
    assert row1t["cells"]["efficiency"]["mean"] == 7.0
    assert row1t["overall_score"]["mean"] is not None

    # SPA-76: no human calibration passed and no deterministic loop_analysis on
    # these records → every axis is an honest 'not_calibrated' (never fabricated).
    # v11: the judge loop_detection axis is retired (counter SPA-75 carries it).
    ar = report["axis_reliability"]
    assert ar["available"] is False
    assert set(ar["axes"]) == {"efficiency", "tool_selection", "parameter_quality",
                               "error_recovery", "goal_alignment"}
    assert "loop_detection" not in ar["axes"]
    assert all(a["status"] == "not_calibrated" and a["source"] == "none"
               for a in ar["axes"].values())

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
    assert cfg2["class_reasons"] == {}  # failure carried no reason text
    assert cfg2["statuses"]["failed"] == 1

    orch = report["orchestrator"]
    assert orch["on"]["configs"] == ["cfg-02"]
    assert orch["off"]["configs"] == ["cfg-01"]
    assert orch["delta"]["quality_mean"] < 0  # orchestrator side scored lower
    assert orch["delta"]["cost_mean"] > 0


def test_build_report_effort_token_difficulty():
    # Two cases (easy 'case-a', token-heavy 'case-b'); cfg-02 spends ~2× the tokens
    # of cfg-01 on BOTH. SPA-77 difficulty-normalisation (tokens ÷ per-case median)
    # exposes cfg-02 as consistently heavier (rel_effort 1.33 vs 0.67) even though
    # 'case-b' is intrinsically token-heavy. Cost is $0 → token fallback.
    toks = {("cfg-01", "case-a"): 100, ("cfg-01", "case-b"): 1000,
            ("cfg-02", "case-a"): 200, ("cfg-02", "case-b"): 2000}
    runs, records = [], {}
    for (cfg, case), t in toks.items():
        r = _run(cfg, case, 0, score=8.0, traj=7.0, cost="0")
        runs.append(r)
        records[r.task_id] = _record(input_tokens=t, output_tokens=0, tool_call_count=5)

    report = build_report(_exp(CONFIGS), runs, records, partial=False)

    eff = report["effort"]
    assert eff["available"] is True
    assert eff["cost_available"] is False  # all $0 → tokens are the only effort signal
    assert eff["primary"] == "tokens"
    by = {e["config_key"]: e for e in eff["per_config"]}
    assert by["cfg-01"]["tokens_mean"] == 550.0
    assert by["cfg-02"]["tokens_mean"] == 1100.0
    assert by["cfg-01"]["steps_mean"] == 5.0
    # difficulty-normalised: cfg-01 below the per-case median (0.667), cfg-02 above (1.333)
    assert by["cfg-01"]["rel_effort"] == 0.6667
    assert by["cfg-02"]["rel_effort"] == 1.3333
    # surfaced in the Summary table rows as well
    sc = {c["config_key"]: c for c in report["summary"]["per_config"]}
    assert sc["cfg-01"]["tokens_mean"] == 550.0 and sc["cfg-01"]["rel_effort"] == 0.6667
    # Pareto bubble/frontier is token effort (not wall-clock); scatter carries tokens
    assert all("effort" in p for p in report["pareto"]["points"])
    assert all("tokens" in s for s in report["scatter"])
    assert all("tokens_mean" in p for p in report["longitudinal"]["points"])


def test_build_report_empty_runs():
    report = build_report(_exp(CONFIGS), [], {}, partial=True)
    assert report["partial"] is True
    assert report["n_terminal_runs"] == 0
    assert report["summary"]["total_runs"] == 0
    assert report["pareto"]["frontier"] == []
    assert report["leaderboard"]["status"] == "empty"
    assert report["significance"] == []
    assert report["orchestrator"]["delta"] is None


def test_build_report_external_pass_rate_and_rq2():
    # cfg-01: checker passes all 3; judge high on 2, low on 1 (pass_high=2, pass_low=1).
    # cfg-02: checker fails both; judge high on 1 (over-credit), low on 1.
    runs = [
        _run("cfg-01", "case-a", 0, score=8.0, external_verdict=True),
        _run("cfg-01", "case-a", 1, score=7.0, external_verdict=True),
        _run("cfg-01", "case-b", 0, score=3.0, external_verdict=True),
        _run("cfg-02", "case-a", 0, score=8.0, external_verdict=False),
        _run("cfg-02", "case-b", 0, score=2.0, external_verdict=False),
        # No verdict / no score → excluded from both views.
        _run("cfg-01", "case-c", 0, score=9.0, external_verdict=None),
        _run("cfg-02", "case-c", 0, score=None, external_verdict=True),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)
    assert report["schema_version"] == 16

    ext = report["external"]
    assert ext["available"] is True
    by = {c["config_key"]: c for c in ext["per_config"]}
    assert (by["cfg-01"]["n_evaluated"], by["cfg-01"]["n_pass"], by["cfg-01"]["pass_rate"]) == (3, 3, 1.0)
    # cfg-02 has 2 fails + 1 pass-without-score → 3 evaluated, 1 passed.
    assert (by["cfg-02"]["n_evaluated"], by["cfg-02"]["n_pass"]) == (3, 1)

    rq2 = report["rq2"]
    assert rq2["available"] is True
    assert rq2["judge_threshold"] == 5.0
    # Only runs with BOTH verdict and score count (5 of them).
    assert rq2["overall"]["cells"] == {"pass_high": 2, "pass_low": 1, "fail_high": 1, "fail_low": 1}
    assert rq2["overall"]["n"] == 5
    assert rq2["overall"]["agreement"] == 0.6  # (pass_high + fail_low) / n = 3/5


def test_build_report_human_feedback_aggregate():
    # cfg-01: two annotated runs — one SUCCESS/approve, one FAILED/reject. The
    # reject must be counted: human aggregation is NOT success-only (else the
    # verdict distribution would drop exactly the rejects it is about).
    r1 = _run("cfg-01", "case-a", 0, status="success", score=8.0)
    r2 = _run("cfg-01", "case-a", 1, status="failed", score=None)
    r3 = _run("cfg-02", "case-a", 0, status="success", score=5.0)
    runs = [r1, r2, r3]
    records = {
        r1.task_id: _record(human_feedback={
            "verdict": "approve",
            "dimensions": [
                {"key": "accuracy", "name": "Accuracy", "score": 9},
                {"key": "clarity", "name": "Clarity", "score": 7},
            ],
        }),
        r2.task_id: _record(human_feedback={
            "verdict": "reject",
            "dimensions": [{"key": "accuracy", "name": "Accuracy", "score": 3}],
        }),
        r3.task_id: _record(),  # no human feedback
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    hf = report["human_feedback"]
    assert hf["available"] is True
    assert hf["dimensions"] == ["accuracy", "clarity"]
    assert hf["dimension_labels"]["accuracy"] == "Accuracy"
    row1 = next(r for r in hf["rows"] if r["config_key"] == "cfg-01")
    # accuracy averaged over BOTH runs (9, 3) → mean 6.0, n 2, σ 3.0
    assert row1["cells"]["accuracy"]["n"] == 2
    assert row1["cells"]["accuracy"]["mean"] == 6.0
    assert row1["cells"]["accuracy"]["std"] == 3.0
    # clarity only on the success run
    assert row1["cells"]["clarity"]["n"] == 1
    # per-run overall = mean of that run's dims: (9+7)/2=8.0 and 3.0 → config 5.5
    assert row1["overall_score"]["mean"] == 5.5
    assert row1["overall_score"]["n"] == 2
    assert row1["n_rated"] == 2
    assert row1["verdicts"] == {"approve": 1, "reject": 1, "none": 0}
    # cfg-02 had no human feedback → empty row, still present
    row2 = next(r for r in hf["rows"] if r["config_key"] == "cfg-02")
    assert row2["n_rated"] == 0
    assert row2["cells"]["accuracy"]["n"] == 0


def test_build_report_checker_human_agreement():
    # v12: pair the executable-checker verdict (external_verdict) with the human
    # gold verdict. 4 runs cover the 2×2; a 5th has a checker verdict but no human
    # verdict (excluded).
    runs, records = [], {}
    for i, (ev, hv) in enumerate(
        [(True, "approve"), (True, "reject"), (False, "reject"), (False, "approve")]
    ):
        r = _run("cfg-01", f"case-{i}", 0, status="success", score=7.0, external_verdict=ev)
        runs.append(r)
        records[r.task_id] = _record(human_feedback={"verdict": hv, "dimensions": []})
    r5 = _run("cfg-01", "case-x", 0, status="success", score=6.0, external_verdict=True)
    runs.append(r5)
    records[r5.task_id] = _record()  # checker verdict but no human → excluded

    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    ch = report["checker_human"]
    assert ch["available"] is True
    assert ch["n"] == 4
    assert ch["cells"] == {
        "pass_approve": 1, "pass_reject": 1, "fail_approve": 1, "fail_reject": 1,
    }
    # agreement = (pass_approve + fail_reject) / n = 2/4
    assert ch["agreement"] == 0.5
    # κ on {both_yes=1, a_only=1, b_only=1, both_no=1}: po=.5, pe=.5 → 0
    assert ch["kappa"] == 0.0


def test_build_report_cost_breakdown():
    r1 = _run("cfg-01", "case-a", 0, status="success", cost="0.10")
    r2 = _run("cfg-02", "case-a", 0, status="success", cost="0.20")
    runs = [r1, r2]
    records = {
        r1.task_id: _record(cost_usd="0.10", quality_cost=0.02, trajectory_cost=0.01,
                            dimensions=[{"key": "a", "score": 8}],
                            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 7}]),
        r2.task_id: _record(cost_usd="0.20", quality_cost=0.05, trajectory_cost=0.03,
                            dimensions=[{"key": "a", "score": 5}],
                            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 5}]),
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    cb = report["cost_breakdown"]
    assert cb["available"] is True
    by = {c["config_key"]: c for c in cb["per_config"]}
    assert by["cfg-01"]["agent"] == 0.10
    assert by["cfg-01"]["judge_outcome"] == 0.02
    assert by["cfg-01"]["judge_trajectory"] == 0.01
    assert by["cfg-01"]["judge_total"] == 0.03
    assert by["cfg-01"]["total"] == 0.13
    assert by["cfg-02"]["judge_evidence"] == 0.0  # E-08 off → zero (column hidden in UI)
    totals = cb["totals"]
    assert totals["agent"] == 0.30
    assert totals["judge_outcome"] == 0.07
    assert totals["judge_trajectory"] == 0.04
    assert totals["total"] == 0.41


def test_build_report_no_human_no_cost():
    report = build_report(_exp(CONFIGS), [], {}, partial=True)
    assert report["human_feedback"]["available"] is False
    assert report["human_feedback"]["dimensions"] == []
    assert report["human_feedback"]["rows"][0]["n_rated"] == 0
    assert report["cost_breakdown"]["available"] is False
    assert report["cost_breakdown"]["totals"]["total"] == 0
    # New Tier-1 aggregates degrade to empty-state, not absent.
    assert report["quality_gate"]["available"] is False
    assert report["loop_detection"]["available"] is False
    assert report["heatmap"]["dimension_labels"] == {}
    assert report["trace_stats"]["available"] is False
    assert report["longitudinal"]["available"] is False
    assert report["longitudinal"]["points"] == []


def test_build_report_trace_stats():
    r1 = _run("cfg-01", "case-a", 0, status="success", traj=7.0)
    r2 = _run("cfg-01", "case-a", 1, status="success", traj=7.0)
    r3 = _run("cfg-02", "case-a", 0, status="success", traj=6.0)
    runs = [r1, r2, r3]
    records = {
        r1.task_id: _record(trace_stats={"steps_total": 10, "cleaned_tokens": 200, "original_tokens": 1000}),
        r2.task_id: _record(trace_stats={"steps_total": 20, "cleaned_tokens": 300, "original_tokens": 1000}),
        r3.task_id: _record(),  # no trace stats
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    ts = report["trace_stats"]
    assert ts["available"] is True
    by = {c["config_key"]: c for c in ts["per_config"]}
    assert by["cfg-01"]["n"] == 2
    assert by["cfg-01"]["steps_mean"] == 15.0
    # compression = sum(cleaned)/sum(original) = 500/2000 = 0.25
    assert by["cfg-01"]["compression"] == 0.25
    assert by["cfg-02"]["n"] == 0
    assert by["cfg-02"]["compression"] is None


def test_build_report_longitudinal():
    # Three repetitions of one cell; quality climbs with the run index.
    runs = [
        _run("cfg-01", "case-a", 0, status="success", score=6.0, traj=7.0, cost="0.01"),
        _run("cfg-01", "case-a", 1, status="success", score=7.0, traj=7.0, cost="0.02"),
        _run("cfg-01", "case-a", 2, status="failed", score=None, cost="0.03"),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)
    lng = report["longitudinal"]
    assert lng["available"] is True
    pts = {p["run_index"]: p for p in lng["points"]}
    assert pts[0]["quality_mean"] == 6.0 and pts[0]["n"] == 1
    assert pts[1]["quality_mean"] == 7.0
    # the failed run carries no score → quality_mean None, but still counts + costs
    assert pts[2]["quality_mean"] is None
    assert pts[2]["cost_mean"] == 0.03


def test_build_report_quality_gate():
    # The gate verdict is carried by BOTH the success and the failed run (it is a
    # verdict on the RESULT, not the run status), so both count toward the rate.
    r1 = _run("cfg-01", "case-a", 0, status="success", score=8.0)
    r2 = _run("cfg-01", "case-a", 1, status="failed", score=3.0)
    r3 = _run("cfg-02", "case-a", 0, status="success", score=6.0)
    runs = [r1, r2, r3]
    records = {
        r1.task_id: _record(
            dimensions=[{"key": "correctness", "score": 8}],
            gate={"passed": True, "failed_dimensions": []},
        ),
        r2.task_id: _record(
            dimensions=[{"key": "correctness", "score": 3}],
            gate={"passed": False, "failed_dimensions": ["correctness"]},
        ),
        r3.task_id: _record(
            dimensions=[{"key": "correctness", "score": 6}],
            gate={"passed": True, "failed_dimensions": []},
        ),
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    qg = report["quality_gate"]
    assert qg["available"] is True
    by = {c["config_key"]: c for c in qg["per_config"]}
    assert (by["cfg-01"]["n"], by["cfg-01"]["n_pass"], by["cfg-01"]["pass_rate"]) == (2, 1, 0.5)
    assert by["cfg-01"]["failed_dimensions"] == {"correctness": 1}
    assert (by["cfg-02"]["n"], by["cfg-02"]["n_pass"], by["cfg-02"]["pass_rate"]) == (1, 1, 1.0)
    assert by["cfg-02"]["failed_dimensions"] == {}


def test_build_report_loop_detection():
    # A FAILED run that looped must still count — looping is often what caused the
    # failure, so a success-only rate would hide the signal where it matters.
    r1 = _run("cfg-01", "case-a", 0, status="success", traj=7.0)
    r2 = _run("cfg-01", "case-a", 1, status="failed", traj=2.0)
    r3 = _run("cfg-02", "case-a", 0, status="success", traj=8.0)
    runs = [r1, r2, r3]
    records = {
        r1.task_id: _record(
            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 7}],
            loop_detected=False,
        ),
        r2.task_id: _record(
            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 2}],
            loop_detected=True,
        ),
        r3.task_id: _record(
            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 8}],
            loop_detected=False,
        ),
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    ld = report["loop_detection"]
    assert ld["available"] is True
    assert ld["structural_available"] is False  # no loop_analysis on these records
    by = {c["config_key"]: c for c in ld["per_config"]}
    assert (by["cfg-01"]["n_scored"], by["cfg-01"]["n_loop"], by["cfg-01"]["loop_rate"]) == (2, 1, 0.5)
    assert (by["cfg-02"]["n_scored"], by["cfg-02"]["n_loop"], by["cfg-02"]["loop_rate"]) == (1, 0, 0.0)


def test_build_report_loop_detection_structural_anchor():
    # Two trajectory-scored runs carry BOTH the LLM loop badge and the deterministic
    # loop_analysis. The deterministic rate sits next to the judge rate, and the
    # agreement is the judge↔counted match.
    r1 = _run("cfg-01", "case-a", 0, status="success", traj=7.0)
    r2 = _run("cfg-01", "case-a", 1, status="failed", traj=2.0)
    runs = [r1, r2]
    records = {
        # judge says no loop, counter agrees (no loop) → agree
        r1.task_id: _record(
            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 7}],
            loop_detected=False,
            loop_analysis={"loop_detected": False, "max_repeat_run": 1},
        ),
        # judge says no loop, but the counter FOUND a real loop → judge under-called
        r2.task_id: _record(
            trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": 6}],
            loop_detected=False,
            loop_analysis={"loop_detected": True, "max_repeat_run": 5},
        ),
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    ld = report["loop_detection"]
    assert ld["structural_available"] is True
    by = {c["config_key"]: c for c in ld["per_config"]}
    cfg1 = by["cfg-01"]
    # LLM badge: 0/2 looped; deterministic: 1/2 looped (caught the one the judge missed)
    assert (cfg1["n_loop"], cfg1["loop_rate"]) == (0, 0.0)
    assert (cfg1["n_structural"], cfg1["n_structural_loop"], cfg1["structural_loop_rate"]) == (2, 1, 0.5)
    # directional split: judge never over-called; counter found 1 the judge missed
    assert cfg1["n_judge_only"] == 0
    assert cfg1["n_counter_only"] == 1
    # agreement: 1 of 2 runs agree (the no-loop one) → 0.5
    assert cfg1["agreement"] == 0.5
    # κ on {both_loop=0, judge_only=0, counter_only=1, both_clean=1}: po=.5, pe=.5 → 0
    assert cfg1["kappa"] == 0.0
    assert ld["agreement"] == 0.5
    assert ld["n_counter_only"] == 1 and ld["n_judge_only"] == 0
    assert ld["kappa"] == 0.0
    assert ld["n_structural"] == 2
    # v11: the judge loop_detection axis is retired from axis_reliability — the SPA-75
    # counter still carries the loop signal in the Loop detection section (asserted
    # above), but it no longer badges a displayed E-07 axis. With no human calibration
    # and loop gone, no real reliability source remains.
    ar = report["axis_reliability"]
    assert "loop_detection" not in ar["axes"]
    assert ar["available"] is False
    assert ar["axes"]["efficiency"]["status"] == "not_calibrated"


def test_classify_reliability_buckets():
    from app.quality.experiment_report import _classify_reliability

    assert _classify_reliability(0.7, 10, has_source=True) == "reliable"
    assert _classify_reliability(0.6, 10, has_source=True) == "reliable"  # boundary
    assert _classify_reliability(0.5, 10, has_source=True) == "directional"
    assert _classify_reliability(0.4, 10, has_source=True) == "directional"  # boundary
    assert _classify_reliability(0.39, 10, has_source=True) == "unreliable"
    assert _classify_reliability(-0.1, 10, has_source=True) == "unreliable"
    assert _classify_reliability(0.9, 2, has_source=True) == "directional"  # too few pairs
    assert _classify_reliability(None, 10, has_source=True) == "directional"  # undefined κ
    assert _classify_reliability(0.9, 10, has_source=False) == "not_calibrated"


def test_classify_reliability_rank_rescue():
    # v13: κ below the bar but ranks agreeing (Spearman ρ≥0.5) → directional, not
    # unreliable — a scale-shifted judge is usable for comparisons only.
    from app.quality.experiment_report import _classify_reliability

    assert _classify_reliability(0.0, 10, has_source=True, rho=0.93) == "directional"
    assert _classify_reliability(0.0, 10, has_source=True, rho=0.5) == "directional"  # boundary
    assert _classify_reliability(0.0, 10, has_source=True, rho=0.49) == "unreliable"
    assert _classify_reliability(0.0, 10, has_source=True, rho=None) == "unreliable"
    assert _classify_reliability(0.0, 10, has_source=True, rho=-0.2) == "unreliable"
    # ranks never DEMOTE: κ above the bar stays what κ says
    assert _classify_reliability(0.7, 10, has_source=True, rho=0.1) == "reliable"
    assert _classify_reliability(0.5, 10, has_source=True, rho=0.1) == "directional"
    # too few pairs still wins over the rank rescue
    assert _classify_reliability(0.0, 2, has_source=True, rho=0.9) == "directional"


def test_outcome_axis_reliability():
    from app.quality.experiment_report import _outcome_axis_reliability

    calibration = {
        "available": True,
        "dimensions": [
            # outcome rubric dims
            {"key": "task_completion", "name": "Task completion", "n": 39, "cohen_kappa": 0.24, "spearman": 0.62},
            {"key": "format_compliance", "name": "Format compliance", "n": 39, "cohen_kappa": 0.03, "spearman": 0.18},
            {"key": "readability", "name": "Readability", "n": 153, "cohen_kappa": 0.49, "spearman": 0.60},
            # trajectory dim must be excluded here (badged by _axis_reliability)
            {"key": "tool_selection", "name": "Tool selection", "n": 11, "cohen_kappa": 0.0, "spearman": 0.93},
        ],
    }
    oar = _outcome_axis_reliability(calibration)
    ax = oar["axes"]
    assert oar["available"] is True
    assert "tool_selection" not in ax
    assert ax["task_completion"]["status"] == "directional"  # rank-rescued (ρ=0.62)
    assert ax["format_compliance"]["status"] == "unreliable"  # low κ AND low ρ
    assert ax["readability"]["status"] == "directional"  # κ in 0.4–0.6 band
    assert ax["task_completion"]["rho"] == 0.62

    # no calibration → honest empty state (axes dict empty: rubric-dependent list)
    oar2 = _outcome_axis_reliability(None)
    assert oar2["available"] is False
    assert oar2["axes"] == {}


def test_axis_reliability_sources_and_priority():
    from app.quality.experiment_report import _axis_reliability

    calibration = {
        "available": True,
        "dimensions": [
            {"key": "efficiency", "name": "Efficiency", "n": 10, "cohen_kappa": 0.72},
            {"key": "tool_selection", "name": "Tool selection", "n": 10, "cohen_kappa": 0.45},
            {"key": "parameter_quality", "name": "Parameter quality", "n": 10, "cohen_kappa": 0.10},
            {"key": "error_recovery", "name": "Error recovery", "n": 2, "cohen_kappa": None},
            # goal_alignment absent → no human source
            {"key": "loop_detection", "name": "Loop detection", "n": 12, "cohen_kappa": 0.05},
        ],
    }
    loop_detection = {"structural_available": True, "kappa": 0.33, "n_structural": 50}
    ar = _axis_reliability(calibration, loop_detection, {})
    ax = ar["axes"]
    assert ar["available"] is True
    assert (ax["efficiency"]["status"], ax["efficiency"]["source"]) == ("reliable", "human")
    assert (ax["tool_selection"]["status"], ax["tool_selection"]["source"]) == ("directional", "human")
    assert (ax["parameter_quality"]["status"], ax["parameter_quality"]["source"]) == ("unreliable", "human")
    # human dim exists but n=2 < MIN_SAMPLES → directional (insufficient), still human-sourced
    assert (ax["error_recovery"]["status"], ax["error_recovery"]["source"]) == ("directional", "human")
    assert (ax["goal_alignment"]["status"], ax["goal_alignment"]["source"]) == ("not_calibrated", "none")
    # v11: the judge loop_detection axis is retired — never badged, even with a human κ.
    assert "loop_detection" not in ax

    # The structural loop anchor no longer surfaces a displayed axis (v11).
    cal2 = {"available": True, "dimensions": [
        {"key": "efficiency", "name": "Efficiency", "n": 10, "cohen_kappa": 0.72}]}
    ar2 = _axis_reliability(cal2, loop_detection, {})
    assert "loop_detection" not in ar2["axes"]
    # a non-loop axis with no human source stays not_calibrated even when a loop anchor exists
    assert ar2["axes"]["goal_alignment"]["status"] == "not_calibrated"

    # Nothing at all → honest empty state.
    ar3 = _axis_reliability(None, {"structural_available": False}, {})
    assert ar3["available"] is False
    assert all(a["status"] == "not_calibrated" for a in ar3["axes"].values())


def test_build_report_failure_reasons():
    # E-14 reasons are surfaced (top-3 per class, highest-confidence first, deduped).
    r1 = _run("cfg-01", "case-a", 0, status="failed")
    r2 = _run("cfg-01", "case-b", 0, status="failed")
    runs = [r1, r2]
    records = {
        r1.task_id: _record(failures=[
            {"class": "loop", "confidence": 0.9, "reason": "repeated the same search 5x"},
        ]),
        r2.task_id: _record(failures=[
            {"class": "loop", "confidence": 0.6, "reason": "stuck refreshing the page"},
            {"class": "premature_stop", "confidence": 0.7, "reason": "stopped before writing the file"},
        ]),
    }
    report = build_report(_exp(CONFIGS), runs, records, partial=False)
    fm = next(f for f in report["failure_modes"]["per_config"] if f["config_key"] == "cfg-01")
    assert fm["classes"] == {"loop": 2, "premature_stop": 1}
    loop_reasons = fm["class_reasons"]["loop"]
    assert [x["reason"] for x in loop_reasons] == [
        "repeated the same search 5x",
        "stuck refreshing the page",
    ]
    assert loop_reasons[0]["confidence"] == 0.9
    assert fm["class_reasons"]["premature_stop"][0]["reason"] == "stopped before writing the file"


# --- the judge↔checker headline needs no threshold (SPA-87) ------------------


def _rq2_runs():
    """Checker passes score 8/7/3; checker fails score 8 (over-credit) and 2."""
    return [
        _run("cfg-01", "case-a", 0, score=8.0, external_verdict=True),
        _run("cfg-01", "case-a", 1, score=7.0, external_verdict=True),
        _run("cfg-01", "case-b", 0, score=3.0, external_verdict=True),
        _run("cfg-02", "case-a", 0, score=8.0, external_verdict=False),
        _run("cfg-02", "case-b", 0, score=2.0, external_verdict=False),
    ]


def test_judge_discrimination_is_the_primary_and_has_no_threshold():
    report = build_report(_exp(CONFIGS), _rq2_runs(), {}, partial=False)
    disc = report["judge_discrimination"]
    assert disc["available"] is True and disc["primary"] is True

    overall = disc["overall"]
    # Nothing in this block is a cut-off — that is the whole claim.
    assert not [k for k in overall if "threshold" in k]
    assert (overall["n_checker_pass"], overall["n_checker_fail"]) == (3, 2)
    # The over-credit number, stated without binarising: the judge's median on
    # work the checker rejected.
    assert overall["median_on_fail"] == 5.0
    assert overall["median_on_pass"] == 7.0
    assert overall["separation"] == 2.0
    # 3×2 = 6 pairs. The judge ranks the pass above the fail in 3 (8>2, 7>2, 3>2),
    # below in 2 (7<8, 3<8), and ties once (8 vs 8, worth ½) → 3.5 / 6.
    assert overall["auc"] == round(3.5 / 6.0, 4)


def test_judge_discrimination_unavailable_without_both_sides():
    runs = [_run("cfg-01", "case-a", 0, score=8.0, external_verdict=True)]
    disc = build_report(_exp(CONFIGS), runs, {}, partial=False)["judge_discrimination"]
    assert disc["available"] is False
    assert disc["overall"]["auc"] is None
    assert disc["overall"]["median_on_fail"] is None


def test_rq2_uses_the_pre_registered_threshold():
    exp = _exp(CONFIGS)
    exp.eval_config = {"judge_threshold": 7.5}
    rq2 = build_report(exp, _rq2_runs(), {}, partial=False)["rq2"]
    assert rq2["judge_threshold"] == 7.5
    assert rq2["threshold_source"] == "pre_registered"
    assert rq2["primary"] is False
    # At 7.5 only the 8.0s are "high": one pass, one fail.
    assert rq2["overall"]["cells"] == {
        "pass_high": 1, "pass_low": 2, "fail_high": 1, "fail_low": 1,
    }


def test_rq2_falls_back_to_the_default_and_says_so():
    rq2 = build_report(_exp(CONFIGS), _rq2_runs(), {}, partial=False)["rq2"]
    assert rq2["judge_threshold"] == 5.0
    assert rq2["threshold_source"] == "default"


def test_rq2_sensitivity_marks_only_the_pre_registered_row():
    # Scores straddling the ladder, so the over-credit count really moves.
    runs = [
        _run("cfg-01", "case-a", 0, score=6.5, external_verdict=True),
        _run("cfg-01", "case-b", 0, score=4.5, external_verdict=True),
        _run("cfg-02", "case-a", 0, score=6.5, external_verdict=False),
        _run("cfg-02", "case-b", 0, score=4.5, external_verdict=False),
    ]
    exp = _exp(CONFIGS)
    exp.eval_config = {"judge_threshold": 6.0}
    rq2 = build_report(exp, runs, {}, partial=False)["rq2"]
    rows = {row["threshold"]: row for row in rq2["sensitivity"]}
    assert sorted(rows) == [4.0, 5.0, 6.0, 7.0]
    assert [t for t, row in rows.items() if row["pre_registered"]] == [6.0]
    # Over-credit: 2 runs at ≥4, 1 at ≥5 and ≥6, none at ≥7. The same corpus, four
    # different headline numbers — which is why the headline is not this.
    assert [rows[t]["cells"]["fail_high"] for t in (4.0, 5.0, 6.0, 7.0)] == [2, 1, 1, 0]
    # …while the threshold-free view says one thing about it, whatever the cut-off.
    assert rq2["overall"]["cells"]["fail_high"] == 1


def test_a_threshold_outside_the_sensitivity_ladder_still_appears_in_it():
    exp = _exp(CONFIGS)
    exp.eval_config = {"judge_threshold": 6.5}
    rq2 = build_report(exp, _rq2_runs(), {}, partial=False)["rq2"]
    assert [row["threshold"] for row in rq2["sensitivity"]] == [4.0, 5.0, 6.0, 6.5, 7.0]


# --- infrastructure failures leave the aggregates (SPA-87) --------------------


def test_contaminated_runs_are_excluded_and_counted():
    runs = [
        _run("cfg-01", "case-a", 0, score=8.0),
        _run("cfg-01", "case-b", 0, status="failed", score=1.0,
             failure_type="llm_rate_limit"),
        _run("cfg-01", "case-c", 0, status="failed", score=1.5,
             failure_type="llm_auth"),
        _run("cfg-02", "case-a", 0, score=6.0),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)

    excl = report["exclusions"]
    assert excl["contaminated"] == 2
    assert excl["by_type"] == {"llm_auth": 1, "llm_rate_limit": 1}
    assert excl["by_config"] == [
        {"config_key": "cfg-01", "label": "fast", "contaminated": 2}
    ]

    summary = report["summary"]
    assert summary["total_runs"] == 2
    assert summary["excluded_contaminated"] == 2
    assert summary["success_rate_basis"] == "settled_non_contaminated"
    # A quota outage must not read as a weak model: cfg-01's mean is its one real
    # run, not that run averaged with two 1.x scores it never earned.
    per_config = {c["config_key"]: c for c in summary["per_config"]}
    assert per_config["cfg-01"]["quality_mean"] == 8.0
    assert per_config["cfg-01"]["success_rate"] == 1.0


def test_a_clean_report_says_the_denominator_is_untouched():
    runs = [_run("cfg-01", "case-a", 0, score=8.0)]
    summary = build_report(_exp(CONFIGS), runs, {}, partial=False)["summary"]
    assert summary["excluded_contaminated"] == 0
    assert summary["success_rate_basis"] == "settled"


def test_cap_hit_and_timeout_failures_are_typed_but_still_counted():
    """A run that hit its iteration cap or the wall clock failed on its own
    merits. Typing it must not quietly delete it from the numbers."""
    runs = [
        _run("cfg-01", "case-a", 0, score=8.0),
        _run("cfg-01", "case-b", 0, status="failed", score=2.0, failure_type="cap_hit"),
        _run("cfg-01", "case-c", 0, status="failed", score=1.0, failure_type="timeout"),
        _run("cfg-01", "case-d", 0, status="failed", score=3.0, failure_type="agent"),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)
    assert report["exclusions"]["contaminated"] == 0
    assert report["summary"]["total_runs"] == 4
    assert report["summary"]["success_rate_basis"] == "settled"


def test_an_unclassified_failure_counts():
    """NULL means «not classified» — an older agent image, a path that never
    reported. Reading it as contamination would silently delete real failures."""
    runs = [
        _run("cfg-01", "case-a", 0, score=8.0),
        _run("cfg-01", "case-b", 0, status="failed", score=2.0, failure_type=None),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)
    assert report["exclusions"]["contaminated"] == 0
    assert report["summary"]["per_config"][0]["success_rate"] == 0.5


def test_contaminated_runs_leave_the_judge_checker_view_too():
    """A quota-killed run scored low with the checker having failed it lands in
    the over-credit quadrant's opposite corner and flatters the agreement."""
    runs = _rq2_runs() + [
        _run("cfg-02", "case-z", 0, status="failed", score=0.5,
             external_verdict=False, failure_type="llm_rate_limit"),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)
    assert report["rq2"]["overall"]["n"] == 5
    assert report["judge_discrimination"]["overall"]["n_checker_fail"] == 2
