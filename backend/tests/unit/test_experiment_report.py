"""Report assembly for SPA-40 experiments (pure build_report + helpers)."""

import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.models.experiment import Experiment, ExperimentRun
from app.quality.experiment_report import (
    SIGNIFICANCE_ALPHA,
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


def _cells(values, prefix="case"):
    """``[8.0, 8.2, ...]`` -> ``{"case-0": 8.0, "case-1": 8.2, ...}``."""
    return {f"{prefix}-{i}": v for i, v in enumerate(values)}


class TestSignificanceMatrix:
    def test_separated_groups_significant(self):
        cells = {
            "cfg-01": {"weighted_score": _cells([8.0, 8.2, 8.1, 7.9, 8.3])},
            "cfg-02": {"weighted_score": _cells([5.0, 5.2, 5.1, 4.9, 5.3])},
        }
        entries, correction = significance_matrix(cells)
        assert len(entries) == 1
        entry = entries[0]
        assert (entry["a"], entry["b"]) == ("cfg-01", "cfg-02")
        assert entry["significant"] is True
        assert entry["design"] == "paired"
        assert entry["primary_test"] == "paired_t"
        # Welch survives as the unpaired cross-check, not as the verdict.
        assert entry["welch"]["p"] < 0.05
        assert entry["mann_whitney"]["approx"] is True
        assert correction["families"]["confirmatory"]["n_tests"] == 1

    def test_identical_distributions_not_significant(self):
        same = [6.0, 7.0, 8.0, 7.5, 6.5]
        entries, _ = significance_matrix(
            {
                "cfg-01": {"weighted_score": _cells(same)},
                "cfg-02": {"weighted_score": _cells(list(same))},
            },
            equivalence_margin=0.5,
        )
        assert entries[0]["significant"] is False
        # Identical on every case defeats all three paired tests, and that is the
        # most definite answer available rather than the absence of one.
        assert entries[0]["design"] == "paired"
        assert entries[0]["primary_test"] == "identical"
        assert entries[0]["p"] == 1.0
        # «Not significant» is not left as a shrug: every case moved by exactly
        # zero, so equivalence is decidable without a test and says so.
        assert entries[0]["equivalence"]["equivalent"] is True

    def test_insufficient_data_omitted(self):
        entries, correction = significance_matrix(
            {
                "cfg-01": {"weighted_score": {"case-0": 1.0}},
                "cfg-02": {"weighted_score": {"case-0": 2.0}},
            }
        )
        assert entries == []
        assert correction["n_tests"] == 0

    def test_pairing_by_case_beats_the_unpaired_test_it_replaced(self):
        """The design was always paired; until SPA-62 the test never was.

        Four cases of wildly different difficulty, config B better by ~1 point on
        every single one. Welch, comparing two independent lists, cannot see it —
        the between-case spread swamps the between-config one. The paired test
        looks at the same four differences and finds them unanimous."""
        a = {"easy": 9.0, "medium": 6.0, "hard": 3.0, "brutal": 1.0}
        b = {"easy": 9.9, "medium": 7.1, "hard": 4.0, "brutal": 2.1}
        entries, _ = significance_matrix(
            {"cfg-01": {"weighted_score": a}, "cfg-02": {"weighted_score": b}}
        )
        entry = entries[0]
        assert entry["design"] == "paired"
        assert entry["p"] < 0.01
        assert entry["welch"]["p"] > 0.5
        assert entry["significant"] is True

    def test_a_case_only_one_config_finished_falls_back_and_says_so(self):
        """A case one config failed is missing from the pairing, and which case
        that is belongs in the row — it is survivor conditioning, not a detail."""
        a = {"c1": 8.0, "c2": 7.0, "c3": 6.0, "csvsum": 9.0}
        b = {"c1": 5.0, "c2": 4.2, "c3": 3.1}
        entries, _ = significance_matrix(
            {"cfg-01": {"weighted_score": a}, "cfg-02": {"weighted_score": b}}
        )
        entry = entries[0]
        assert entry["design"] == "paired"      # 3 shared cases is still enough
        assert entry["n_pairs"] == 3
        assert entry["unpaired_cases"] == {"a": ["csvsum"], "b": []}

    def test_too_few_shared_cases_falls_back_to_welch(self):
        a = {"c1": 8.0, "c2": 7.0, "c3": 6.0, "c4": 9.0}
        b = {"c1": 5.0, "x2": 4.0, "x3": 3.0, "x4": 4.5}
        entries, _ = significance_matrix(
            {"cfg-01": {"weighted_score": a}, "cfg-02": {"weighted_score": b}}
        )
        entry = entries[0]
        assert entry["design"] == "unpaired"
        assert entry["unpaired_reason"] == "insufficient_shared_cases"
        assert entry["primary_test"] == "welch"
        assert entry["effect_kind"] == "hedges_g"

    def test_the_screen_is_corrected_against_its_own_size_not_the_headline(self):
        """Two families, two counts. A pooled correction would charge the metrics
        the experiment was built to compare for forty-odd rubric curiosities."""
        cells = {
            "cfg-01": {
                "weighted_score": _cells([8.0, 8.2, 8.1, 7.9, 8.3]),
                **{f"dim:d{i}": _cells([7.0, 7.1, 6.9, 7.0, 7.1]) for i in range(20)},
            },
            "cfg-02": {
                "weighted_score": _cells([5.0, 5.2, 5.1, 4.9, 5.3]),
                **{f"dim:d{i}": _cells([7.0, 7.1, 6.9, 7.0, 7.1]) for i in range(20)},
            },
        }
        entries, correction = significance_matrix(cells)
        assert correction["families"]["confirmatory"]["n_tests"] == 1
        assert correction["families"]["exploratory"]["n_tests"] == 20
        headline = next(e for e in entries if e["metric"] == "weighted_score")
        # Corrected against a family of one, the real difference survives.
        assert headline["significant"] is True
        assert headline["q"] == headline["p"]

    def test_a_lone_nominal_hit_among_many_does_not_survive_correction(self):
        """The shape of the audit's own false positive: one row at p < 0.05 out of
        dozens is what pure noise produces, and the corrected verdict says so."""
        cells = {"cfg-01": {}, "cfg-02": {}}
        for i in range(24):
            cells["cfg-01"][f"dim:d{i}"] = _cells([7.0, 7.1, 6.9, 7.05, 7.02])
            cells["cfg-02"][f"dim:d{i}"] = _cells([7.02, 7.08, 6.93, 7.03, 7.04])
        # One dimension lands just under the bar on its own — p = 0.035, the same
        # shape as Эксп 5b's dim:originality at 0.0399 among 48.
        cells["cfg-01"]["dim:d0"] = _cells([8.0, 8.2, 8.1, 7.9, 8.3])
        cells["cfg-02"]["dim:d0"] = _cells([7.95, 7.65, 7.95, 7.475, 8.05])
        entries, correction = significance_matrix(cells)
        hit = next(e for e in entries if e["metric"] == "dim:d0")
        assert hit["significant_uncorrected"] is True
        assert hit["significant"] is False
        assert hit["q"] > hit["p"]
        assert correction["families"]["exploratory"]["n_significant"] == 0


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
            input_tokens=None, output_tokens=None, tool_call_count=None,
            orchestrator_cost_usd="0", reasoning_tokens=None):
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
        orchestrator_cost_usd=Decimal(str(orchestrator_cost_usd)),
        quality_profile=quality_profile,
        failure_profile={"failures": failures} if failures else None,
        trajectory_profile=trajectory_profile,
        trajectory_match_profile=trajectory_match,
        trajectory_evidence_profile=None,
        hallucination_profile=None,
        human_feedback=human_feedback,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
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

    assert report["schema_version"] == 20
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

    # SPA-62: two cases run three times each is TWO observations per config, not
    # six — repeated runs of one case are averaged into their cell. Below three
    # cases nothing is testable, and the report says that rather than showing an
    # empty table that reads like «we looked and found nothing».
    assert report["significance"] == []
    correction = report["significance_correction"]
    assert correction["n_tests"] == 0
    assert correction["min_cases"] == 3
    assert correction["omitted"]["too_few_cases"] == correction["n_omitted"] > 0

    estimand = report["estimand"]
    assert estimand["population"] == "success_runs"
    assert estimand["unit"] == "case_cell_mean"
    # cfg-02 lost a run; its scores therefore describe its luckier subset.
    assert estimand["survivor_conditioned"] is True
    assert estimand["excluded_by_status"] == {"cfg-01": 0, "cfg-02": 1}
    assert estimand["margin_source"] == "default"

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
    assert report["schema_version"] == 20

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
    # These runs predate the orchestrator being metered at all: the column is
    # zero AND the report says so, so the total reads as a lower bound rather
    # than as a config that spent nothing on orchestration (SPA-111).
    assert totals["orchestrator"] == 0.0
    assert cb["orchestrator_metered"] is False


