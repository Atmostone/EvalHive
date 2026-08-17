# Webhook protocol (agent → orchestrator)

> P3 formalised the contract via a Pydantic discriminated union; R2 added auth + idempotency + URL versioning; R6 made the whole flow atomic.
> Source of truth: `backend/app/schemas/webhooks.py` + `backend/app/api/webhooks.py`.

## Endpoint (R2)

Canonical path: **`POST /api/v1/agent-webhook/{task_id}`**.

The legacy alias `POST /api/agent-webhook/{task_id}` stays around until 2026-08-01 — it returns the same status codes plus `Sunset` / `Deprecation` / `Link` headers (even on 4xx responses).

Headers:
- `Authorization: Bearer <SPAWNHIVE_AGENT_TOKEN>` — required. A missing, invalid, or expired token returns 401.
- `Content-Type: application/json`.

Pydantic validation runs **first**, before the Task lookup, to prevent timing-side-channel probes for valid task ids.

## Idempotency (R2 + R6 atomicity)

Every webhook should carry an `idempotency_key`. The handler reserves a row in `webhook_deliveries(task_id, event_type, idempotency_key)` (UNIQUE `(task_id, idempotency_key)`) and runs all task mutations + event writes inside a **single transaction** that commits the delivery row last.

Outcomes:
- Concurrent replay → one wins on UNIQUE, the loser rolls back its in-flight mutations and returns `{"status":"duplicate"}` cleanly.
- Crash mid-processing → no delivery row was committed → the retry processes successfully (no false "duplicate").
- Successful pass → 200 `{"status":"ok"}`.

The agent generates `idempotency_key = uuid.uuid4().hex` for every `_send_progress` and the final `report_webhook`.

## Retry

`agent-image/entrypoint.py:report_webhook` — exponential backoff 0/2/4/8s (4 attempts). On final failure the payload is written to `/tmp/failed_webhooks.json` (post-mortem only, not a delivery guarantee).

`agent-image/agent.py:_send_progress` — no retry (2s timeout, errors are suppressed): missing a progress update is preferable to blocking the tool loop.

## Envelope

```json
{
  "schema_version": "1.0",
  "event": "completed | failed | progress | aborted",
  "timestamp": "2026-05-03T11:42:00Z",
  "task_id": "uuid",
  "idempotency_key": "uuid-hex",
  "data": { ...event-specific... }
}
```

`schema_version` is optional with default "1.0". `task_id` in the body is redundant with the URL path, but allowed.

`event` is the discriminator. The shape of `data` depends on `event`.

## Event variants

### event = "progress"

```json
{
  "current_step": "tool:bash",
  "tool_name": "bash",
  "iteration": 3,
  "tokens_used_so_far": {"input": 1200, "output": 80},
  "recent_output": "stdout snippet truncated to 500 chars"
}
```

All fields are optional. The agent sends **no more than** one update every 5 seconds (rate-limited inside `agent.py`).

The orchestrator persists this as an `agent_events` row with `event_type=agent_progress`.

### event = "completed"

```json
{
  "result_summary": "string",
  "files": ["list/of/output/paths"],
  "token_usage": {"input_tokens": 1633, "output_tokens": 95}
}
```

Orchestrator:
1. Stores summary/files/tokens on the task.
2. Computes `cost_usd` (via the `model_pricing` setting).
3. Uploads files from `/workspace/output/` to MinIO.
4. Moves the task to `review`.
5. Calls LLM `evaluate_agent_result` → `awaiting_approval` (approved) or retry/failed.
6. If `memory_mode=structured` and approved, runs `extract_memory(task_id)` in the background.

### event = "failed"

```json
{
  "error": "human-readable message",
  "failure": {
    "kind": "llm_error | cap_hit | tool_leak | crash",
    "exception": "RateLimitError",
    "status_code": 429,
    "message": "first 500 chars of the exception"
  },
  "token_usage": {"input_tokens": 0, "output_tokens": 0}
}
```

`failure` (SPA-87) carries **facts, not a verdict**. The agent knows the site of
the failure — a cap-hit is not an exception, a crash has no HTTP status — and
passes the exception class and status through untyped; the backend turns them
into `tasks.failure_type` with the single classifier in `app/utils/failures.py`,
so that judgement exists once instead of in both images. The agent keeps its own
transient-error predicate, but that one only decides whether to retry an HTTP
call: it can drift from the backend's table without changing whether a run counts
as infrastructure-contaminated. The whole object is optional — an image built
before it existed leaves `failure_type` NULL, which means «not classified» and is
treated as ordinary data.

Orchestrator: retries when `retry_count < max_retries` (never on the benchmark
path, where a failure is a result), otherwise marks the task as `failed`. A task
put back to `ready` has its `failure_type` cleared — it has not failed.

### event = "aborted" (P1)

```json
{
  "reason": "user requested",
  "token_usage": {"input_tokens": ..., "output_tokens": ...}
}
```

Sent by the agent after receiving an `/abort` command. The orchestrator marks the task as `failed` and emits an `agent_aborted` event.

## TokenUsage — aliases

`TokenUsage` accepts both naming conventions (via `AliasChoices`):
- `input_tokens` or `input`
- `output_tokens` or `output`

Internally it is normalised to `input_tokens / output_tokens`. The agent uses `input/output` in `progress` events and `input_tokens/output_tokens` in completed/failed/aborted.

## Validation

- The `TypeAdapter` validates the body **before** any task lookup (R1 fix). Invalid body → 422 + `webhook_validation_failed` event in `agent_events`.
- Discriminated union: an unknown `event` → 422 (`union_tag_invalid`).
- Unknown `task_id` → 200 + `{"status":"error","detail":"Task not found"}` (but only after the schema passes validation).

## OpenAPI

Available at `/openapi.json`. The discriminated union is rendered as `oneOf` with the `event` discriminator.

## Sister channel — `POST /api/v1/agent-log/{task_id}` (Foundations Этапы 1–2)

Same auth model (`Authorization: Bearer <SPAWNHIVE_AGENT_TOKEN>` + `idempotency_key`), separate idempotency table (`agent_log_deliveries`). The agent posts full tool stdout/stderr in chunks here while keeping the existing `agent_progress` webhook (with the 500-char `recent_output` ticker) untouched — the dashboard live-status uses progress, the task-drawer log viewer uses log chunks. After event=completed/failed/aborted the webhook handler compacts chunks to a MinIO blob and prunes `agent_log_chunks`. See `docs/architecture.md` (`Frontend / Tasks (AgentLogViewer)`) and `docs/data-model.md` (`agent_log_chunks`, `agent_log_deliveries`, `tasks.log_archive_s3_path`).
