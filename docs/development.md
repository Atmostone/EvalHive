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

The independent Python/TypeScript connector SDK and its shared protocol checks
are documented in [sdk/README.md](../sdk/README.md). They run without Docker,
database services or model credentials. Run both process suites before changing
the canonical schema; an incompatible change requires a new protocol version.

Backend image builds now use the repository root as their context:
`docker build -f backend/Dockerfile .`. Compose already supplies this path. The
image installs `sdk/python` and includes `harnesses`; direct host development must
also install `pip install -e sdk/python`. `HARNESS_MANIFEST_DIR` can select another
administrator-controlled declaration directory. Each connector occupies a
subdirectory containing `manifest.json`; worker packages/images are installed
separately, and no Python worker module is imported by catalog discovery.

The process runtime requires a rootless Docker daemon. `RUNTIME_NETWORK` selects
its registered bridge; `RUNTIME_MAX_CPU_QUOTA` (default 200000, with period 100000)
and `RUNTIME_MAX_MEMORY_MB` (default 2048) set server limits. Additional environment
dependency trees can be registered through `RUNTIME_READONLY_ROOTS` (JSON array of
absolute host paths); they cannot be mounted writable. `HOST_DATA_DIR` must be an
absolute host path. Each attempt gets separate `workspace` and private input
directories under `workspaces/<task>/<attempt>/`; the runner alone receives the
private inputs, and artifact collection starts at the workspace directory.

Install the runner alongside the SDK for host development: `pip install -e runner`.
`RUNNER_API_URL` defaults to the internal `http://api:8000`; `RUNTIME_MAX_CONCURRENCY`
defaults to one. Capacity is serialized across API replicas. SDK workers must wait
for the JSONL start command; starting the container alone must not invoke a model.
The runner's own API token stays outside the worker. Native credentials use a
separate read-only file named by `EVALHIVE_CREDENTIALS_FILE`; worker code resolves
the RunSpec references from that map and must not discover global auth caches.

Pi is the built-in default connector; OpenCode uses the same installed-package
contract. The previous executable agent and transitional adapters have been
removed. Historical journals, log archives and template versions remain readable.
Retired webhook endpoints authenticate old service tokens but reject execution
writes; feedback and cancellation compatibility endpoints queue attempt commands.

Build the shared workspace dependencies before either connector:

```bash
docker build --target runtime -f environments/workspace/Dockerfile -t evalhive-environment-workspace:0.1.0 .
```

This environment contains Python and the existing time/web MCP servers at their
registry paths. Its Python base is pinned by digest and dependencies by hashes.
Connector images add their own pinned Node runtime and native SDK, while custom
benchmark base images provide their independent toolchains.

Build the Pi connector from the repository root. Its Node/Pi/SDK dependency
versions are pinned by the package lockfile. Native tests use the actual Pi SDK
with an in-container local OpenAI-compatible fixture; the container needs no
external network and no model credentials:

```bash
docker build --target test -f harnesses/pi/Dockerfile -t evalhive-pi-tests:harness-dev .
docker run --rm --network none --memory 2g --cpus 1 --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges:true --init evalhive-pi-tests:harness-dev
docker build --target runtime -f harnesses/pi/Dockerfile -t evalhive-pi:1.5.0 .
```

Use the EvalHive rootless Docker endpoint on the VPS. Tests run serially. The
runtime stage includes production dependencies only. Pi requires an explicit
OpenAI-compatible `RunSpec.model` (`name`, `endpoint`, `credential_ref`); the key is
looked up only in the private credential input. Agent/session/auth state stays in
an isolated temporary directory, outside artifacts. Native resource/MCP/skill
translation lives at the connector boundary. Pi uses the explicitly pinned
`@earendil-works/pi-mcp` client for stdio and Streamable HTTP, exposes native MCP
names with canonical aliases, and closes clients with the session. Native tests
cover both transports, private authorization, explicit skills, disabled memory
and an empty toolset.

When a rootless `toolathlon-pack:latest` image is installed, build the registered
environment variant using the same worker and lockfile:

```bash
docker build --target runtime --build-arg BASE_IMAGE=toolathlon-pack:latest -f harnesses/pi/Dockerfile -t evalhive-pi-toolathlon:1.5.0 .
```

The base supplies its Python/MCP/benchmark dependencies; the connector pins its
own Node runtime. This does not run a benchmark. The task environment must pass
its MCP environment references (including per-lane database coordinates) and
prepared inputs. Portal tasks retain their owned environment network namespace.

The [OpenCode connector](../harnesses/opencode/README.md) uses the same SDK and
resource declarations. Its CLI and official TypeScript SDK are pinned to 1.19.5.
Build and test it serially with the same rootless endpoint and resource limits:

```bash
docker build --target test -f harnesses/opencode/Dockerfile -t evalhive-opencode-tests:harness-dev .
docker run --rm --network none --memory 2g --cpus 1 --pids-limit 256 --cap-drop ALL --security-opt no-new-privileges:true --init evalhive-opencode-tests:harness-dev
docker build --target runtime -f harnesses/opencode/Dockerfile -t evalhive-opencode:1.19.5 .
```

These tests run the actual native server against local HTTP/MCP fixtures. They
cover feedback persistence, approval, SSE recovery, generation/tool cancellation,
server failure, unknown usage, exact tool selection, skills and hard turn limits.
The initial runtime image targets Linux/amd64. No provider configuration or
credentials are needed. The environment-image build argument also applies to
OpenCode; a Toolathlon base must be installed separately before building that
variant.

## Migrations

SDK runs save the connector's effective public configuration before execution.
New Data Lake records identify their source attempt; an archived execution is
rerun as a linked task. Recorded replay requires the same installed declaration,
image and model connection. Legacy snapshots remain readable, but their retired
agent cannot be replayed as Pi under the same conditions.

Bundle exports with `--with-traces` include SDK attempt headers and ordered
normalized/native JSONL journals in `blobs/attempts/`. Verification checks their
digests and expected count. Journal export is bounded to 256 MiB and fails
explicitly rather than truncating evidence. Remote model revisions, credential
versions and point-in-time external systems are not pinned by a harness image.

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

The image ships only `requirements.txt` — CI installs the dev extras on the runner, so
`pytest` is **not** in the container until you put it there. It is also lost on every
container restart, which is why the install is part of the command rather than a setup step
you are assumed to remember:

```bash
docker compose exec api sh -c 'pip install -q -r requirements-dev.txt && \
  TEST_DATABASE_URL=postgresql+asyncpg://evalhive:password@postgres:5432/evalhive_test \
  pytest tests/unit -q'
```

`TEST_DATABASE_URL` is required in the container: conftest defaults to `localhost:5432`,
which does not resolve there, and the whole suite dies at the `_migrate` fixture with an
unhelpful `getaddrinfo() returned empty list`.

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