def test_cost_breakdown_counts_the_orchestrators_own_calls():
    """Template selection, the decomposition decision and result evaluation are
    LLM calls the platform makes on the run's behalf. They were spent and never
    counted, which made every cost figure an undercount of unknown size."""
    r1 = _run("cfg-01", "case-a", 0, status="success", cost="0.10")
    r2 = _run("cfg-02", "case-a", 0, status="success", cost="0.10")
    records = {
        r1.task_id: _record(cost_usd="0.10", quality_cost=0.02,
                            orchestrator_cost_usd="0.03",
                            dimensions=[{"key": "a", "score": 8}]),
        # orchestrator OFF for this config — a real zero, not a missing meter
        r2.task_id: _record(cost_usd="0.10", quality_cost=0.02,
                            dimensions=[{"key": "a", "score": 8}]),
    }
    report = build_report(_exp(CONFIGS), [r1, r2], records, partial=False)
    cb = report["cost_breakdown"]
    by = {c["config_key"]: c for c in cb["per_config"]}
    assert by["cfg-01"]["orchestrator"] == 0.03
    assert by["cfg-01"]["total"] == 0.15  # 0.10 agent + 0.02 judge + 0.03 orchestrator
    assert by["cfg-02"]["orchestrator"] == 0.0
    assert by["cfg-02"]["total"] == 0.12
    assert cb["totals"]["orchestrator"] == 0.03
    assert cb["orchestrator_metered"] is True


