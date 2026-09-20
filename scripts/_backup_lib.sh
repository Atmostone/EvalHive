#!/usr/bin/env bash
#
# SPA-92. Shared helpers for scripts/backup.sh and scripts/restore.sh.
#
# The pairing check lives here rather than in each script because a rehearsal
# that re-implemented it would prove the rehearsal works, not the backup — the
# same reason the reproduction bundle recomputes through the production code
# instead of a copy of it (SPA-90). Backup and restore ask the identical
# question of different volumes, so they run identical code.
#
# Sourced, not executed.

# The bucket name is a constant in the application; read it from there rather
# than keeping a second copy that can drift out of step with the code.
sh_bucket_name() {
    local repo_root="$1" bucket
    bucket="$(sed -n 's/^BUCKET = "\(.*\)"$/\1/p' "${repo_root}/backend/app/storage/minio_client.py")"
    if [[ -z "${bucket}" ]]; then
        echo "FATAL: could not read BUCKET from backend/app/storage/minio_client.py" >&2
        echo "       (the constant moved or changed shape — fix the script, do not guess)" >&2
        return 2
    fi
    printf '%s' "${bucket}"
}

# Columns holding S3 keys are derived from the live schema, not from a list kept
# by hand: a migration adding a fourth one is then covered by construction
# instead of silently escaping the check. Callers record the returned list in the
# manifest, so a column that turns out not to be an S3 key stays visible rather
# than quietly inflating the missing count.
sh_path_columns() {
    local psql_fn="$1"
    "${psql_fn}" "
        SELECT table_name || '.' || column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND column_name LIKE '%s3_path'
        ORDER BY 1"
}

# Every non-null S3 key in the database, de-duplicated, one per line in $out.
#
# Per-column bookkeeping goes to $counts_out as `column<TAB>collected<TAB>expected`,
# where `expected` is an independent SQL count of the non-empty values in that
# column. The two must agree; a column where they do not means the collection
# itself failed, which is a different thing from a stand that legitimately holds
# no keys, and only the first is a defect. Comparing against the row count of
# the table would have confused the two — a stand whose paths are all NULL is
# perfectly valid.
#
# The column list is read into an array BEFORE any query runs. It used to be a
# `while read` loop fed by a here-string, which broke the moment the caller's
# psql helper attached stdin (`docker exec -i`): the first query drained the
# here-string, the loop ended after one column, and the check reported zero keys
# and zero missing — a pass that had examined nothing.
sh_collect_s3_keys() {
    local psql_fn="$1" columns="$2" out="$3" counts_out="$4"
    local -a cols=()
    local tc tbl col before after

    while IFS= read -r tc; do
        [[ -n "${tc}" ]] && cols+=("${tc}")
    done <<< "${columns}"

    : > "${out}"; : > "${counts_out}"
    for tc in "${cols[@]}"; do
        tbl="${tc%%.*}"; col="${tc#*.}"
        before="$(wc -l < "${out}" | tr -d ' ')"
        "${psql_fn}" "SELECT ${col} FROM ${tbl} WHERE ${col} IS NOT NULL AND ${col} <> ''" >> "${out}"
        after="$(wc -l < "${out}" | tr -d ' ')"
        printf '%s\t%s\t%s\n' "${tc}" "$((after - before))" \
            "$("${psql_fn}" "SELECT count(*) FROM ${tbl} WHERE ${col} IS NOT NULL AND ${col} <> ''")" \
            >> "${counts_out}"
    done

    sort -u "${out}" -o "${out}"
}

# Keys with no object behind them in the given volume, one per line on stdout.
#
# MinIO stores each object as a directory holding `xl.meta`, so presence is a
# filesystem question needing no running MinIO — which is the point: this has to
# work when the stack is down, because that is when it is asked.
sh_missing_keys() {
    local volume="$1" bucket="$2" keys_file="$3"
    docker run --rm \
        -v "${volume}":/data:ro \
        -v "${keys_file}":/keys.txt:ro \
        alpine sh -c '
            while IFS= read -r key; do
                [ -z "$key" ] && continue
                [ -e "/data/'"${bucket}"'/$key/xl.meta" ] || echo "$key"
            done < /keys.txt
        '
}

# sha256 of a file, portable between macOS and Linux.
sh_sha256() {
    if command -v shasum >/dev/null; then shasum -a 256 "$1" | cut -d' ' -f1
    else sha256sum "$1" | cut -d' ' -f1; fi
}
