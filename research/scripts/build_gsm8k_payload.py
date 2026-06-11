#!/usr/bin/env python3
"""Build the ready-to-POST /api/experiments body for RQ1a (SPA-50).

Reads the frozen 50-case dataset (research/datasets/gsm8k-50.jsonl, one
UploadCase JSON object per line) and emits a complete experiment-create
payload matching the ExperimentCreate model in backend/app/api/experiments.py:

    {name, description, dataset: {source: "upload", cases: [...]},
     configurations: [...], n_runs_per_cell, budget_limit_usd, max_parallel,
     eval_config}

The single configuration pins the writer template + glm-4.7 model with the
orchestrator off (``"orchestrator": false`` + required ``template_id``, per
expand_matrix/_config_errors in backend/app/quality/experiments.py).

eval_config: the E-02 quality judge (which scores the per-case "correctness"
reference dimension via E-03) ALWAYS runs on the experiment settle path
(_evaluate_child); "trajectory" defaults on and "failure_modes" defaults off —
both are set explicitly here for provenance.

Usage:
    python3 build_gsm8k_payload.py \
        [--dataset research/datasets/gsm8k-50.jsonl] \
        [--output research/datasets/gsm8k_experiment_payload.json]

Defaults resolve relative to the repository's research/ directory. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import pathlib

RESEARCH_DIR = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATASET = RESEARCH_DIR / "datasets" / "gsm8k-50.jsonl"
DEFAULT_OUTPUT = RESEARCH_DIR / "datasets" / "gsm8k_experiment_payload.json"

EXPERIMENT_NAME = "RQ1a GSM8K judge-vs-GT"
TEMPLATE_ID = "f5fa7fb5-6616-4b0c-b9aa-b4f519c28213"
MODEL_ID = "5de518ab-85e9-4e9a-8f16-af7e8b9136af"


def build_payload(cases: list[dict]) -> dict:
    return {
        "name": EXPERIMENT_NAME,
        "description": (
            "RQ1a (SPA-50): 50 GSM8K test problems (seed-42 sample, see "
            "research/datasets/gsm8k-50.jsonl) run through the Experiment "
            "Runner; the per-case 'correctness' reference dimension (E-03 "
            "pointwise judge vs reference_answer) is later compared against "
            "exact ground truth by research/scripts/gsm8k_agreement.py."
        ),
        "dataset": {"source": "upload", "cases": cases},
        "configurations": [
            {
                "label": "glm-4.7 writer",
                "orchestrator": False,
                "template_id": TEMPLATE_ID,
                "model_id": MODEL_ID,
            }
        ],
        "n_runs_per_cell": 1,
        "budget_limit_usd": 2.0,
        "max_parallel": 3,
        "eval_config": {"trajectory": True, "failure_modes": False},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    with open(args.dataset, encoding="utf-8") as fh:
        cases = [json.loads(line) for line in fh if line.strip()]
    if len(cases) != 50:
        raise SystemExit(f"expected 50 cases in {args.dataset}, got {len(cases)}")

    payload = build_payload(cases)
    with open(args.output, "w", encoding="utf-8") as out:
        json.dump(payload, out, ensure_ascii=False, indent=2)
        out.write("\n")
    print(f"wrote {args.output}: {len(cases)} cases, 1 configuration, n_runs_per_cell=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