def test_cost_breakdown_shows_up_when_only_orchestration_cost_anything():
    """An agent on a zero-priced model still pays for its decision calls. Hiding
    the panel because the agent column is empty would hide the only figure it
    has — which is exactly the state of a stand whose working model rows carry
    no prices."""
    r = _run("cfg-01", "case-a", 0, status="success", cost="0")
    records = {r.task_id: _record(cost_usd="0", orchestrator_cost_usd="0.004")}
    report = build_report(_exp(CONFIGS), [r], records, partial=False)
    cb = report["cost_breakdown"]
    assert cb["available"] is True
    assert cb["totals"]["orchestrator"] == 0.004
    assert cb["totals"]["total"] == 0.004


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
    assert qg["n_uncertifiable"] == 0


def test_quality_gate_separates_a_failure_nobody_earned():
    """A config whose pass rate is dragged down because the provider would not
    answer is not being out-performed — it is being under-measured, and the
    report has to be able to say which (SPA-111)."""
    r1 = _run("cfg-01", "case-a", 0, status="success", score=8.0)
    r2 = _run("cfg-02", "case-a", 0, status="success", score=3.0)
    r3 = _run("cfg-02", "case-b", 0, status="success", score=None)
    records = {
        r1.task_id: _record(
            dimensions=[{"key": "correctness", "score": 8}],
            gate={"passed": True, "failed_dimensions": [], "uncertifiable_dimensions": []},
        ),
        # earned it: the deliverable was scored and fell short
        r2.task_id: _record(
            dimensions=[{"key": "correctness", "score": 3}],
            gate={
                "passed": False,
                "failed_dimensions": ["correctness"],
                "uncertifiable_dimensions": [],
            },
        ),
        # did not: the judge never got a verdict out of the provider
        r3.task_id: _record(
            dimensions=[{"key": "correctness", "score": None}],
            gate={
                "passed": False,
                "failed_dimensions": ["correctness"],
                "uncertifiable_dimensions": ["correctness"],
            },
        ),
    }
    report = build_report(_exp(CONFIGS), [r1, r2, r3], records, partial=False)
    qg = report["quality_gate"]
    by = {c["config_key"]: c for c in qg["per_config"]}
    # cfg-02 fails both runs, but only ONE of them is about the work
    assert by["cfg-02"]["pass_rate"] == 0.0
    assert by["cfg-02"]["n_uncertifiable"] == 1
    assert by["cfg-01"]["n_uncertifiable"] == 0
    assert qg["n_uncertifiable"] == 1


def test_effort_separates_thinking_from_writing():
    """Reasoning tokens are billed inside completion_tokens, so a reasoning model
    looked expensive AND shallow at once — both halves artefacts of one number
    (SPA-114). The share is of OUTPUT, not of the total: dividing by input+output
    would understate it by however long the prompt happened to be."""
    r1 = _run("cfg-01", "case-a", 0, status="success")
    r2 = _run("cfg-02", "case-a", 0, status="success")
    records = {
        r1.task_id: _record(input_tokens=1000, output_tokens=400, reasoning_tokens=300),
        r2.task_id: _record(input_tokens=1000, output_tokens=400),  # no split reported
    }
    report = build_report(_exp(CONFIGS), [r1, r2], records, partial=False)
    eff = report["effort"]
    assert eff["reasoning_available"] is True
    by = {c["config_key"]: c for c in eff["per_config"]}
    assert by["cfg-01"]["reasoning_tokens_mean"] == 300
    assert by["cfg-01"]["reasoning_share"] == 0.75  # of OUTPUT, not of 1400
    # a model that does not reason, or a provider that does not say, is absent —
    # not a share of zero
    assert by["cfg-02"]["reasoning_tokens_mean"] is None
    assert by["cfg-02"]["reasoning_share"] is None


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

    assert _classify_reliability(0.7, 10, has_source=True) == "reliable_absolute"
    assert _classify_reliability(0.6, 10, has_source=True) == "reliable_absolute"  # boundary
    assert _classify_reliability(0.5, 10, has_source=True) == "moderate_agreement"
    assert _classify_reliability(0.4, 10, has_source=True) == "moderate_agreement"  # boundary
    assert _classify_reliability(0.39, 10, has_source=True) == "unreliable"
    assert _classify_reliability(-0.1, 10, has_source=True) == "unreliable"
    # SPA-88: too little data is its own answer, not a weak endorsement
    assert _classify_reliability(0.9, 2, has_source=True) == "insufficient"
    assert _classify_reliability(None, 10, has_source=True) == "insufficient"
    assert _classify_reliability(0.9, 10, has_source=False) == "not_calibrated"


