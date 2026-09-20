#!/usr/bin/env bash
#
# SPA-92. Restore a stand backup — by default into throwaway containers, so the
# rehearsal can be run at any time without touching the live stand.
#
#     scripts/restore.sh --backup DIR [--scratch] [--keep]
#     scripts/restore.sh --backup DIR --live
#
# A backup that has never been restored is a hypothesis. `--scratch` (the
# default) restores the dump into a temporary Postgres container and the tarball
# into a temporary volume, re-asks the pairing question of the RESTORED pair,
# compares the row counts against the manifest, and then removes both. Nothing
# it does can reach the real stand.
#
# `--live` is the disaster path: it restores over the project's own database and
# MinIO volume, and it is destructive. It refuses to run while the application
# containers are up, and it requires the phrase to be typed out.
#
# The pairing check is the same code the backup ran (scripts/_backup_lib.sh). A
# rehearsal with its own re-implementation would prove the rehearsal works.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/_backup_lib.sh
source "${REPO_ROOT}/scripts/_backup_lib.sh"

BACKUP=""
MODE="scratch"
KEEP=0
PROJECT="spawnhive"

usage() { sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --backup) BACKUP="$2"; shift 2 ;;
        --scratch) MODE="scratch"; shift ;;
        --live) MODE="live"; shift ;;
        --keep) KEEP=1; shift ;;
        --project) PROJECT="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

[[ -n "${BACKUP}" ]] || { echo "FATAL: --backup DIR is required" >&2; usage 1; }
[[ -f "${BACKUP}/manifest.json" ]] || { echo "FATAL: no manifest.json in ${BACKUP}" >&2; exit 2; }

command -v docker  >/dev/null || { echo "FATAL: docker not found" >&2; exit 2; }
command -v python3 >/dev/null || { echo "FATAL: python3 not found" >&2; exit 2; }

BUCKET="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source"]["bucket"])' "${BACKUP}/manifest.json")"

# ------------------------------------------------- verify the archive itself --

# Before trusting a backup enough to restore from it, check it is the bytes it
# claims to be. A corrupted tarball discovered halfway through a live restore is
# the worst possible moment to find out.
echo "==> verifying artifact checksums against the manifest"
BAD=0
while IFS=$'\t' read -r name want; do
    [[ -z "${name}" ]] && continue
    if [[ ! -f "${BACKUP}/${name}" ]]; then
        echo "    MISSING  ${name}"; BAD=1; continue
    fi
    got="$(sh_sha256 "${BACKUP}/${name}")"
    if [[ "${got}" == "${want}" ]]; then
        echo "    ok       ${name}"
    else
        echo "    MISMATCH ${name}"; echo "             expected ${want}"; echo "             actual   ${got}"; BAD=1
    fi
done < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
for name, meta in sorted(m["artifacts"].items()):
    print(name + "\t" + meta["sha256"])
' "${BACKUP}/manifest.json")

if [[ "${BAD}" -ne 0 ]]; then
    echo "FATAL: the backup does not match its manifest — refusing to restore from it." >&2
    exit 1
fi

# Which Postgres server to rehearse on: the same one the stand runs, so the
# rehearsal is not quietly a test of a different server version.
PG_IMAGE="$(docker inspect --format '{{.Config.Image}}' "${PROJECT}-postgres-1" 2>/dev/null || true)"
if [[ -z "${PG_IMAGE}" ]]; then
    PG_IMAGE="$(sed -n '/^  postgres:/,/^  [a-z]/p' "${REPO_ROOT}/docker-compose.yml" \
                | sed -n 's/^ *image: *//p' | head -1)"
fi
PG_IMAGE="${PG_IMAGE:-postgres:16}"

# ==============================================================  live restore ==

