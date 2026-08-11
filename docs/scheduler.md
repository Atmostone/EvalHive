# Scheduler (P8)

> Implementation: `backend/app/scheduler.py`, `app/api/scheduled_jobs.py`, the `scheduled_jobs` table.

## Architecture

`AsyncIOScheduler` (apscheduler) runs inside the dedicated `scheduler` container (post-R3). On startup:

1. `seed_default_jobs` — creates the built-in jobs if missing.
2. `reload_jobs` — reads every `enabled=true` row from the DB and registers it with the scheduler.
3. When a job is mutated via `/api/scheduled-jobs` (POST/PATCH/DELETE), `reload_jobs` runs again.

The container holds the Postgres advisory lock `8723452`. If multiple scheduler instances run, only the lock holder is active; the others sleep+retry — providing automatic failover.

## Job kinds

| kind | Trigger | Fields |
|------|---------|--------|
| cron | `CronTrigger.from_crontab(cron_expr)` | cron_expr |
| interval | `IntervalTrigger(seconds=interval_seconds)` | interval_seconds |
| once | `DateTrigger(run_date=fire_at)` | fire_at; the job is auto-disabled (`enabled=false`) after firing |

## Built-in jobs

`seed_default_jobs` seeds **15** jobs on startup if missing (matched by name), all attached to the default workspace. `_job_runner` dispatches each by its `payload.action`.

| name / action | kind | interval / expr | gating setting |
|---------------|------|-----------------|----------------|
| daily_cost_rollup | cron | `0 0 * * *` | — |
| agent_progress_check | interval | 60s | — |
| quality_record_backfill | interval | 300s | — |
| quality_record_retention | cron | `30 0 * * *` | `data_lake_retention_days` (0 = keep forever; > 0 enables pruning) |
| quality_judge_evaluate | interval | 600s | `quality_eval_enabled` |
| trajectory_judge_evaluate | interval | 600s | `trajectory_eval_enabled` |
| trace_evidence_evaluate | interval | 600s | `trace_evidence_eval_enabled` |
| capability_evaluate | interval | 600s | `capability_eval_enabled` |
| failure_mode_evaluate | interval | 600s | `failure_mode_eval_enabled` |
| hallucination_evaluate | interval | 600s | `hallucination_eval_enabled` |
| calibration_evaluate | interval | 600s | `calibration_eval_enabled` |
| variance_run_tick | interval | 20s | — |
| perturbation_run_tick | interval | 20s | — |
| experiment_run_tick | interval | 20s | — |
| pairwise_run_tick | interval | 20s | — |

### daily_cost_rollup

