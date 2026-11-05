#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DOCKER_HOST:?Select the EvalHive rootless Docker socket explicitly}"
docker info --format '{{json .SecurityOptions}}' | grep -q 'name=rootless' || {
  echo 'This engineering suite requires rootless Docker.' >&2; exit 1;
}
: "${E2E_DATA_DIR:?Set an absolute disposable workspace directory}"
: "${E2E_RESULTS_DIR:?Set an absolute engineering results directory}"
export E2E_DOCKER_SOCKET="${E2E_DOCKER_SOCKET:-${DOCKER_HOST#unix://}}"
export E2E_BACKEND_IMAGE="${E2E_BACKEND_IMAGE:-evalhive-backend:harness-e2e}"
mkdir -p "$E2E_DATA_DIR" "$E2E_RESULTS_DIR"
compose=(docker compose -f tests/e2e/compose.yml)
cleanup() {
  "${compose[@]}" logs --no-color > "$E2E_RESULTS_DIR/services.log" 2>&1 || true
  # Only harness containers on this suite's private network belong to this cleanup.
  mapfile -t runners < <(docker ps -aq --filter network=evalhive-harness-e2e --filter label=evalhive.role=runner)
  if ((${#runners[@]})); then docker rm -f "${runners[@]}" >/dev/null; fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null
}
trap cleanup EXIT
if [[ "${E2E_SKIP_BUILD:-0}" != 1 ]]; then
  docker build -f backend/Dockerfile --build-arg GIT_SHA="$(git rev-parse HEAD)" -t "$E2E_BACKEND_IMAGE" .
  if ! docker image inspect evalhive-minio:RELEASE.2025-10-15T17-29-55Z >/dev/null 2>&1; then
    docker build -f tests/e2e/Minio.Dockerfile -t evalhive-minio:RELEASE.2025-10-15T17-29-55Z tests/e2e
  fi
  docker build --target test -f environments/workspace/Dockerfile -t evalhive-environment-tests:e2e .
  docker run --rm --network none --memory 512m --cpus 1 --pids-limit 128 --cap-drop ALL \
    --security-opt no-new-privileges --init evalhive-environment-tests:e2e
  docker build --target runtime -f environments/workspace/Dockerfile -t evalhive-environment-workspace:0.1.0 .
  # Native tests and execution are deliberately serial.
  for harness in pi opencode; do
    docker build --target test -f "harnesses/$harness/Dockerfile" -t "evalhive-$harness-tests:e2e" .
    docker run --rm --network none --memory 2g --cpus 1 --pids-limit 256 --cap-drop ALL \
      --security-opt no-new-privileges --init "evalhive-$harness-tests:e2e"
    version=1.5.0
    if [[ "$harness" == opencode ]]; then version=1.19.5; fi
    docker build -f "harnesses/$harness/Dockerfile" -t "evalhive-$harness:$version" .
  done
fi
"${compose[@]}" up -d api
# API migrations must finish before the worker reads the new schema.
"${compose[@]}" exec -T api python -c 'import time,urllib.request
for attempt in range(90):
 try:
  if urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3).status == 200: break
 except Exception: time.sleep(1)
else: raise SystemExit("Engineering API did not become healthy")'
"${compose[@]}" up -d worker
"${compose[@]}" run --rm --no-deps --use-aliases -T browser
for attempt in {1..20}; do
  if [[ -z "$(docker ps -q --filter network=evalhive-harness-e2e --filter label=evalhive.role=runner)" ]]; then
    exit 0
  fi
  sleep 1
done
echo 'A native harness container remained active after acceptance completed.' >&2
exit 1