if [[ "${MODE}" == "live" ]]; then
    RUNNING="$(docker ps --format '{{.Names}}' | grep -E "^${PROJECT}-(api|scheduler|orchestrator|minio)-1$" || true)"
    if [[ -n "${RUNNING}" ]]; then
        echo "FATAL: these containers are up and would write while the restore runs:" >&2
        echo "${RUNNING}" | sed 's/^/       /' >&2
        echo "       Stop them first:  docker compose stop api scheduler orchestrator minio" >&2
        exit 2
    fi

    echo
    echo "    LIVE RESTORE — this REPLACES database '${PROJECT}' and volume '${PROJECT}_miniodata'."
    echo "    Current contents are destroyed and not recoverable from here."
    read -r -p "    Type 'restore live' to proceed: " CONFIRM
    [[ "${CONFIRM}" == "restore live" ]] || { echo "    aborted."; exit 1; }

    echo "==> restoring MinIO volume"
    docker run --rm -v "${PROJECT}_miniodata":/data -v "${BACKUP}":/backup:ro \
        alpine sh -c 'rm -rf /data/* /data/.minio.sys && tar xzf /backup/minio.tar.gz -C /data'

    echo "==> restoring database"
    docker compose -f "${REPO_ROOT}/docker-compose.yml" up -d postgres >/dev/null
    until docker exec "${PROJECT}-postgres-1" pg_isready -U spawnhive >/dev/null 2>&1; do sleep 1; done
    PGUSER="$(docker exec "${PROJECT}-postgres-1" printenv POSTGRES_USER)"
    PGDB="$(docker exec "${PROJECT}-postgres-1" printenv POSTGRES_DB)"
    docker exec -i "${PROJECT}-postgres-1" pg_restore -U "${PGUSER}" -d "${PGDB}" \
        --clean --if-exists --no-owner < "${BACKUP}/db.dump"

    echo "==> live restore complete. Bring the rest of the stack back up when ready."
    exit 0
fi

# ===========================================================  scratch restore ==

STAMP="$(date -u +%Y%m%d%H%M%S)"
SCRATCH_PG="spawnhive-restore-rehearsal-${STAMP}"
SCRATCH_VOL="spawnhive-restore-rehearsal-${STAMP}-minio"
SCRATCH_USER="spawnhive"
SCRATCH_DB="spawnhive"