def test_classify_reliability_rank_rescue():
    # κ below the bar but ranks agreeing (Spearman ρ≥0.5) → rank_only, not
    # unreliable — a scale-shifted judge is usable for comparisons only.
    from app.quality.experiment_report import _classify_reliability

    assert _classify_reliability(0.0, 10, has_source=True, rho=0.93) == "rank_only"
    assert _classify_reliability(0.0, 10, has_source=True, rho=0.5) == "rank_only"  # boundary
    assert _classify_reliability(0.0, 10, has_source=True, rho=0.49) == "unreliable"
    assert _classify_reliability(0.0, 10, has_source=True, rho=None) == "unreliable"
    assert _classify_reliability(0.0, 10, has_source=True, rho=-0.2) == "unreliable"
    # ranks never DEMOTE: κ above the bar stays what κ says
    assert _classify_reliability(0.7, 10, has_source=True, rho=0.1) == "reliable_absolute"
    assert _classify_reliability(0.5, 10, has_source=True, rho=0.1) == "moderate_agreement"
    # too few pairs still wins over the rank rescue
    assert _classify_reliability(0.0, 2, has_source=True, rho=0.9) == "insufficient"


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
    assert ax["task_completion"]["status"] == "rank_only"  # rank-rescued (ρ=0.62)
    assert ax["format_compliance"]["status"] == "unreliable"  # low κ AND low ρ
    assert ax["readability"]["status"] == "moderate_agreement"  # κ in 0.4–0.6 band
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
    assert (ax["efficiency"]["status"], ax["efficiency"]["source"]) == ("reliable_absolute", "human")
    assert (ax["tool_selection"]["status"], ax["tool_selection"]["source"]) == ("moderate_agreement", "human")
    assert (ax["parameter_quality"]["status"], ax["parameter_quality"]["source"]) == ("unreliable", "human")
    # human dim exists but n=2 < MIN_SAMPLES → insufficient, still human-sourced
    assert (ax["error_recovery"]["status"], ax["error_recovery"]["source"]) == ("insufficient", "human")
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


def test_a_config_whose_every_run_was_contaminated_survives_the_report():
    """A provider outage can take out one arm of the matrix entirely. The config
    must still appear, with empty numbers rather than a crash or a fabricated 0%."""
    runs = [
        _run("cfg-01", "case-a", 0, score=8.0),
        _run("cfg-02", "case-a", 0, status="failed", score=1.0,
             failure_type="llm_rate_limit"),
        _run("cfg-02", "case-b", 0, status="failed", score=1.0,
             failure_type="llm_rate_limit"),
    ]
    report = build_report(_exp(CONFIGS), runs, {}, partial=False)
    per_config = {c["config_key"]: c for c in report["summary"]["per_config"]}
    assert per_config["cfg-02"]["n_runs"] == 0
    assert per_config["cfg-02"]["success_rate"] is None
    assert per_config["cfg-02"]["quality_mean"] is None
    assert report["exclusions"]["by_config"] == [
        {"config_key": "cfg-02", "label": "orch", "contaminated": 2}
    ]


# --- SPA-88: the reliability gate acts ---------------------------------------


def test_trust_split_partitions_axes_by_what_they_may_drive():
    from app.quality.experiment_report import _trust_split

    def ax(name, status):
        return {"name": name, "status": status, "source": "human",
                "kappa": 0.5, "rho": 0.5, "n": 10}

    numeric, rank_keys, block = _trust_split(
        {
            "axes": {
                "a": ax("A", "reliable_absolute"),
                "b": ax("B", "moderate_agreement"),
                "c": ax("C", "rank_only"),
                "d": ax("D", "unreliable"),
                "e": ax("E", "insufficient"),
                "f": ax("F", "not_calibrated"),
            }
        }
    )
    assert numeric == frozenset({"a", "b"})
    # rank is a SUPERSET: good enough to average is good enough to order
    assert rank_keys == frozenset({"a", "b", "c"})
    assert [r["key"] for r in block["numeric"]] == ["a", "b"]
    assert [r["key"] for r in block["rank_only"]] == ["c"]
    # every quarantine carries the reason it was quarantined for
    assert [r["key"] for r in block["excluded"]] == ["d", "e", "f"]
    assert block["excluded"][0]["kappa"] == 0.5
    assert block["n_axes"] == 6

    numeric2, rank2, block2 = _trust_split({})
    assert numeric2 == frozenset() and rank2 == frozenset()
    assert block2["n_axes"] == 0


def test_trusted_weighted_renormalizes_instead_of_scoring_a_dropped_axis_zero():
    from app.quality.experiment_report import _trusted_weighted

    rec = _record(
        dimensions=[
            {"key": "correctness", "name": "Correctness", "score": 8, "weight": 3, "status": "scored"},
            {"key": "originality", "name": "Originality", "score": 2, "weight": 1, "status": "scored"},
        ]
    )
    assert _trusted_weighted(rec, frozenset({"correctness", "originality"})) == 6.5
    # Dropping an axis must RENORMALIZE (8.0), not average a phantom zero in (6.0).
    assert _trusted_weighted(rec, frozenset({"correctness"})) == 8.0
    assert _trusted_weighted(rec, frozenset()) is None
    assert _trusted_weighted(rec, frozenset({"absent"})) is None

    # A dimension the judge could not score contributes nothing, exactly as in
    # the judge's own aggregate — a trusted axis that failed is not a trusted 0.
    errored = _record(
        dimensions=[{"key": "correctness", "score": None, "weight": 1, "status": "error"}]
    )
    assert _trusted_weighted(errored, frozenset({"correctness"})) is None


