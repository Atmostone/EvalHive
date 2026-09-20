#!/usr/bin/env bash
#
# SPA-92. One command that backs up a SpawnHive stand: the Postgres dump AND the
# MinIO volume, written together with a manifest that pairs them.
#
#     scripts/backup.sh [--out DIR] [--project NAME]
#
# Why both, and why together. `quality_records.record_s3_path`,
# `tasks.log_archive_s3_path` and `knowledge_documents.s3_path` hold keys into
# MinIO, not content: traces, execution snapshots and deliverables live in the
# volume. A Postgres dump on its own restores rows that point into nothing — the
# failure this project already lived through once. So the pairing is not a
# convention here, it is checked: every S3 key in the database is looked up in
# the volume being tarred, and the manifest records the result.
#
# Unlike the reproduction bundle (SPA-90), which refuses to write an archive
# that does not verify, this writes the backup no matter what it finds. A bundle
# is a claim about reproducibility and a bad one should not exist; a backup is
# insurance, and refusing to preserve an imperfect stand would destroy the only
# copy of it. The verdict is carried honestly instead: `complete: false` in the
# manifest and a non-zero exit, so a caller cannot read a half-dead pair as ok.
#
# Runs at the docker layer on purpose: it must work when the application is
# broken, which is when a backup matters. It needs docker and the postgres
# container, nothing from the api image.
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/_backup_lib.sh
source "${REPO_ROOT}/scripts/_backup_lib.sh"

OUT_DIR="${HOME}/spawnhive-backups"
PROJECT="spawnhive"

usage() { sed -n '3,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT_DIR="$2"; shift 2 ;;
        --project) PROJECT="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

PG_CONTAINER="${PROJECT}-postgres-1"
MINIO_VOLUME="${PROJECT}_miniodata"
BUCKET="$(sh_bucket_name "${REPO_ROOT}")"

# ---------------------------------------------------------------- preflight --

command -v docker  >/dev/null || { echo "FATAL: docker not found" >&2; exit 2; }
command -v python3 >/dev/null || { echo "FATAL: python3 not found" >&2; exit 2; }

if ! docker ps --format '{{.Names}}' | grep -qx "${PG_CONTAINER}"; then
    echo "FATAL: container ${PG_CONTAINER} is not running." >&2
    echo "       Start it first:  docker compose up -d postgres minio" >&2
    exit 2
fi

docker volume inspect "${MINIO_VOLUME}" >/dev/null 2>&1 || {
    echo "FATAL: volume ${MINIO_VOLUME} does not exist." >&2; exit 2; }

PGUSER="$(docker exec "${PG_CONTAINER}" printenv POSTGRES_USER)"
PGDB="$(docker exec "${PG_CONTAINER}" printenv POSTGRES_DB)"

# No `-i`: `psql -c` does not read stdin, and attaching it makes this helper
# swallow whatever its caller is iterating over.
psql_q() { docker exec "${PG_CONTAINER}" psql -U "${PGUSER}" -d "${PGDB}" -At -c "$1"; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${OUT_DIR}/spawnhive-backup-${STAMP}"
mkdir -p "${DEST}"

echo "==> backup destination: ${DEST}"

# ------------------------------------------------------------------- dump DB --

echo "==> pg_dump ${PGDB}"
docker exec "${PG_CONTAINER}" pg_dump -U "${PGUSER}" -d "${PGDB}" -Fc > "${DEST}/db.dump"

# Annotation rows separately, as plain JSON. Not a second copy of the data for
# its own sake: the append-only schema needs something to be migration
# smoke-tested against, and these all land as `legacy` (SPA-85).
echo "==> dumping annotation rows"
psql_q "SELECT coalesce(json_agg(row_to_json(a)), '[]'::json) FROM annotations a" > "${DEST}/annotations.json"

# ---------------------------------------------------------- tar MinIO volume --

# The whole volume, `.minio.sys` included, so a restore reconstitutes the object
# store rather than a directory that merely looks like one.
echo "==> tarring ${MINIO_VOLUME}"
docker run --rm \
    -v "${MINIO_VOLUME}":/data:ro \
    -v "${DEST}":/backup \
    alpine tar czf /backup/minio.tar.gz -C /data .

# ------------------------------------------------------- blob pairing check --

echo "==> checking that every S3 key in the database exists in the volume"

PATH_COLUMNS="$(sh_path_columns psql_q)"
sh_collect_s3_keys psql_q "${PATH_COLUMNS}" "${DEST}/.s3-keys.txt" "${DEST}/.s3-counts.tsv"
sh_missing_keys "${MINIO_VOLUME}" "${BUCKET}" "${DEST}/.s3-keys.txt" > "${DEST}/.s3-missing.txt"

N_KEYS="$(wc -l < "${DEST}/.s3-keys.txt" | tr -d ' ')"
N_MISSING="$(wc -l < "${DEST}/.s3-missing.txt" | tr -d ' ')"

# ------------------------------------------------------------------ manifest --

echo "==> writing manifest"

GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo '')"

