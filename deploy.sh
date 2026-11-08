#!/usr/bin/env bash
# Delegate VPS releases to the installed EvalHive deployment helper.
# The helper owns the stand's Compose files and private environment references.
# Prepare and verify a paired backup before switching an existing stand.
set -euo pipefail

cd "$(dirname "$0")"

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"
docker info --format '{{json .SecurityOptions}}' | grep -q name=rootless || {
  echo 'The EvalHive rootless Docker daemon is required.' >&2
  exit 1
}
helper="${EVALHIVE_DEPLOY_HELPER:-${HOME}/deployment/build-and-start}"
[[ -x "$helper" ]] || {
  echo 'Install the stand deployment helper or set EVALHIVE_DEPLOY_HELPER.' >&2
  exit 1
}
exec "$helper" "$@"