def test_traj_score_under_a_gate_never_falls_back_to_the_stored_overall():
    from app.quality.experiment_report import _traj_score

    rec = _record(
        trajectory_axes=[
            {"key": "efficiency", "name": "Efficiency", "score": 9},
            {"key": "goal_alignment", "name": "Goal alignment", "score": 3},
        ]
    )
    assert _traj_score(rec, 7.0) == 6.0
    assert _traj_score(rec, 7.0, allowed=frozenset({"efficiency"})) == 9.0
    # The stored overall averages every axis — including the ones just gated out —
    # so falling back to it would smuggle them in under a trusted label.
    assert _traj_score(rec, 7.0, allowed=frozenset({"tool_selection"})) is None
    assert _traj_score(None, 7.0, allowed=frozenset({"efficiency"})) is None


def test_a_rank_only_metric_is_judged_on_ranks_not_on_means():
    from app.quality.experiment_report import significance_matrix

    cells = {
        "a": {"dim:x": _cells([8, 9, 10, 11])},
        "b": {"dim:x": _cells([1, 2, 3.5, 4])},
    }
    row = significance_matrix(cells, rank_only_metrics=frozenset({"dim:x"}))[0][0]
    assert row["rank_only"] is True
    # Welch compares MEANS — the one thing a scale-shifted judge cannot support.
    # Neither does the paired t-test, so the rank-rescued row rests on the signed
    # rank test and, below its minimum, on Mann-Whitney.
    assert row["welch"] is None
    assert row["primary_test"] in {"wilcoxon", "sign", "mann_whitney"}
    assert row["mann_whitney"] is not None

    plain = significance_matrix(cells)[0][0]
    assert plain["rank_only"] is False
    assert plain["welch"] is not None
    assert plain["primary_test"] == "paired_t"
    assert plain["p"] == plain["paired_t"]["p"]


def _two_dim_runs(per_config: dict[str, dict[str, list[float]]]):
    """Runs + records for two configs scored on the same two rubric dimensions.

    ``per_config`` is ``{config_key: {dim_key: [score per case]}}``; every list is
    the same length, one entry per case. The stored ``weighted_score`` is the
    equally-weighted mean of the dimensions, matching what the judge would have
    written."""
    runs, records = [], {}
    for cfg, dims in per_config.items():
        n = len(next(iter(dims.values())))
        for i in range(n):
            scores = {k: v[i] for k, v in dims.items()}
            weighted = sum(scores.values()) / len(scores)
            task_id = uuid.uuid4()
            runs.append(_run(cfg, f"case-{i}", 0, score=weighted, task_id=task_id))
            records[task_id] = _record(
                dimensions=[
                    {"key": k, "name": k.title(), "score": v, "weight": 1, "status": "scored"}
                    for k, v in scores.items()
                ]
            )
    return runs, records


def _calibration(dims: list[dict]):
    return {"available": True, "dimensions": dims}


def test_a_rank_only_row_produces_no_magnitude_of_any_kind():
    """SPA-115. Skipping Welch is not enough.

    A rank-rescued axis is one whose ORDER agrees with the human and whose scale
    does not. Every magnitude — a mean difference, an interval on one, a
    standardised effect, an equivalence verdict measured in judge points — is a
    claim that scale supports, and a strictly monotone rescaling that preserves
    every rank moves all of them freely. The row must therefore carry none of
    them, and must say that it is refusing rather than look like missing data."""
    from app.quality.experiment_report import significance_matrix

    cells = {
        "a": {"dim:x": _cells([8.0, 9.0, 10.0, 11.0])},
        "b": {"dim:x": _cells([1.0, 2.0, 3.5, 4.0])},
    }
    row = significance_matrix(
        cells, rank_only_metrics=frozenset({"dim:x"}), equivalence_margin=1.0
    )[0][0]
    assert row["rank_only"] is True
    assert row["magnitudes_withheld"] == "rank_only_axis"
    assert row["effect"] is None and row["effect_kind"] is None
    assert row["ci"] is None
    assert row["power"] is None
    assert row["equivalence"] is None
    assert row["welch"] is None
    assert row["primary_test"] in {"wilcoxon", "sign"}

    # The same axis without the rank-only flag keeps everything — so the absence
    # above is the flag's doing, not a shortage of data.
    plain = significance_matrix(cells, equivalence_margin=1.0)[0][0]
    assert plain["ci"] is not None and plain["effect"] is not None
    assert plain["magnitudes_withheld"] is None


