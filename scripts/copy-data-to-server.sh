#!/usr/bin/env bash
# SPA-78 — copy ALL local EvalHive data to the server (Postgres, MinIO, Qdrant,
# and ./data/{shared,workspaces}). This brings the tester@x.dev account and every
# experiment/result over so the committee can view real data.
#
# ================================ SAFETY ====================================
# This script is READ-ONLY against the LOCAL host:
#   * source volumes are mounted ':ro' when tarred — it CANNOT modify/delete them;
#   * it uses `docker compose stop` for a consistent snapshot, NEVER `down`, NEVER `-v`;
#   * it never deletes any local file.
# The only destructive steps (clearing + replacing volumes) run on the REMOTE
# server via ssh, inside the clearly-marked quoted heredoc at the end.
# It is a DRY-RUN by default; you must pass --go AND type COPY to transfer.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."   # repo root

# --- config (override via env) ---
LOCAL_PROJECT="${LOCAL_PROJECT:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')}"
REMOTE_PROJECT="${REMOTE_PROJECT:-evalhive}"
REMOTE_DIR="${REMOTE_DIR:-/opt/evalhive}"
DEPLOY_HOST="${DEPLOY_HOST:-}"        # e.g. root@203.0.113.10
VOLS=(pgdata qdrantdata miniodata)
GO=0; [ "${1:-}" = "--go" ] && GO=1

# Best-effort: pick the server IP out of .deploy_info if DEPLOY_HOST is unset.
if [ -z "$DEPLOY_HOST" ] && [ -f .deploy_info ]; then
  ip=$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' .deploy_info | head -1 || true)
  [ -n "$ip" ] && DEPLOY_HOST="root@$ip"
fi

echo "Local project : $LOCAL_PROJECT      (volumes ${LOCAL_PROJECT}_<name>)"
echo "Remote        : ${DEPLOY_HOST:-<unset>} -> $REMOTE_DIR (project $REMOTE_PROJECT)"
echo "Stores        : ${VOLS[*]} + ./data/{shared,workspaces}"
echo "Mode          : $([ $GO -eq 1 ] && echo 'GO (will transfer)' || echo 'DRY-RUN (no transfer)')"
echo

# Verify local volumes exist before touching anything (never guess).
missing=0
for v in "${VOLS[@]}"; do
  if ! docker volume inspect "${LOCAL_PROJECT}_${v}" >/dev/null 2>&1; then
    echo "  MISSING local volume: ${LOCAL_PROJECT}_${v}"; missing=1
  fi
done
if [ $missing -eq 1 ]; then
  echo; echo "Set LOCAL_PROJECT=<name> to match your local volumes. Candidates:"
  docker volume ls --format '  {{.Name}}' | grep -iE 'pgdata|minio|qdrant' || true
  exit 1
fi

if [ $GO -ne 1 ]; then
  echo "DRY-RUN: nothing read from or written to the host beyond volume inspection."
  echo "Re-run with --go to snapshot (read-only) and push to the server."
  exit 0
fi

[ -z "$DEPLOY_HOST" ] && { echo "ERROR: DEPLOY_HOST unset (e.g. DEPLOY_HOST=root@IP)." >&2; exit 1; }
read -r -p "Type COPY to snapshot local data and push to $DEPLOY_HOST: " ans
[ "$ans" = "COPY" ] || { echo "Aborted."; exit 1; }

STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGING_DIR"' EXIT

# 1) Quiesce the LOCAL stack for a consistent snapshot (stop only — never down/-v).
echo "==> Stopping local stack for a consistent snapshot (volumes untouched)"
docker compose stop

# 2) READ-ONLY tar of each volume + ./data (source mounted :ro → cannot be altered).
echo "==> Snapshotting to $STAGING_DIR (read-only mounts)"
for v in "${VOLS[@]}"; do
  echo "    - ${LOCAL_PROJECT}_${v}"
  docker run --rm -v "${LOCAL_PROJECT}_${v}:/v:ro" -v "$STAGING_DIR:/backup" alpine \
    tar czf "/backup/${v}.tgz" -C /v .
done
tar czf "$STAGING_DIR/data.tgz" -C ./data shared workspaces

# 3) Restart the local stack (leave the host as we found it).
echo "==> Restarting local stack"
docker compose start || true

# 4) Upload snapshots to the server.
echo "==> Uploading to $DEPLOY_HOST:$REMOTE_DIR/_import/"
ssh "$DEPLOY_HOST" "mkdir -p '$REMOTE_DIR/_import'"
rsync -avP "$STAGING_DIR"/ "$DEPLOY_HOST:$REMOTE_DIR/_import/"

# 5) Load on the SERVER. Destructive steps (clear+replace volumes) are REMOTE-only.
echo "==> Loading on the server (stops remote stack, replaces remote volumes)"
ssh "$DEPLOY_HOST" "REMOTE_DIR='$REMOTE_DIR' REMOTE_PROJECT='$REMOTE_PROJECT' VOLS='${VOLS[*]}' bash -s" <<'REMOTE'
set -euo pipefail
cd "$REMOTE_DIR"
export COMPOSE_PROJECT_NAME="$REMOTE_PROJECT"
C="docker compose -f docker-compose.yml -f docker-compose.prod.yml"
$C stop
for v in $VOLS; do
  docker volume create "${REMOTE_PROJECT}_${v}" >/dev/null
  docker run --rm -v "${REMOTE_PROJECT}_${v}:/v" -v "$REMOTE_DIR/_import:/backup" alpine \
    sh -ec 'find /v -mindepth 1 -delete; tar xzf "/backup/'"$v"'.tgz" -C /v'
done
mkdir -p data/shared data/workspaces
tar xzf _import/data.tgz -C data
$C up -d
rm -rf _import
echo "Remote load complete."
REMOTE

echo
echo "Done. Next on the server: scrub secrets ->"
echo "  $ docker compose -f docker-compose.yml -f docker-compose.prod.yml exec -T postgres \\"
echo "      psql -U evalhive -d evalhive < scripts/sanitize-secrets.sql"
