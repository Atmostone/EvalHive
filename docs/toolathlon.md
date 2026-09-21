# Toolathlon-GYM infrastructure (SPA-42)

How to run the [eigent-ai/toolathlon_gym](https://github.com/eigent-ai/toolathlon_gym)
environment next to our stack: the mock PostgreSQL as a compose profile and a derived
agent image that combines their MCP toolchain with our agent loop. Background and the
adoption verdict: [research-toolathlon-gym.md](research-toolathlon-gym.md).

## Prerequisites

A local clone of the dataset repo (it is an **external dependency** — we do not vendor
the 503 tasks or the 8.2 MB DB dump into git):

```bash
git clone https://github.com/eigent-ai/toolathlon_gym ../toolathlon_gym
```

`TOOLATHLON_GYM_PATH` — path to that clone, used by `docker-compose.yml`.
Default: `../toolathlon_gym` (sibling of the EvalHive repo, relative to the compose
file). Override via the environment or `.env`:

```bash
TOOLATHLON_GYM_PATH=/abs/path/to/toolathlon_gym
```

## 1. Database: `toolathlon_pg` (compose profile `toolathlon`)

```bash
docker compose --profile toolathlon up -d toolathlon_pg
```

The service is behind the `toolathlon` profile, so plain `docker compose up -d` never
starts it. postgres:15 is initialized on **first** start from
`${TOOLATHLON_GYM_PATH}/db/init.sql.gz` (≈1–2 min to load; 15 task schemas: `arxiv`,
`arxiv_latex`, `canvas`, `email`, `gcal`, `gform`, `gsheet`, `notion`, `scholarly`,
`sf`, `sf_data`, `train`, `wc`, `yf`, `youtube` — plus `public`). Verify:

```bash
docker compose --profile toolathlon exec toolathlon_pg \
  psql -U eigent -d toolathlon_gym -c '\dn'
```

Fixed coordinates (their MCP configs `configs/mcp_servers/*.yaml` and scripts hardcode
these — do not change):

| Parameter | Value |
|---|---|
| container/host name | `toolathlon_pg` |
| user / password / db | `eigent` / `camel` / `toolathlon_gym` |
| network | `evalhive-net` — the same network agent containers are attached to (`backend/app/orchestrator/docker_manager.py: DOCKER_NETWORK = "evalhive_evalhive-net"`), so agents resolve `toolathlon_pg` via Docker DNS |

Data persists in the named volume `toolathlon-pgdata`. Task `preprocess/main.py`
scripts reset state before each run; for a **full** re-init from the dump:

```bash
docker compose --profile toolathlon down toolathlon_pg
docker volume rm evalhive_toolathlon-pgdata
docker compose --profile toolathlon up -d toolathlon_pg
```

Known upstream quirk: `init.sql.gz` creates `email.sent_log` without `ON DELETE
CASCADE`; their runners (`scripts/run_containerized.sh`, `run_parallel.sh`) re-create
the FK before every run. Our runner glue must do the same.

## 2. Images: build order

```bash
# 1) Their base image (ubuntu 22.04 + uv + node 22 + playwright chromium
#    + 19 pre-built MCP servers in /opt/local_servers + venv at /opt/venv).
#    Heavy: several GB, tens of minutes cold.
docker build -t toolathlon-pack:latest "$TOOLATHLON_GYM_PATH"

# 2) Our derived agent image: their toolchain + our agent.py/entrypoint.py/…,
#    python deps (litellm, mcp, fastapi, uvicorn, httpx) installed into THEIR
#    venv /opt/venv via uv (the venv has no pip).
docker build -f agent-image/Dockerfile.toolathlon \
  -t evalhive-agent-toolathlon:latest agent-image/
```

Rebuild #2 whenever `agent-image/*.py` or `requirements.txt` changes (same rule as the
regular `evalhive-agent:latest`).

