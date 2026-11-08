#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DOCKER_HOST:?Select the EvalHive rootless daemon}"
docker info --format '{{json .SecurityOptions}}' | grep -q name=rootless
api_container="${EVALHIVE_SMOKE_API:-evalhive-api-1}"
smoke_network="${EVALHIVE_SMOKE_NETWORK:-evalhive_evalhive-net}"
scheduler="${EVALHIVE_SMOKE_SCHEDULER-evalhive-scheduler-1}"
if [[ -n "$scheduler" ]]; then
  scheduler_state="$(docker inspect "$scheduler" --format '{{.State.Running}}')"
  if [[ "$scheduler_state" == true ]]; then
    echo 'Pause the EvalHive scheduler before running release fixtures.' >&2; exit 1
  fi
fi
fixture_name="evalhive-release-model-$(date -u +%Y%m%d%H%M%S)-$$"
fixture_id=''
cleanup_fixture() { if [[ -n "$fixture_id" ]]; then docker rm -f "$fixture_id" >/dev/null 2>&1 || true; fi; }
trap cleanup_fixture EXIT
fixture_id="$(docker run -d --name "$fixture_name" --network "$smoke_network" \
  --memory 256m --cpus 1 --pids-limit 64 --cap-drop ALL --security-opt no-new-privileges --init \
  --mount "type=bind,src=$PWD/tests/e2e/release-model.mjs,dst=/fixture/release-model.mjs,readonly" \
  --mount "type=bind,src=$PWD/harnesses/shared/fake-openai.mjs,dst=/fixture/fake-openai.mjs,readonly" \
  node:22.23.1-bookworm-slim@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 \
  node /fixture/release-model.mjs)"
native_args=()
if [[ -n "${EVALHIVE_SMOKE_NATIVE_DATASET_ID:-}" ]]; then
  native_args+=(--native-dataset-id "$EVALHIVE_SMOKE_NATIVE_DATASET_ID")
fi
docker exec -i "$api_container" python - --model-url "http://$fixture_name:4100/v1" "${native_args[@]}" < tests/e2e/release-smoke.py