COUNTS="$(psql_q "
    SELECT json_build_object(
        'experiments',         (SELECT count(*) FROM experiments),
        'experiment_runs',     (SELECT count(*) FROM experiment_runs),
        'experiment_attempts', (SELECT count(*) FROM experiment_attempts),
        'quality_records',     (SELECT count(*) FROM quality_records),
        'annotations',         (SELECT count(*) FROM annotations),
        'tasks',               (SELECT count(*) FROM tasks)
    )")"

DEST="${DEST}" BUCKET="${BUCKET}" MINIO_VOLUME="${MINIO_VOLUME}" PROJECT="${PROJECT}" \
PGDB="${PGDB}" STAMP="${STAMP}" GIT_SHA="${GIT_SHA}" COUNTS="${COUNTS}" \
PATH_COLUMNS="${PATH_COLUMNS}" N_KEYS="${N_KEYS}" N_MISSING="${N_MISSING}" \
python3 - <<'PY'
import hashlib, json, os, pathlib

dest = pathlib.Path(os.environ["DEST"])

def sha256(p):
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

artifacts = {
    name: {"bytes": (dest / name).stat().st_size, "sha256": sha256(dest / name)}
    for name in ("db.dump", "minio.tar.gz", "annotations.json")
}

missing = [ln for ln in (dest / ".s3-missing.txt").read_text().splitlines() if ln]
n_keys = int(os.environ["N_KEYS"])
n_missing = int(os.environ["N_MISSING"])

per_column = []
for line in (dest / ".s3-counts.tsv").read_text().splitlines():
    if not line:
        continue
    col, collected, expected = line.split("\t")
    per_column.append(
        {"column": col, "collected": int(collected), "expected": int(expected)}
    )

short = [c for c in per_column if c["collected"] != c["expected"]]

# "Nothing was checked" and "everything checked out" are different answers, and
# the first one was briefly reported as the second: a collector that lost its
# stdin gathered 0 keys, found 0 of them missing, and wrote a cheerful
# `complete: true`. A pass has to have examined something to mean anything, so
# the collection is now audited against its own source before its result is
# believed.
if short:
    status = "suspicious"
elif n_missing:
    status = "incomplete"
elif n_keys:
    status = "verified"
else:
    status = "nothing_to_check"

manifest = {
    "kind": "spawnhive-stand-backup",
    "version": 1,
    "created_at": os.environ["STAMP"],
    "source": {
        "compose_project": os.environ["PROJECT"],
        "database": os.environ["PGDB"],
        "minio_volume": os.environ["MINIO_VOLUME"],
        "bucket": os.environ["BUCKET"],
        "platform_git_sha": os.environ["GIT_SHA"] or None,
    },
    "artifacts": artifacts,
    "counts": json.loads(os.environ["COUNTS"]),
    "blob_pairing": {
        "status": status,
        "columns_checked": per_column,
        "keys_in_database": n_keys,
        "present_in_volume": n_keys - n_missing,
        "missing_from_volume": n_missing,
        # Capped: a wholesale mismatch should not produce a megabyte of manifest.
        "missing_sample": missing[:50],
        "missing_sample_truncated": len(missing) > 50,
    },
    # `complete` answers a different question from "did the files get written":
    # it is whether the dump and the volume are a usable pair.
    "complete": status in ("verified", "nothing_to_check"),
}

(dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
PY

STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["blob_pairing"]["status"])' "${DEST}/manifest.json")"
rm -f "${DEST}/.s3-keys.txt" "${DEST}/.s3-missing.txt" "${DEST}/.s3-counts.tsv"

# -------------------------------------------------------------------- report --

echo
echo "==> backup written to ${DEST}"
du -h "${DEST}"/* | sed 's/^/    /'
echo
echo "    S3 keys in database: ${N_KEYS}   missing from volume: ${N_MISSING}   status: ${STATUS}"

case "${STATUS}" in
    incomplete)
        echo
        echo "    WARNING: the backup was written, but the dump and the volume are NOT a"
        echo "    complete pair — ${N_MISSING} key(s) point at objects the volume does not hold."
        echo "    manifest.complete = false. See manifest.json -> blob_pairing.missing_sample."
        exit 1 ;;
    suspicious)
        echo
        echo "    WARNING: key collection disagrees with the database — a column yielded a"
        echo "    different number of keys than it holds non-empty values. The pairing check"
        echo "    did not examine everything, so it proved less than it appears to."
        echo "    Treat this backup as UNVERIFIED and fix the collection before relying on it."
        echo "    See manifest.json -> blob_pairing.columns_checked (collected vs expected)."
        exit 1 ;;
    nothing_to_check)
        echo "    manifest.complete = true, but there was nothing to pair: the stand holds no"
        echo "    S3 keys at all. The dump is a faithful backup of an empty object store." ;;
    verified)
        echo "    manifest.complete = true — every S3 key in the dump resolves in the volume." ;;
esac

echo
echo "    A backup that has never been restored is a hypothesis. Rehearse it:"
echo "      scripts/restore.sh --backup ${DEST} --scratch"