def test_a_rank_only_verdict_survives_rescaling_the_axis():
    """The property the whole rank-only category exists for: rescale the axis in
    a way that preserves every ordering, and nothing the row claims may move.

    Enough pairs that Wilcoxon is genuinely available — which is the point. An
    earlier version of this test used four, below Wilcoxon's minimum, so the row
    fell through to the sign test and the property held for the wrong reason
    while the code was still wrong. A test that cannot reach the branch it is
    guarding proves nothing about it."""
    from app.quality.experiment_report import significance_matrix
    from app.quality.stats import MIN_WILCOXON_PAIRS, wilcoxon_signed_rank

    a = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0]
    b = [1.0, 2.0, 3.5, 4.0, 11.5, 12.5, 13.5]
    assert len(a) >= MIN_WILCOXON_PAIRS, "the branch under test must be reachable"

    # Strictly increasing, so every rank is preserved and every gap is not.
    squash = lambda v: v**2 / 10  # noqa: E731
    scaled_a, scaled_b = [squash(v) for v in a], [squash(v) for v in b]

    # Wilcoxon ranks the MAGNITUDES of the differences, so it moves — which is
    # exactly why it cannot be the verdict for an axis trusted on order alone.
    assert (
        wilcoxon_signed_rank(list(zip(a, b)))["p"]
        != wilcoxon_signed_rank(list(zip(scaled_a, scaled_b)))["p"]
    )

    def row(xs, ys):
        return significance_matrix(
            {"a": {"dim:x": _cells(xs)}, "b": {"dim:x": _cells(ys)}},
            rank_only_metrics=frozenset({"dim:x"}),
        )[0][0]

    base, scaled = row(a, b), row(scaled_a, scaled_b)
    assert base["primary_test"] == "sign", "not Wilcoxon, however rank-flavoured"
    assert base["p"] == scaled["p"]
    assert base["significant"] == scaled["significant"]
    assert base["q"] == scaled["q"]
    # The diagnostic is still reported — it just does not decide anything.
    assert base["wilcoxon"] is not None

    # The unpaired half of the category holds too, and for a different reason:
    # Mann-Whitney ranks the raw VALUES, which a monotone map leaves in place.
    # Asserted rather than assumed, so the property covers the whole category.
    def unpaired(xs, ys):
        return significance_matrix(
            {
                "a": {"dim:x": {f"a{i}": v for i, v in enumerate(xs)}},
                "b": {"dim:x": {f"b{i}": v for i, v in enumerate(ys)}},
            },
            rank_only_metrics=frozenset({"dim:x"}),
        )[0][0]

    u_base, u_scaled = unpaired(a, b), unpaired(scaled_a, scaled_b)
    assert u_base["design"] == "unpaired"
    assert u_base["primary_test"] == "mann_whitney"
    assert u_base["p"] == u_scaled["p"]


def test_a_degenerate_paired_test_does_not_change_the_design():
    """SPA-115. Four cases moved by exactly +1 is the strongest paired evidence a
    matrix that size can produce, and it is exactly the case that leaves the
    t-test with no variance. The design describes the experiment, not the
    arithmetic, so it stays paired and the row reports a paired answer."""
    from app.quality.experiment_report import significance_matrix

    row = significance_matrix(
        {
            "a": {"weighted_score": _cells([8.0, 6.0, 9.0, 4.0])},
            "b": {"weighted_score": _cells([9.0, 7.0, 10.0, 5.0])},
        }
    )[0][0]
    assert row["design"] == "paired"
    assert row["n_pairs"] == 4
    assert row["primary_test"] == "sign"
    assert row["paired_t"] is None, "the unavailable inference stays unavailable"
    # The interval is the paired one and excludes zero, instead of the unpaired
    # [-5.5, 3.5] the old fallback reported.
    assert row["ci"]["lo"] == row["ci"]["hi"] == -1.0
    # No spread in the differences means no detectable-effect arithmetic to do,
    # and saying so beats inventing a number.
    assert row["power"] is None
    # Welch rides along as the cross-check and is visibly the weaker reading.
    assert row["welch"]["p"] > 0.5 > row["p"]


def test_repeated_runs_of_one_case_are_one_observation_not_many():
    """Three cases run three times each is a sample of THREE, not nine.

    Repeated runs of the same case share everything about that case; entering
    them as independent observations is pseudoreplication, and it inflates
    confidence exactly where the design is weakest. The cell is the unit, so the
    row reports three pairs — and stays testable, unlike the two-case matrix."""
    runs, records = [], {}
    for case_i in range(3):
        for run_i in range(3):
            for cfg, base in (("cfg-01", 8.0), ("cfg-02", 6.0)):
                # Case difficulty dominates; the config effect is the constant on
                # top of it, which is precisely what the pairing recovers.
                score = base + case_i * 1.5 + run_i * 0.05
                r = _run(cfg, f"case-{case_i}", run_i, score=score, traj=7.0)
                runs.append(r)
                records[r.task_id] = _record(
                    dimensions=[{"key": "correctness", "score": score}]
                )

    report = build_report(_exp(CONFIGS), runs, records)
    row = next(
        r for r in report["significance"] if r["metric"] == "weighted_score"
    )
    assert row["design"] == "paired"
    assert row["n_pairs"] == 3
    assert row["n_cases_a"] == 3 and row["n_cases_b"] == 3
    assert row["effect_kind"] == "cohens_dz"
    assert row["ci"] is not None and row["power"]["n"] == 3


