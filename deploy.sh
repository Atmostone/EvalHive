#!/usr/bin/env bash
# SPA-78 — bring up the EvalHive production/demo stack on the server.
# Builds the agent runtime image (compose never builds it, so a first spawn would
# otherwise fail with ImageNotFound), then starts base + prod overlay.
# Run from the repo root, with a filled-in .env (see .env.prod.example).
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.prod.example -> .env and fill it in first." >&2
  exit 1
fi

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

echo "==> Building agent runtime image (evalhive-agent:latest)"
docker build -t evalhive-agent:latest agent-image/

echo "==> Building + starting the production stack (migrations run automatically)"
$COMPOSE up -d --build

echo
echo "Stack is up. Check migration + api health:"
echo "  $COMPOSE logs migrate api"
echo
echo "First run only — obtain the TLS certificate next (needs DNS -> this host + 80/443 open):"
echo "  ./scripts/init-letsencrypt.sh          # real cert"
echo "  STAGING=1 ./scripts/init-letsencrypt.sh # staging cert, for testing"
