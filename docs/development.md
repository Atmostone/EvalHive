# Development

## Running locally

Requirements: Docker + docker compose v2.

```bash
git clone <repo>
cd EvalHive
cp .env.example .env   # edit LLM_BASE_URL/API_KEY
docker compose up -d
docker compose exec api alembic upgrade head
```

Building the api image with `GIT_SHA=$(git rev-parse HEAD) docker compose build api` stamps the
commit into it, so a reproduction bundle (SPA-90) can name the checkout it must be recomputed
with. Without it a bundle still exports, and its manifest says the checkout is unpinned rather
than leaving the field silently empty:

```bash
GIT_SHA=$(git rev-parse HEAD) docker compose build api
docker compose exec api python -m app.cli.bundle export --experiment-id <uuid>
docker compose exec api python -m app.cli.bundle verify --bundle /tmp/bundle-<uuid>.tar.gz
```

UI: http://localhost:3002 — Vite dev (HMR). Frontend stack: React 18 + TypeScript + Vite + Tailwind + TanStack Query + Zustand + react-router-dom + reactflow + recharts (for the Analytics page).
API: http://localhost:8002 — FastAPI behind nginx LB.
OpenAPI: http://localhost:8002/docs.

## Services

| Service | Host port | Purpose |
|---------|-----------|---------|
| nginx | 8002 | Reverse proxy / load balancer for the api replicas |
| api | — (expose 8000) | FastAPI; REST + WS only (orchestrator/scheduler are separate) |
| orchestrator | — | Polling loop; holds advisory lock 8723451 |
| scheduler | — | APScheduler; holds advisory lock 8723452 |
| frontend | 3002 | Vite dev (3001 was taken by another project on the host) |
| postgres | 5432 | |
| qdrant | 6333 / 6334 | |
| minio | 9000 / 9001 | console on :9001 |
| redis | — | Pub/sub for cross-replica WS event fan-out |

## Agent image

```bash
docker build -t evalhive-agent:latest agent-image/
```

Rebuild whenever `agent-image/*.py` or `requirements.txt` changes. The API uses this image through the Docker socket.

## Migrations

Create a new one:

```bash
docker compose exec api alembic revision -m "what changed"
# edit backend/alembic/versions/<rev>.py — fill in upgrade/downgrade
docker compose exec api alembic upgrade head
```

Roll back the last one:

```bash
docker compose exec api alembic downgrade -1
```

**Rule:** every PR that adds a migration must include a working `downgrade`. CI enforces a round-trip migration test.

## Backup and restore

A Postgres dump is **not** a backup of this stand. `quality_records.record_s3_path`,
`tasks.log_archive_s3_path` and `knowledge_documents.s3_path` hold keys into MinIO, not
content — traces, execution snapshots and deliverables live in the `evalhive_miniodata`
volume. Dump the database alone and you restore rows that point into nothing.

```bash
docker compose up -d postgres minio            # both must be up
scripts/backup.sh                              # -> ~/evalhive-backups/evalhive-backup-<UTC>/
scripts/backup.sh --out /some/where            # somewhere else
```

Each backup directory holds `db.dump` (pg_dump `-Fc`), `minio.tar.gz` (the whole volume,
`.minio.sys` included), `annotations.json` and a `manifest.json` that pairs them: sha256
per artifact, row counts, and the result of looking up **every** S3 key from the database
in the volume being tarred. `blob_pairing.status` is one of `verified` (keys checked, all
resolved), `nothing_to_check` (the stand holds no keys), `incomplete` (keys point at
objects the volume lacks) or `suspicious` (collection disagreed with the database, so the
check proved less than it appears to). Only the first two set `complete: true`.

The backup is still written when the pairing fails — a backup is insurance, and refusing
to preserve an imperfect stand would destroy the only copy of it — but the exit code is
non-zero and the manifest says so. This is deliberately the opposite choice from the
reproduction bundle (SPA-90), which refuses to write an archive that does not verify,
because a bundle is a *claim* and a bad one should not exist.

A backup that has never been restored is a hypothesis:

```bash
scripts/restore.sh --backup <dir> --scratch    # rehearse — throwaway container + volume
scripts/restore.sh --backup <dir> --scratch --keep   # leave them up for inspection
```

The rehearsal verifies the artifacts against the manifest before touching anything,
restores into a temporary Postgres of the same image and a temporary volume, re-asks the
pairing question of the **restored** pair, and compares row counts against the manifest.
It cannot reach the live stand.

The disaster path replaces the real database and volume, refuses to run while `api` /
`scheduler` / `orchestrator` / `minio` are up, and requires the phrase to be typed:

```bash
docker compose stop api scheduler orchestrator minio
scripts/restore.sh --backup <dir> --live
```

## Tests

```bash
docker compose exec api pytest                 # full suite
docker compose exec api pytest --cov=app       # with coverage
```

CI (`.github/workflows/ci.yml`) enforces `--cov-fail-under=60`. Conftest creates `evalhive_test` DB; if missing, run `docker compose exec postgres createdb -U evalhive evalhive_test` once.

## Useful curl commands (after R1)

```bash
# Register / login — obtain a JWT
TOK=$(curl -s -X POST http://localhost:8002/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"me@example.com","password":"strongpass1","display_name":"Me"}' \
  | jq -r .access_token)

# Create a task
curl -X POST http://localhost:8002/api/tasks \
  -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" \
  -d '{"title":"do X","priority":"high","description":"…"}'

# Move it to ready (orchestrator picks it up)
curl -X PATCH http://localhost:8002/api/tasks/<id> \
  -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" -d '{"status":"ready"}'

# Approve after awaiting_approval
curl -X PATCH http://localhost:8002/api/tasks/<id>/approve \
  -H "Authorization: Bearer $TOK"

# Slash command via WS chat (token in query string)
echo '{"content":"/status"}' \
  | websocat "ws://localhost:8002/ws/chat?token=$TOK"

# Memory entities
curl -H "Authorization: Bearer $TOK" http://localhost:8002/api/memory/entities | jq .
```

After R1, every endpoint except `/api/auth/*`, `/api/health`, `/api/v1/agent-webhook/*` returns 401 without `Authorization`.

## JWT_SECRET

`.env` must contain `JWT_SECRET=<64-byte hex>`. Generate one with `python -c "import secrets; print(secrets.token_hex(64))"`. The placeholder in `.env.example` is for dev only — replace it with your own value.

## Where to look at logs

```bash
docker compose logs -f api          # backend
docker compose logs -f frontend     # vite
docker logs <agent-container-id>    # individual agent
```

All events are also written to `agent_events`; query them via `/api/events?...` or the WebSocket.

## AI assistant instructions

See the root `CLAUDE.md`. It doesn't contradict this folder — it just lists short working rules (style, DRY/KISS, ask before picking models, docker-only runs).

## Pre-PR checklist

1. ✅ Migration (if needed) with working upgrade and downgrade.
2. ✅ Relevant files in `docs/` are updated.
3. ✅ `pytest` is green and coverage stays at or above 60% (CI gate).
4. ✅ No new workarounds without a `docs/workarounds.md` entry.
5. ✅ No backwards-compatibility shims "just in case".
6. ✅ No stubs/mocks left in production code.