def test_an_unreliable_axis_cannot_produce_a_significant_row():
    """Regression fixture from Эксп 5b.

    There, ``dim:originality`` came out Welch-significant (p = 0.0399) between two
    configurations on an axis whose judge agreed with humans at κ = 0.058 and
    ρ = 0.095 — no agreement on level, none on order either. The raw view still
    reports it, because hiding it would be a different dishonesty; the trusted
    view must not, and must say that it dropped it."""
    runs, records = _two_dim_runs(
        {
            # correctness: no real difference (κ = 0.71 — trustworthy, and it says
            # there is nothing here)
            "cfg-01": {"correctness": [8, 7, 8, 7, 8], "originality": [7, 8, 8, 8, 7]},
            "cfg-02": {"correctness": [7, 8, 7, 8, 8], "originality": [6, 7, 7, 7, 7]},
        }
    )
    report = build_report(
        _exp(CONFIGS),
        runs,
        records,
        calibration=_calibration(
            [
                {"key": "correctness", "name": "Correctness", "n": 40, "cohen_kappa": 0.71, "spearman": 0.80},
                {"key": "originality", "name": "Originality", "n": 40, "cohen_kappa": 0.058, "spearman": 0.095},
            ]
        ),
    )

    raw = next(r for r in report["significance"] if r["metric"] == "dim:originality")
    assert raw["significant"] is True
    assert raw["p"] < SIGNIFICANCE_ALPHA
    # SPA-62: the verdict now comes from the paired test, and on the same data it
    # is STRICTLY stronger than the Welch number this fixture used to assert —
    # which is the whole argument for pairing, visible on the audit's own case.
    assert raw["design"] == "paired"
    assert raw["p"] < raw["welch"]["p"]
    # The raw row carries its own condition: the axis it was measured through.
    assert raw["axis"]["status"] == "unreliable"
    assert raw["axis"]["numeric"] is False

    trusted = report["trusted"]
    assert trusted["available"] is True
    assert [r["key"] for r in trusted["outcome_axes"]["excluded"]] == ["originality"]
    assert [r["key"] for r in trusted["outcome_axes"]["numeric"]] == ["correctness"]
    assert not [r for r in trusted["significance"] if r["metric"] == "dim:originality"]
    assert trusted["dropped"]["significant_metrics"] == ["dim:originality"]
    assert trusted["dropped"]["significant_rows"] == 1


def test_an_unreliable_axis_cannot_pick_a_winner():
    # cfg-01 wins on the axis nobody should trust and loses on the one they can.
    runs, records = _two_dim_runs(
        {
            "cfg-01": {"correctness": [6, 6, 6, 6, 6], "originality": [10, 10, 10, 10, 10]},
            "cfg-02": {"correctness": [7, 7, 7, 7, 7], "originality": [5, 5, 5, 5, 5]},
        }
    )
    report = build_report(
        _exp(CONFIGS),
        runs,
        records,
        calibration=_calibration(
            [
                {"key": "correctness", "name": "Correctness", "n": 40, "cohen_kappa": 0.71, "spearman": 0.80},
                {"key": "originality", "name": "Originality", "n": 40, "cohen_kappa": 0.058, "spearman": 0.095},
            ]
        ),
    )

    assert report["leaderboard"]["players"][0]["player"] == "cfg-01"
    assert report["pareto"]["frontier"] == ["cfg-01"]
    summary = {e["config_key"]: e["quality_mean"] for e in report["summary"]["per_config"]}
    assert summary["cfg-01"] > summary["cfg-02"]

    trusted = report["trusted"]
    assert trusted["leaderboard"]["players"][0]["player"] == "cfg-02"
    assert trusted["pareto"]["frontier"] == ["cfg-02"]
    t_summary = {e["config_key"]: e["quality_mean"] for e in trusted["summary"]["per_config"]}
    assert t_summary == {"cfg-01": 6.0, "cfg-02": 7.0}


def test_a_rank_only_axis_reaches_a_rank_test_and_nothing_else():
    runs, records = _two_dim_runs(
        {
            "cfg-01": {"helpfulness": [9, 8, 9, 8, 9]},
            "cfg-02": {"helpfulness": [4, 3, 4, 3, 4]},
        }
    )
    report = build_report(
        _exp(CONFIGS),
        runs,
        records,
        calibration=_calibration(
            # κ collapsed, ranks intact: the SPA-79 rescue, and its whole licence
            [{"key": "helpfulness", "name": "Helpfulness", "n": 40, "cohen_kappa": 0.10, "spearman": 0.91}]
        ),
    )
    trusted = report["trusted"]
    assert [r["key"] for r in trusted["outcome_axes"]["rank_only"]] == ["helpfulness"]
    assert trusted["outcome_axes"]["numeric"] == []
    # No numeric aggregate exists — a scale-shifted judge has no level to average.
    assert all(e["quality_mean"] is None for e in trusted["summary"]["per_config"])
    assert trusted["pareto"]["frontier"] == []
    # …and no leaderboard either: the leaderboard runs on a weighted MEAN of the
    # trusted axes, which is an average however few axes go into it.
    assert trusted["leaderboard"]["basis"] == "numeric_trusted_axes"
    assert trusted["leaderboard"]["status"] == "empty"
    # The one thing it may do: its own rank test, on its own scores.
    row = next(r for r in trusted["significance"] if r["metric"] == "dim:helpfulness")
    assert row["rank_only"] is True and row["welch"] is None