- kind: `cron`, expr: `0 0 * * *` (midnight UTC).
- Computes `sum(cost_usd)` / `count(tasks)` for tasks completed yesterday (scoped to the job's workspace).
- Writes an `agent_events` row with `event_type=daily_cost_summary`.

### agent_progress_check

- kind: `interval`, 60 seconds.
- For every active container it calls `runtime.health(...)` (P1) and writes `agent_events` with `event_type=agent_health` and the `current_step` / `iteration`.

### quality_record_backfill

- kind: `interval`, 300 seconds.
- Quality Data Lake (E-01): builds quality records for any terminal task (`done` / `failed`) that has none yet, and reconciles `final_status` of existing records. Global (all workspaces).
- Writes `event_type=quality_record_backfill` when anything was built/reconciled.

### quality_record_retention

- kind: `cron`, expr: `30 0 * * *`.
- Prunes quality records older than the `data_lake_retention_days` setting (`0` = keep forever; `public_dataset_opt_in` records are never auto-deleted). Deletes the backing blob too.
- Writes `event_type=quality_record_retention` when anything was deleted.

### quality_judge_evaluate

- kind: `interval`, 600 seconds. Gated by the `quality_eval_enabled` setting (off by default; the on-demand API button works regardless).
- Multi-dim Quality Rubric Engine (E-02): scores up to 10 terminal `done` records that have no `quality_profile` yet.
- Writes `event_type=quality_judge_batch`.

### trajectory_judge_evaluate

- kind: `interval`, 600 seconds. Gated by the `trajectory_eval_enabled` setting (off by default).
- 6-axis Trajectory Judge (E-07): scores up to 10 terminal `done` records that have no `trajectory_profile` yet.
- Writes `event_type=trajectory_judge_batch`.

### trace_evidence_evaluate

- kind: `interval`, 600 seconds. Gated by the `trace_evidence_eval_enabled` setting (off by default).
- TRACE Evidence Bank Judge (E-08): scores up to 5 terminal `done` records that have no `trajectory_evidence_profile` yet (smaller batch — N+1 calls per task).
- Writes `event_type=trace_evidence_batch`.

### capability_evaluate

- kind: `interval`, 600 seconds. Gated by the `capability_eval_enabled` setting (off by default).
- Capability-isolation Tests (E-13): runs the deterministic Glass-Box harness on up to 10 successful tasks (`done` / `awaiting_approval`) that carry a `capability_spec` but have no `capability_profile` yet.
- Writes `event_type=capability_batch`.

### failure_mode_evaluate

- kind: `interval`, 600 seconds. Gated by the `failure_mode_eval_enabled` setting (off by default).
- Failure Mode Classifier (E-14): classifies failure modes on up to 10 terminal `done` records that have no `failure_profile` yet.
- Writes `event_type=failure_mode_batch`.

### hallucination_evaluate

- kind: `interval`, 600 seconds. Gated by the `hallucination_eval_enabled` setting (off by default).
- Hallucination Detection (E-15): fact-checks up to 10 terminal `done` records that have no `hallucination_profile` yet.
- Writes `event_type=hallucination_batch`.

### calibration_evaluate

- kind: `interval`, 600 seconds. Gated by the `calibration_eval_enabled` setting (off by default).
- Confidence Calibration (E-16): probes up to 10 terminal `done` records that have no `calibration_profile` yet.
- Writes `event_type=calibration_batch`.

### variance_run_tick

- kind: `interval`, 20 seconds. No setting gate (cost is only incurred for user-created runs).
- Variance / Robustness Harness (E-11): advances every non-terminal run — creates the next children under the cost cap, evaluates finished ones, aggregates when complete.
- Writes `event_type=variance_run_tick` when anything advanced.

### perturbation_run_tick

- kind: `interval`, 20 seconds. No setting gate.
- Adversarial / Perturbation Judge (E-12): advances every non-terminal run — creates the next baseline/perturbed children under the cost cap, evaluates finished ones, aggregates when complete.
- Writes `event_type=perturbation_run_tick` when anything advanced.

### experiment_run_tick

- kind: `interval`, 20 seconds. No setting gate.
- Experiment Runner (SPA-40): advances every running experiment — settles finished matrix-cell runs, claims the next pending cells under the parallelism/budget limits, finalizes when everything is settled.
- Writes `event_type=experiment_run_tick` when anything advanced.

### pairwise_run_tick

- kind: `interval`, 20 seconds. No setting gate.
- Pairwise Comparison Framework (E-21): advances every comparison whose candidate B is still being generated — clones B from a rerun of the source, waits for it to finish, then auto-judges (llm mode).
- Writes `event_type=pairwise_run_tick` when anything advanced.

## Manual jobs

`POST /api/scheduled-jobs`:

```json
{
  "name": "string",
  "kind": "cron|interval|once",
  "cron_expr": "0 */6 * * *",
  "interval_seconds": null,
  "fire_at": null,
  "payload": {"action": "test"},
  "enabled": true
}
```

If `payload.action` is not one of the 15 built-in actions dispatched by `_job_runner` (see [Built-in jobs](#built-in-jobs)), the fallback `else` branch writes an `agent_events` row with `event_type=scheduled_job_fired` and the original `name` + `action` + `payload`.

## Extension

To add a new built-in action:

1. Add an `elif action == "my_action": …` branch in `_job_runner` (`app/scheduler.py`).
2. If you want the job created automatically, add a seed entry in `seed_default_jobs`.
3. Document it here.

After R5 these actions will live as plugins (e.g. `Notifier` or similar) without touching `_job_runner`.

## Known limitations

- Time-once jobs whose `fire_at` is already in the past will **not** fire on the next restart (APScheduler skips them by design).
- Built-in jobs live in the default workspace; user-created jobs are scoped to the creator's workspace (post-R1).