> Docker Hub geo-block workaround: if pulls of `ubuntu:22.04` / `postgres:15` fail
> with `403 Forbidden` from registry-1.docker.io, pull the same images from the AWS
> mirror and retag:
> `docker pull public.ecr.aws/docker/library/ubuntu:22.04 && docker tag public.ecr.aws/docker/library/ubuntu:22.04 ubuntu:22.04`
> (likewise for `postgres:15`), then build with `--pull=false`.

## 3. Agent containers: PG env passthrough

Toolathlon MCP servers and the task `preprocess`/`evaluation` scripts read the DB
coordinates from the environment. When spawning an agent from
`evalhive-agent-toolathlon:latest`, pass (mirrors their `run_containerized.sh` /
`run_parallel.sh`, both `PGHOST` and `PG_HOST` spellings are used upstream):

```
PGHOST=toolathlon_pg
PG_HOST=toolathlon_pg
PGPORT=5432
PGUSER=eigent
PGPASSWORD=camel
PGDATABASE=toolathlon_gym
LOCAL_SERVERS_PATH=/opt/local_servers
PYTHON_BIN=/opt/venv/bin/python3
```

In our stack this goes through `extra_env` of
`docker_manager.spawn_agent()` — no code change needed for a serialized pilot. The
image keeps the toolathlon_gym source in `/workspace`; the orchestrator mounts the
task workspace over it, which is fine — we only use their `preprocess`/`evaluation`
scripts and `/opt/local_servers`, never their agent loop (`main.py`).

## 4. Evaluation: `--launch_time` must match preprocess

409/503 evaluation scripts query the live DB and many checks are date-based. Their
harness passes the **same** `launch_time` (format `YYYY-MM-DD HH:MM:SS`, captured once
at run start) to both `preprocess/main.py --launch_time …` and
`evaluation/main.py --agent_workspace … --groundtruth_workspace … --launch_time …`
(see `utils/roles/task_agent.py` and `utils/evaluation/evaluator.py`). Re-using a
different timestamp at eval time produces spurious date-based FAILs — our runner glue
must capture `launch_time` once per run and pass it to both phases.

## 5. Parallel runs: per-run postgres, not the shared DB

The shared `toolathlon_pg` permits **serialized runs only**: every task's
`preprocess` DELETEs/INSERTs across the same schemas, so two concurrent runs corrupt
each other's state. Upstream encodes this directly:

- `scripts/run_containerized.sh` serializes runs on the shared DB with an flock-style
  lock;
- `run_parallel.sh` gives **each task its own** `postgres:15` container (initialized
  from the same `db/init.sql.gz`), its own agent container and its own Docker
  network, tears all three down after the task, and throttles concurrency with a
  FIFO semaphore.

EvalHive ships a per-**lane** variant of this scheme (SPA-69). An experiment opts in
via `Experiment.n_toolathlon_lanes` (1..`MAX_TOOLATHLON_LANES = 4`,
`backend/app/quality/experiments.py`); `_lanes_enabled()` treats `NULL`/`< 1` as the
legacy serial path on the single shared `toolathlon_pg`, which stays the default —
existing experiments are untouched. With lanes enabled, the scheduler pins each run to
a lane and stamps `Experiment.lane_index` (0..n-1), and `_pg_host_for_lane(lane_index)`
routes that run's preprocess/eval/agent to host `toolathlon_pg_lane_<lane_index>` (only
the host varies — db name, user and password stay identical, so the gym scripts need no
changes; `None` falls back to `toolathlon_pg`).

The lanes are N **static** per-lane `postgres:15` containers
(`toolathlon_pg_lane_0..7` in `docker-compose.yml`), each an independent clone of the
gym DB from the same `db/init.sql.gz` on its own named volume, behind the
`toolathlon-lanes` compose profile:

```bash
docker compose --profile toolathlon-lanes up -d
```

The shared `toolathlon_pg` remains serial-only (concurrent runs on one DB corrupt each
other's state, per above); it serves ad-hoc/manual use and serial runs (`NULL`/1 lane),
while parallel runs fan out across the lane containers.
