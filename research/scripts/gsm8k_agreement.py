#!/usr/bin/env python3
"""Offline judge-vs-ground-truth agreement analysis for RQ1a (SPA-50).

Compares the E-03 reference judge's verdict (the "correctness" rubric
dimension, pass = score >= 6) against exact ground truth (the agent's
extracted final number vs the GSM8K gold answer) over an experiment's runs.

Input rows (one of):
- a JSON file with the output of GET /api/experiments/{id}/results
  (per-run rows carrying ``result_summary`` and ``quality_profile``) — the
  recommended source, since the flat /export rows do not include result text;
- a JSON file with the output of GET /api/experiments/{id}/export?format=json
  (flat rows with ``dim_correctness``) — judge scores only; the script exits
  with an explanatory error because ground truth cannot be computed without
  the result text;
- ``--experiment-id`` to fetch /results live (``--base-url``, auth via
  ``--token``/$SPAWNHIVE_TOKEN and ``--workspace-id``/$SPAWNHIVE_WORKSPACE_ID).

Gold answers come from the frozen dataset JSONL (``--dataset``,
research/datasets/gsm8k-50.jsonl), keyed by case_id == case_key.

Per run:
- extracted = the number on the last "FINAL ANSWER:" line of the result text
  (fallback: the last number anywhere in the text); "$", commas and trailing
  punctuation are normalized.
- gt_correct  = |extracted - gold| <= 1e-6
- judge_pass  = correctness score >= 6 (the dataset rubric's threshold)

Output: n, raw agreement rate, Cohen's kappa (ported verbatim from
backend/app/quality/stats.py), the 2x2 confusion matrix and a disagreements
table. ``--selftest`` runs the whole pipeline on fabricated rows with a
hand-checked kappa. Plain python3, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

JUDGE_PASS_THRESHOLD = 6
TOLERANCE = 1e-6

# --- Cohen's kappa (ported from backend/app/quality/stats.py) ----------------

MIN_SAMPLES = 3


def cohen_kappa(a: list[str], b: list[str], labels: list[str]) -> float | None:
    """Cohen's kappa for two categorical raters over a fixed ``labels`` set.

    ``None`` below :data:`MIN_SAMPLES`. When the labels are perfectly predictable
    from the marginals (expected agreement ``pe == 1``) kappa is undefined, so we
    return ``1.0`` if the raters fully agree and ``0.0`` otherwise rather than
    dividing by zero."""
    n = len(a)
    if n != len(b) or n < MIN_SAMPLES:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = 0.0
    for lab in labels:
        pa = sum(1 for x in a if x == lab) / n
        pb = sum(1 for y in b if y == lab) / n
        pe += pa * pb
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return round((po - pe) / (1 - pe), 4)


# --- number extraction --------------------------------------------------------

_NUMBER_RE = re.compile(r"-?\$?\d[\d,]*(?:\.\d+)?")
_FINAL_RE = re.compile(r"FINAL\s*ANSWER\s*:\s*(.+)", re.IGNORECASE)


def _to_number(token: str) -> float | None:
    cleaned = token.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_final_number(text: str | None) -> float | None:
    """The agent's final numeric answer from its result text.

    The number on the LAST "FINAL ANSWER:" line wins; if no such line yields a
    number, fall back to the last number anywhere in the text."""
    if not text:
        return None
    for match in reversed(_FINAL_RE.findall(text)):
        nums = _NUMBER_RE.findall(match)
        if nums:
            value = _to_number(nums[0])
            if value is not None:
                return value
    nums = _NUMBER_RE.findall(text)
    for token in reversed(nums):
        value = _to_number(token)
        if value is not None:
            return value
    return None


# --- row parsing ----------------------------------------------------------------


def judge_score(row: dict) -> float | None:
    """The correctness dimension's 0-10 score from a /results row
    (quality_profile.dimensions) or a flat /export row (dim_correctness)."""
    profile = row.get("quality_profile")
    if isinstance(profile, dict):
        for dim in profile.get("dimensions") or []:
            if dim.get("key") == "correctness":
                return dim.get("score")
        return None
    return row.get("dim_correctness")


def load_gold(dataset_path: str) -> dict[str, float]:
    gold: dict[str, float] = {}
    with open(dataset_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            case = json.loads(line)
            value = _to_number(str(case.get("reference_answer") or ""))
            if value is None:
                raise SystemExit(
                    f"non-numeric reference_answer for {case.get('case_id')!r} in {dataset_path}"
                )
            gold[case["case_id"]] = value
    if not gold:
        raise SystemExit(f"no cases found in {dataset_path}")
    return gold


# --- analysis --------------------------------------------------------------------


def analyze(rows: list[dict], gold: dict[str, float]) -> dict:
    judge_labels: list[str] = []
    gt_labels: list[str] = []
    disagreements: list[dict] = []
    excluded: list[dict] = []

    for row in rows:
        case_key = row.get("case_key")
        ident = {"case_id": case_key, "run_index": row.get("run_index")}
        score = judge_score(row)
        if score is None:
            excluded.append({**ident, "reason": f"no judge score (status={row.get('status')})"})
            continue
        if case_key not in gold:
            excluded.append({**ident, "reason": "case_id not in dataset"})
            continue
        text = row.get("result_summary")
        extracted = extract_final_number(text)
        if extracted is None:
            excluded.append({**ident, "reason": "no number extractable from result text"})
            continue
        gt_correct = abs(extracted - gold[case_key]) <= TOLERANCE
        judge_pass = score >= JUDGE_PASS_THRESHOLD
        judge_labels.append("pass" if judge_pass else "fail")
        gt_labels.append("correct" if gt_correct else "wrong")
        if judge_pass != gt_correct:
            disagreements.append(
                {
                    **ident,
                    "gold": gold[case_key],
                    "extracted": extracted,
                    "judge_score": score,
                    "judge_pass": judge_pass,
                    "gt_correct": gt_correct,
                }
            )

    n = len(judge_labels)
    pairs = list(zip(judge_labels, gt_labels))
    confusion = {
        "judge_pass_gt_correct": pairs.count(("pass", "correct")),
        "judge_pass_gt_wrong": pairs.count(("pass", "wrong")),
        "judge_fail_gt_correct": pairs.count(("fail", "correct")),
        "judge_fail_gt_wrong": pairs.count(("fail", "wrong")),
    }
    agree = confusion["judge_pass_gt_correct"] + confusion["judge_fail_gt_wrong"]
    # Map both raters onto the same binary label set for kappa.
    a = ["pos" if x == "pass" else "neg" for x in judge_labels]
    b = ["pos" if y == "correct" else "neg" for y in gt_labels]
    return {
        "n": n,
        "agreement_rate": round(agree / n, 4) if n else None,
        "cohen_kappa": cohen_kappa(a, b, ["pos", "neg"]),
        "confusion": confusion,
        "disagreements": disagreements,
        "excluded": excluded,
    }


def print_report(result: dict) -> None:
    c = result["confusion"]
    print(f"n (paired runs):        {result['n']}")
    print(f"agreement rate:         {result['agreement_rate']}")
    print(f"Cohen's kappa:          {result['cohen_kappa']}")
    print()
    print("confusion matrix (judge verdict vs ground truth):")
    print("                      gt_correct   gt_wrong")
    print(f"  judge_pass          {c['judge_pass_gt_correct']:>10}   {c['judge_pass_gt_wrong']:>8}")
    print(f"  judge_fail          {c['judge_fail_gt_correct']:>10}   {c['judge_fail_gt_wrong']:>8}")
    if result["excluded"]:
        print(f"\nexcluded rows ({len(result['excluded'])}):")
        for e in result["excluded"]:
            print(f"  {e['case_id']} (run {e['run_index']}): {e['reason']}")
    if result["disagreements"]:
        print(f"\ndisagreements ({len(result['disagreements'])}):")
        print(f"  {'case_id':<14} {'gold':>10} {'extracted':>12} {'judge_score':>12}")
        for d in result["disagreements"]:
            print(
                f"  {str(d['case_id']):<14} {d['gold']:>10g} {d['extracted']:>12g} "
                f"{d['judge_score']:>12}"
            )
    else:
        print("\nno disagreements")


# --- fetching ----------------------------------------------------------------------


def fetch_results(experiment_id: str, base_url: str, token: str, workspace_id: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/experiments/{experiment_id}/results"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Workspace-Id": workspace_id,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


# --- selftest ---------------------------------------------------------------------


def _fabricated_row(case_id: str, text: str, score: int) -> dict:
    return {
        "case_key": case_id,
        "run_index": 0,
        "status": "success",
        "result_summary": text,
        "quality_profile": {
            "dimensions": [{"key": "correctness", "score": score, "status": "scored"}]
        },
    }


def selftest() -> int:
    # Extraction unit checks.
    assert extract_final_number("Step 1...\nFINAL ANSWER: 72") == 72
    assert extract_final_number("FINAL ANSWER: 1,000") == 1000
    assert extract_final_number("FINAL ANSWER: $18.50") == 18.5
    assert extract_final_number("final answer: 3.5 (approx)") == 3.5
    assert extract_final_number("FINAL ANSWER: -4") == -4
    assert extract_final_number("first 12 then the result is 99.") == 99  # fallback: last number
    assert extract_final_number("FINAL ANSWER: 7\nFINAL ANSWER: 8") == 8  # last marker wins
    assert extract_final_number("no numbers here") is None
    assert extract_final_number(None) is None

    # Hand-checked 10-row example:
    #   judge_pass: T T T T T T F F F F      (scores 10/8/7/9/6/10 pass, 3/0/2/5 fail)
    #   gt_correct: T T T T T F F F F T
    # po = 8/10 = 0.8; pe = 0.6*0.6 + 0.4*0.4 = 0.52
    # kappa = (0.8 - 0.52) / (1 - 0.52) = 0.28/0.48 = 0.583333 -> 0.5833
    gold = {f"c{i}": float(i) for i in range(1, 11)}
    plan = [  # (case, extracted-answer-in-text, judge score)
        ("c1", "1", 10), ("c2", "2", 8), ("c3", "3", 7), ("c4", "4", 9), ("c5", "5", 6),
        ("c6", "999", 10),  # judge pass, gt wrong
        ("c7", "999", 3), ("c8", "999", 0), ("c9", "999", 2),
        ("c10", "10", 5),  # judge fail, gt correct
    ]
    rows = [
        _fabricated_row(case, f"working...\nFINAL ANSWER: {ans}", score)
        for case, ans, score in plan
    ]
    # Plus one excluded row (no judge score) that must not affect the stats.
    rows.append({"case_key": "c1", "run_index": 1, "status": "skipped",
                 "result_summary": None, "quality_profile": None})

    result = analyze(rows, gold)
    assert result["n"] == 10, result
    assert result["agreement_rate"] == 0.8, result
    assert result["cohen_kappa"] == 0.5833, result
    assert result["confusion"] == {
        "judge_pass_gt_correct": 5,
        "judge_pass_gt_wrong": 1,
        "judge_fail_gt_correct": 1,
        "judge_fail_gt_wrong": 3,
    }, result
    assert len(result["disagreements"]) == 2, result
    assert len(result["excluded"]) == 1, result

    # kappa guard rails (ported behavior): too few samples and pe == 1.
    assert cohen_kappa(["pos"], ["pos"], ["pos", "neg"]) is None
    assert cohen_kappa(["pos"] * 5, ["pos"] * 5, ["pos", "neg"]) == 1.0

    print_report(result)
    print("\nselftest OK")
    return 0


# --- main -------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", help="path to a /results (preferred) or /export JSON file")
    parser.add_argument("--experiment-id", help="fetch /results live for this experiment")
    parser.add_argument("--base-url", default="http://localhost:8002")
    parser.add_argument("--token", default=os.environ.get("SPAWNHIVE_TOKEN"))
    parser.add_argument("--workspace-id", default=os.environ.get("SPAWNHIVE_WORKSPACE_ID"))
    parser.add_argument("--dataset", help="frozen dataset JSONL with gold reference_answer")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    if not args.dataset:
        parser.error("--dataset is required (e.g. research/datasets/gsm8k-50.jsonl)")
    if bool(args.input) == bool(args.experiment_id):
        parser.error("provide exactly one of --input or --experiment-id")

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            rows = json.load(fh)
    else:
        if not args.token or not args.workspace_id:
            parser.error(
                "--token/$SPAWNHIVE_TOKEN and --workspace-id/$SPAWNHIVE_WORKSPACE_ID "
                "are required with --experiment-id"
            )
        rows = fetch_results(args.experiment_id, args.base_url, args.token, args.workspace_id)

    if not isinstance(rows, list) or not rows:
        raise SystemExit("input contains no run rows")
    if not any("result_summary" in row for row in rows):
        raise SystemExit(
            "rows carry no result text (this looks like /export output) — use "
            "GET /api/experiments/{id}/results, which includes result_summary"
        )

    gold = load_gold(args.dataset)
    result = analyze(rows, gold)
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        print()
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
