# SpawnHive research assets (SPA-50)

Experiment program RQ1/RQ2. This directory holds frozen datasets, payload
builders and offline analysis scripts. Everything runs on plain `python3`
(stdlib only).

## Layout

| Path | What it is |
|---|---|
| `datasets/gsm8k-50.jsonl` | Frozen 50-problem GSM8K sample, one UploadCase per line (the schema of `backend/app/quality/experiments.py::UploadCase`) |
| `datasets/gsm8k_experiment_payload.json` | Ready-to-POST `/api/experiments` body ("RQ1a GSM8K judge-vs-GT") |
| `scripts/sample_gsm8k.py` | Deterministic sampler: GSM8K test split → `gsm8k-50.jsonl` |
| `scripts/build_gsm8k_payload.py` | `gsm8k-50.jsonl` → `gsm8k_experiment_payload.json` |
| `scripts/gsm8k_agreement.py` | Offline judge-vs-ground-truth agreement analysis (RQ1a) |

## Dataset provenance (gsm8k-50.jsonl)

- Source: the official GSM8K **test** split (1319 problems),
  <https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl>.
- Sampling: `random.seed(42)` then `random.sample(range(1319), 50)`; the
  chosen 0-based indices are sorted ascending for a stable file order. The
  sampled index range is 13..1309.
- `case_id` = `gsm8k-<original_index>` (0-based line number in `test.jsonl`).
- `reference_answer` = the text after the final `#### ` marker in the GSM8K
  "answer" field, with commas/spaces/`$` stripped (a plain number string).
- `task_input.description` = the question text + two newlines + a fixed
  instruction to end with `FINAL ANSWER: <number>` (makes the offline
  extraction deterministic).
- Each case carries an inline rubric with a single dimension:
  `correctness` / evaluator `reference` (E-03, `reference_mode: pointwise`) /
  weight 1 / threshold 6 / critical. The E-03 judge scores the agent result
  against `reference_answer` 0-10; pass = score >= 6.

Regenerate (bit-identical) with:

```bash
curl -sSL -o /tmp/gsm8k_test.jsonl \
  https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl
python3 research/scripts/sample_gsm8k.py --input /tmp/gsm8k_test.jsonl \
  --output research/datasets/gsm8k-50.jsonl
python3 research/scripts/build_gsm8k_payload.py
```

## Creating and running the experiment

The payload pins one configuration (label `glm-4.7 writer`, orchestrator off,
`template_id` Writer, `model_id` glm-4.7), `n_runs_per_cell: 1`,
`budget_limit_usd: 2.0`, `max_parallel: 3` — a 1 × 50 × 1 = 50-run matrix.

```bash
BASE=http://localhost:8002          # API port of the running stack
TOKEN=...                            # a workspace owner/admin bearer token
WS=c6986b6e-4fd1-4f9e-8a45-3da2698d3c2b

# 0) optional stateless dry-run (no rows created)
curl -sS -X POST "$BASE/api/experiments/preview" \
  -H "Authorization: Bearer $TOKEN" -H "X-Workspace-Id: $WS" \
  -H "Content-Type: application/json" \
  --data-binary @research/datasets/gsm8k_experiment_payload.json
# -> {"n_configs":1,"n_cases":50,"n_runs_per_cell":1,"total_runs":50,...}

# 1) create (draft)
EXP_ID=$(curl -sS -X POST "$BASE/api/experiments" \
  -H "Authorization: Bearer $TOKEN" -H "X-Workspace-Id: $WS" \
  -H "Content-Type: application/json" \
  --data-binary @research/datasets/gsm8k_experiment_payload.json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

# 2) start (materializes the 50 cells; the scheduler tick drives it)
curl -sS -X POST "$BASE/api/experiments/$EXP_ID/run" \
  -H "Authorization: Bearer $TOKEN" -H "X-Workspace-Id: $WS"

# 3) watch progress
curl -sS "$BASE/api/experiments/$EXP_ID" \
  -H "Authorization: Bearer $TOKEN" -H "X-Workspace-Id: $WS" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d['run_totals'])"
```

Evaluation runs automatically on the experiment settle path: the E-02 quality
judge always runs and scores the per-case rubric (so the `correctness`
reference dimension lands in `quality_profile.dimensions`); trajectory eval is
left enabled (`eval_config: {"trajectory": true, "failure_modes": false}`).

## Judge-vs-ground-truth agreement (RQ1a)

Once the experiment is terminal, compare the E-03 judge verdict against exact
ground truth. Use the **/results** endpoint (it includes `result_summary`;
the flat `/export` rows do not carry result text):

```bash
curl -sS "$BASE/api/experiments/$EXP_ID/results" \
  -H "Authorization: Bearer $TOKEN" -H "X-Workspace-Id: $WS" > /tmp/results.json
python3 research/scripts/gsm8k_agreement.py \
  --input /tmp/results.json --dataset research/datasets/gsm8k-50.jsonl

# or fetch live:
SPAWNHIVE_TOKEN=$TOKEN SPAWNHIVE_WORKSPACE_ID=$WS \
  python3 research/scripts/gsm8k_agreement.py \
  --experiment-id "$EXP_ID" --base-url "$BASE" \
  --dataset research/datasets/gsm8k-50.jsonl
```

Per run the script extracts the agent's final number (the last
`FINAL ANSWER:` line, falling back to the last number in the text, with
`$`/commas normalized), compares it to the gold answer (tolerance 1e-6), and
sets `judge_pass` = correctness score >= 6. It reports n, raw agreement rate,
Cohen's kappa (ported verbatim from `backend/app/quality/stats.py`,
dependency-free), a 2x2 confusion matrix and a disagreements table.
`--json` emits machine-readable output; `--selftest` proves the math on a
hand-checked example (kappa 0.5833 on a fabricated 10-row set).

## GAIA gate status (RQ2 scouting, checked 2026-06-11)

`https://huggingface.co/api/datasets/gaia-benchmark/GAIA` reports
`"gated": "auto"`; an unauthenticated file download
(`/datasets/gaia-benchmark/GAIA/resolve/main/2023/validation/metadata.jsonl`)
returns **401**. Access requires a Hugging Face account, accepting the gate
conditions on the dataset page (auto-approved checkbox: agree not to reshare),
then downloading with an HF token (`huggingface-cli download
gaia-benchmark/GAIA --repo-type dataset --token $HF_TOKEN`).

## Результат RQ1a / GSM8K (2026-06-11/12)

Эксперимент bc494ee5 (50 кейсов × glm-4.7, n=1): **judge vs exact GT — agreement 1.0, κ=1.0 (n=49)**.
- `exports/gsm8k_agreement.json` — финальный результат (после фикса экстрактора).
- `exports/gsm8k_agreement_v1_raw_extraction.json` — первая версия (agreement 0.98, κ=0.846): единственное «расхождение» (gsm8k-318) оказалось артефактом извлечения GT-ответа из имени файла `gsm8k_318_solution.txt`, а не ошибкой судьи — LLM-судья устойчивее наивного regex-извлечения. Методологический вывод для документа.
- Исключение gsm8k-689: агент вернул «Task completed» без ответа где-либо — судья консистентно поставил 0 (fail); GT-сторона неизвестна, кейс исключён честно.