cleanup() {
    if [[ "${KEEP}" -eq 1 ]]; then
        echo
        echo "    --keep: left in place for inspection —"
        echo "      container ${SCRATCH_PG}"
        echo "      volume    ${SCRATCH_VOL}"
        echo "    Remove with: docker rm -f ${SCRATCH_PG}; docker volume rm ${SCRATCH_VOL}"
        return
    fi
    docker rm -f "${SCRATCH_PG}" >/dev/null 2>&1 || true
    docker volume rm "${SCRATCH_VOL}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo
echo "==> rehearsing into throwaway container ${SCRATCH_PG} and volume ${SCRATCH_VOL}"

echo "==> unpacking MinIO tarball into the scratch volume"
docker volume create "${SCRATCH_VOL}" >/dev/null
docker run --rm -v "${SCRATCH_VOL}":/data -v "${BACKUP}":/backup:ro \
    alpine tar xzf /backup/minio.tar.gz -C /data

echo "==> starting ${PG_IMAGE}"
docker run -d --name "${SCRATCH_PG}" \
    -e POSTGRES_USER="${SCRATCH_USER}" \
    -e POSTGRES_PASSWORD=rehearsal \
    -e POSTGRES_DB="${SCRATCH_DB}" \
    "${PG_IMAGE}" >/dev/null

for _ in $(seq 60); do
    docker exec "${SCRATCH_PG}" pg_isready -U "${SCRATCH_USER}" >/dev/null 2>&1 && break
    sleep 1
done
docker exec "${SCRATCH_PG}" pg_isready -U "${SCRATCH_USER}" >/dev/null 2>&1 || {
    echo "FATAL: scratch postgres never became ready" >&2; exit 1; }

echo "==> pg_restore into the scratch database"
if ! docker exec -i "${SCRATCH_PG}" pg_restore -U "${SCRATCH_USER}" -d "${SCRATCH_DB}" \
        --no-owner --exit-on-error < "${BACKUP}/db.dump" 2> "${BACKUP}/.restore.log"; then
    echo "FATAL: pg_restore failed:" >&2
    sed 's/^/       /' "${BACKUP}/.restore.log" >&2
    rm -f "${BACKUP}/.restore.log"
    exit 1
fi
rm -f "${BACKUP}/.restore.log"

# No `-i`: `psql -c` does not read stdin, and attaching it makes this helper
# swallow whatever its caller is iterating over.
scratch_psql() { docker exec "${SCRATCH_PG}" psql -U "${SCRATCH_USER}" -d "${SCRATCH_DB}" -At -c "$1"; }

# ------------------------------------------- re-ask both questions of the copy --

echo "==> re-checking the pairing on the RESTORED pair"
# Kept inside the backup directory rather than in $TMPDIR: on macOS mktemp
# returns a path under /var/folders, which Docker Desktop does not necessarily
# share, and the bind mount below would then silently be an empty file.
KEYS_FILE="${BACKUP}/.rehearsal-keys.txt"; MISSING_FILE="${BACKUP}/.rehearsal-missing.txt"
COUNTS_FILE="${BACKUP}/.rehearsal-counts.tsv"
trap 'rm -f "${KEYS_FILE}" "${MISSING_FILE}" "${COUNTS_FILE}"; cleanup' EXIT

PATH_COLUMNS="$(sh_path_columns scratch_psql)"
sh_collect_s3_keys scratch_psql "${PATH_COLUMNS}" "${KEYS_FILE}" "${COUNTS_FILE}"
sh_missing_keys "${SCRATCH_VOL}" "${BUCKET}" "${KEYS_FILE}" > "${MISSING_FILE}"

R_KEYS="$(wc -l < "${KEYS_FILE}" | tr -d ' ')"
R_MISSING="$(wc -l < "${MISSING_FILE}" | tr -d ' ')"

echo "==> comparing row counts against the manifest"
RESTORED_COUNTS="$(scratch_psql "
    SELECT json_build_object(
        'experiments',         (SELECT count(*) FROM experiments),
        'experiment_runs',     (SELECT count(*) FROM experiment_runs),
        'experiment_attempts', (SELECT count(*) FROM experiment_attempts),
        'quality_records',     (SELECT count(*) FROM quality_records),
        'annotations',         (SELECT count(*) FROM annotations),
        'tasks',               (SELECT count(*) FROM tasks)
    )")"

BACKUP="${BACKUP}" RESTORED_COUNTS="${RESTORED_COUNTS}" COUNTS_FILE="${COUNTS_FILE}" \
R_KEYS="${R_KEYS}" R_MISSING="${R_MISSING}" \
python3 - <<'PY'
import json, os, pathlib, sys

backup = pathlib.Path(os.environ["BACKUP"])
manifest = json.loads((backup / "manifest.json").read_text())
restored = json.loads(os.environ["RESTORED_COUNTS"])
expected = manifest["counts"]

ok = True
print()
print("    table                 manifest   restored")
for key in sorted(expected):
    want, got = expected[key], restored.get(key)
    flag = "" if want == got else "   <-- MISMATCH"
    if want != got:
        ok = False
    print(f"    {key:<20} {want:>8}   {got:>8}{flag}")

r_keys, r_missing = int(os.environ["R_KEYS"]), int(os.environ["R_MISSING"])
print()
print(f"    S3 keys in restored database: {r_keys}   missing from restored volume: {r_missing}")

# The rehearsal audits its own collection the same way the backup did: a count
# that disagrees with the database means this check examined less than it looks
# like, and a vacuous pass is worse than a failure.
for line in pathlib.Path(os.environ["COUNTS_FILE"]).read_text().splitlines():
    if not line:
        continue
    col, collected, expected = line.split("\t")
    if collected != expected:
        print(f"    SUSPICIOUS: {col} collected {collected} keys but holds {expected}")
        ok = False

# The manifest said what it found at backup time; the rehearsal must reproduce
# it. A rehearsal that finds FEWER keys than the manifest has lost rows, and one
# that finds more has restored from something else.
m = manifest["blob_pairing"]
if r_keys != m["keys_in_database"]:
    print(f"    MISMATCH: manifest recorded {m['keys_in_database']} keys")
    ok = False
if r_missing != m["missing_from_volume"]:
    print(f"    MISMATCH: manifest recorded {m['missing_from_volume']} missing")
    ok = False

print()
if ok and r_missing == 0:
    print("    RESTORE REHEARSAL PASSED — the dump and the volume restore as a complete pair.")
elif ok:
    print(f"    RESTORE REHEARSAL REPRODUCED THE BACKUP, which was itself incomplete")
    print(f"    ({r_missing} key(s) with no object). The backup is faithful; the stand was not.")
    sys.exit(1)
else:
    print("    RESTORE REHEARSAL FAILED — the restored copy does not match the manifest.")
    sys.exit(1)
PY
