#!/usr/bin/env python3
"""Sample 50 GSM8K test problems into the frozen SpawnHive upload dataset (SPA-50).

Provenance (deterministic):
- Input: the official GSM8K test split (1319 problems), e.g. downloaded from
  https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl
- Sampling: ``random.seed(42)`` then ``random.sample(range(len(rows)), 50)``
  over the full 1319-row list; the chosen 0-based indices are sorted ascending
  for a stable file order. ``case_id`` is ``gsm8k-<original_index>`` where
  ``original_index`` is the 0-based line number in test.jsonl.
- Gold answer: the text after the final ``#### `` marker in the "answer"
  field, with commas/spaces/"$" stripped (GSM8K convention), stored as a
  plain string in ``reference_answer``.

Each output line matches the UploadCase schema in
backend/app/quality/experiments.py (task_input{title, description}, case_id,
reference_answer, rubric with one critical "correctness" reference dimension).

Usage:
    python3 sample_gsm8k.py --input /path/to/test.jsonl \
        --output research/datasets/gsm8k-50.jsonl

Stdlib only; no third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import random
import sys

SEED = 42
N_SAMPLES = 50

ANSWER_MARKER = "#### "

INSTRUCTION = (
    "Solve the problem step by step. End your response with a line in exactly "
    "this format: FINAL ANSWER: <number>"
)

RUBRIC = {
    "name": "GSM8K correctness",
    "dimensions": [
        {
            "key": "correctness",
            "name": "Correctness",
            "description": (
                "Does the result state the correct final numeric answer to the "
                "math problem? Compare the FINAL ANSWER in the result against "
                "the reference (gold) answer; the number must match exactly "
                "(equivalent formatting such as '18', '18.0' or '$18' counts)."
            ),
            "evaluator": "reference",
            "reference_mode": "pointwise",
            "weight": 1,
            "threshold": 6,
            "critical": True,
        }
    ],
}


def gold_answer(answer_field: str) -> str:
    """The GSM8K gold final answer: text after the last '#### ' marker,
    normalized to a plain number string (no commas, spaces or $)."""
    if ANSWER_MARKER not in answer_field:
        raise ValueError(f"no '{ANSWER_MARKER.strip()}' marker in answer: {answer_field[:80]!r}")
    raw = answer_field.rsplit(ANSWER_MARKER, 1)[1].strip()
    return raw.replace(",", "").replace("$", "").replace(" ", "")


def build_case(index: int, row: dict) -> dict:
    return {
        "case_id": f"gsm8k-{index}",
        "task_input": {
            "title": f"GSM8K #{index}",
            "description": row["question"].strip() + "\n\n" + INSTRUCTION,
        },
        "reference_answer": gold_answer(row["answer"]),
        "rubric": RUBRIC,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", required=True, help="path to GSM8K test.jsonl (1319 rows)")
    parser.add_argument("--output", required=True, help="path for the frozen 50-case JSONL")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if len(rows) != 1319:
        print(f"warning: expected 1319 GSM8K test rows, got {len(rows)}", file=sys.stderr)

    random.seed(SEED)
    indices = sorted(random.sample(range(len(rows)), N_SAMPLES))

    with open(args.output, "w", encoding="utf-8") as out:
        for index in indices:
            out.write(json.dumps(build_case(index, rows[index]), ensure_ascii=False) + "\n")

    print(f"wrote {N_SAMPLES} cases to {args.output} (seed={SEED}, indices {indices[0]}..{indices[-1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
