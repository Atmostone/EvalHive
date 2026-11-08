#!/usr/bin/env bash
# Prepare or activate a release using the stand's existing deployment files.
set -euo pipefail
cd "$(dirname "$0")/.."

mode=prepare
backup=''
restore_proof=''
while (($#)); do
  case "$1" in
    --prepare) mode=prepare; shift ;;
    --activate) mode=activate; shift ;;
    --backup) backup="$2"; shift 2 ;;
    --restore-proof) restore_proof="$2"; shift 2 ;;
    --help|-h) echo 'Use --prepare or --activate --backup DIR --restore-proof FILE.'; exit 0 ;;
    *) echo 'Use --prepare or --activate --backup DIR --restore-proof FILE.' >&2; exit 2 ;;
  esac
done

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/docker.sock"
run="${EVALHIVE_DEPLOY_DIR:-${HOME}/deployment}"
candidate="${EVALHIVE_COMPOSE_CANDIDATE:-${run}/compose.yml}"
docker info --format '{{json .SecurityOptions}}' | grep -q name=rootless
[[ -z "$(git status --porcelain)" ]] || { echo 'Commit release sources first.' >&2; exit 1; }
release_sha="$(git rev-parse HEAD)"
release_dir="$run/releases/$release_sha"
deployed_sha="$(python3 - "$run/release.env" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
values = dict(line.partition('=')[::2] for line in path.read_text().splitlines() if '=' in line)
value = values.get('EVALHIVE_RELEASE_SHA', '')
assert re.fullmatch('[a-f0-9]{40}', value)
print(value)
PY
)"

if [[ "$mode" == prepare ]]; then
  : "${EVALHIVE_BENCHMARK_DIR:?Select the newly published benchmark catalog}"
  [[ "$EVALHIVE_BENCHMARK_DIR" == /* && -f "$EVALHIVE_BENCHMARK_DIR/images.json" ]]
  mkdir -p "$release_dir"
  # Retain installed project images before reusing a source tag.
  for reference in "evalhive-backend:$deployed_sha" "evalhive-frontend:$deployed_sha" \
      evalhive-environment-workspace:0.1.0 evalhive-pi:1.5.0 evalhive-opencode:1.19.5 \
      evalhive-pi-bundle:1.5.0 evalhive-opencode-bundle:1.19.5; do
    image_id="$(docker image inspect "$reference" --format '{{.Id}}' 2>/dev/null || true)"
    if [[ -n "$image_id" ]]; then docker image tag "$image_id" "evalhive-release-retained:${image_id#sha256:}"; fi
  done
  build() {
    python3 scripts/engineering-disk-guard.py --min-free-gib "${EVALHIVE_RELEASE_MIN_FREE_GIB:-8}"
    docker build "$@"
    python3 scripts/engineering-disk-guard.py --min-free-gib "${EVALHIVE_RELEASE_MIN_FREE_GIB:-8}"
  }
  build -f backend/Dockerfile --build-arg "GIT_SHA=$release_sha" -t "evalhive-backend:$release_sha" .
  build --target prod -t "evalhive-frontend:$release_sha" frontend
  build --target runtime -f environments/workspace/Dockerfile -t evalhive-environment-workspace:0.1.0 .
  build --target artifact -f environments/runtime/Dockerfile -t evalhive-supervisor:development .
  build --target bundle-tools -f environments/runtime/Dockerfile -t evalhive-runtime-tools:development .
  for connector in pi opencode; do
    version=1.5.0
    [[ "$connector" != opencode ]] || version=1.19.5
    build --target runtime -f "harnesses/$connector/Dockerfile" -t "evalhive-$connector:$version" .
    build --target bundle -f "harnesses/$connector/Dockerfile" -t "evalhive-$connector-bundle:$version" .
  done
  if [[ ! -d "$release_dir/harnesses" ]]; then
    python3 scripts/install-harness-manifests.py --destination "$release_dir/harnesses"
  fi
  backend_id="$(docker image inspect "evalhive-backend:$release_sha" --format '{{.Id}}')"
  frontend_id="$(docker image inspect "evalhive-frontend:$release_sha" --format '{{.Id}}')"
  printf '%s\n' "EVALHIVE_RELEASE_SHA=$release_sha" "EVALHIVE_BACKEND_IMAGE=$backend_id" \
    "EVALHIVE_FRONTEND_IMAGE=$frontend_id" "EVALHIVE_HARNESS_DIR=$release_dir/harnesses" \
    "EVALHIVE_BENCHMARK_DIR=$EVALHIVE_BENCHMARK_DIR" > "$release_dir/release.env"
  cp "$candidate" "$release_dir/compose.yml"
  docker compose --project-directory "$PWD" --env-file "$run/runtime.env" --env-file "$release_dir/release.env" -f "$release_dir/compose.yml" config --quiet
  echo "Prepared $release_dir; production services were not switched."
  exit 0
fi

[[ -f "$release_dir/release.env" && -f "$release_dir/compose.yml" && -n "$backup" && -n "$restore_proof" ]] || {
  echo 'Prepare this exact source revision and provide its paired backup/restore proof.' >&2; exit 1;
}
python3 - "$backup" "$restore_proof" "$deployed_sha" <<'PY'
import hashlib, json, pathlib, sys
backup, proof = pathlib.Path(sys.argv[1]), json.loads(pathlib.Path(sys.argv[2]).read_text())
manifest = backup / 'manifest.json'
data = json.loads(manifest.read_text())
assert data['complete'] is True and data['source']['compose_project'] == 'evalhive'
assert data['source']['platform_git_sha'] == sys.argv[3]
assert proof['status'] == 'passed' and proof['backup_manifest_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
for name, expected in data['artifacts'].items():
    with (backup / name).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert digest == expected['sha256']
PY
for service in api orchestrator scheduler; do
  [[ "$(docker inspect "evalhive-$service-1" --format '{{.State.Running}}')" == false ]] || {
    echo 'Pause application writes before activation and the paired backup.' >&2; exit 1;
  }
done
mkdir -p "$release_dir/previous-config"
for name in compose.yml release.env compose build-and-start; do
  if [[ -f "$run/$name" && ! -e "$release_dir/previous-config/$name" ]]; then cp "$run/$name" "$release_dir/previous-config/$name"; fi
done
cp "$release_dir/compose.yml" "$run/compose.yml"
cp "$release_dir/release.env" "$run/release.env"
"$run/compose" up -d --no-build api orchestrator frontend
echo 'Run release smoke and verify cleanup before restoring scheduler state.'