def test_rescaling_a_rank_only_axis_cannot_move_the_trusted_view():
    """The property that makes the rescue safe, stated as a test.

    A rank-rescued axis is one whose ORDER tracks the human while its level does
    not — so any monotone rescaling of it is equally consistent with what the
    calibration measured. Raw is free to move under such a rescaling, and does.
    Nothing in the trusted view may."""
    # Same ordering on the rescued axis both times (cfg-02 above cfg-01), same
    # ordering on the trustworthy one (cfg-01 above cfg-02) — only the SIZE of the
    # rescued gap differs, which is precisely what ρ does not constrain.
    def _report(helpfulness):
        runs, records = _two_dim_runs(
            {
                "cfg-01": {"correctness": [9] * 5, "helpfulness": [helpfulness[0]] * 5},
                "cfg-02": {"correctness": [6] * 5, "helpfulness": [helpfulness[1]] * 5},
            }
        )
        return build_report(
            _exp(CONFIGS),
            runs,
            records,
            calibration=_calibration(
                [
                    {"key": "correctness", "name": "Correctness", "n": 40, "cohen_kappa": 0.71, "spearman": 0.80},
                    {"key": "helpfulness", "name": "Helpfulness", "n": 40, "cohen_kappa": 0.10, "spearman": 0.91},
                ]
            ),
        )

    wide = _report((1.0, 9.0))    # raw means 5.0 vs 7.5 → cfg-02 leads raw
    narrow = _report((5.0, 6.0))  # raw means 7.0 vs 6.0 → cfg-01 leads raw

    # The rescaling is real: it flips the raw winner without changing any ordering
    # the calibration actually validated.
    assert wide["leaderboard"]["players"][0]["player"] == "cfg-02"
    assert narrow["leaderboard"]["players"][0]["player"] == "cfg-01"

    # …and reaches nothing in the trusted view.
    for report in (wide, narrow):
        assert report["trusted"]["leaderboard"]["players"][0]["player"] == "cfg-01"
        assert {
            e["config_key"]: e["quality_mean"] for e in report["trusted"]["summary"]["per_config"]
        } == {"cfg-01": 9.0, "cfg-02": 6.0}
        assert report["trusted"]["pareto"]["frontier"] == ["cfg-01"]


def test_without_a_calibration_source_nothing_is_trusted():
    runs, records = _two_dim_runs(
        {
            "cfg-01": {"correctness": [8, 8, 8, 8, 8]},
            "cfg-02": {"correctness": [5, 5, 5, 5, 5]},
        }
    )
    report = build_report(_exp(CONFIGS), runs, records)
    trusted = report["trusted"]
    # Unknown is not trusted: an uncalibrated corpus produces an empty trusted
    # view, not a table that looks like a result.
    assert trusted["available"] is False
    assert [r["status"] for r in trusted["outcome_axes"]["excluded"]] == []
    assert all(e["quality_mean"] is None for e in trusted["summary"]["per_config"])
    assert trusted["significance"] == []
    assert trusted["leaderboard"]["status"] == "empty"
    # …while the raw view is untouched.
    assert report["leaderboard"]["players"][0]["player"] == "cfg-01"


def test_a_rank_rescued_trajectory_axis_does_not_open_an_empty_trusted_view():
    """`available` promises a view with something in it, not a view that exists.

    A rank-rescued OUTCOME axis earns its own Mann-Whitney row, so it is content.
    A rank-rescued TRAJECTORY axis earns nothing — the report has no per-axis
    trajectory significance rows, only the aggregate, and the aggregate is
    numeric. Counting it as availability opens a Trusted tab in which every single
    cell is «—»."""
    runs, records = [], {}
    for cfg, scores in (("cfg-01", [9, 8, 9, 8, 9]), ("cfg-02", [4, 3, 4, 3, 4])):
        for i, v in enumerate(scores):
            task_id = uuid.uuid4()
            runs.append(_run(cfg, f"case-{i}", 0, score=7.0, traj=float(v), task_id=task_id))
            records[task_id] = _record(
                trajectory_axes=[{"key": "efficiency", "name": "Efficiency", "score": v}]
            )
    report = build_report(
        _exp(CONFIGS),
        runs,
        records,
        calibration=_calibration(
            [{"key": "efficiency", "name": "Efficiency", "n": 40, "cohen_kappa": 0.10, "spearman": 0.91}]
        ),
    )
    # The badge is still earned, and still shown in the raw report.
    assert report["axis_reliability"]["axes"]["efficiency"]["status"] == "rank_only"
    trusted = report["trusted"]
    assert [r["key"] for r in trusted["trajectory_axes"]["rank_only"]] == ["efficiency"]
    # …but it licenses nothing here, so there is no trusted view to offer.
    assert trusted["available"] is False
    assert all(e["trajectory_mean"] is None for e in trusted["summary"]["per_config"])
    assert trusted["significance"] == []

    # A trajectory axis the calibrator DOES trust numerically is content, and opens it.
    ok = build_report(
        _exp(CONFIGS),
        runs,
        records,
        calibration=_calibration(
            [{"key": "efficiency", "name": "Efficiency", "n": 40, "cohen_kappa": 0.71, "spearman": 0.80}]
        ),
    )
    assert ok["trusted"]["available"] is True
    assert ok["trusted"]["summary"]["per_config"][0]["trajectory_mean"] is not None
