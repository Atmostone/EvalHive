"""MinIO storage utilities for task result files."""

import logging
import os

from minio import Minio

from app.config import get_settings

logger = logging.getLogger(__name__)

BUCKET = "spawnhive"


def get_minio_client() -> Minio:
    settings = get_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=False,
    )


def ensure_bucket():
    client = get_minio_client()
    if not client.bucket_exists(BUCKET):
        client.make_bucket(BUCKET)


def upload_task_results(task_id: str, workspace_dir: str) -> list[str]:
    """Upload files from workspace/output/ to MinIO. Returns list of S3 paths."""
    output_dir = os.path.join(workspace_dir, "output")
    if not os.path.exists(output_dir):
        return []

    client = get_minio_client()
    ensure_bucket()

    s3_paths = []
    for root, _dirs, files in os.walk(output_dir):
        for f in files:
            local_path = os.path.join(root, f)
            rel_path = os.path.relpath(local_path, output_dir)
            s3_path = f"results/{task_id}/{rel_path}"

            client.fput_object(BUCKET, s3_path, local_path)
            s3_paths.append(s3_path)
            logger.info(f"Uploaded {rel_path} -> s3://{BUCKET}/{s3_path}")

    return s3_paths


def upload_task_results_root(task_id: str, workspace_dir: str) -> list[str]:
    """Upload deliverables from the workspace ROOT to MinIO (benchmark mode).

    Benchmark/Toolathlon agents write their deliverables to the ``/workspace``
    root with exact filenames — not the ``output/`` subdir that product mode (and
    :func:`upload_task_results`) harvests — so without this the judge (E-02) gets
    zero files and grades only the agent's self-reported summary (SPA-70). This
    harvests every regular file at the root, skipping dotfiles and system logs
    (``.pre_log.json`` / ``.eval_log.json``) and any subdirectory (the unused
    ``output/``, seed dirs). Newest-first, so when the judge's per-eval file cap
    truncates, the agent's just-written deliverables win over older seeded stub
    files (README/config/data copied in by preprocess).
    """
    if not os.path.isdir(workspace_dir):
        return []

    files = [
        os.path.join(workspace_dir, name)
        for name in os.listdir(workspace_dir)
        if not name.startswith(".")
        and os.path.isfile(os.path.join(workspace_dir, name))
    ]
    files.sort(key=os.path.getmtime, reverse=True)

    client = get_minio_client()
    ensure_bucket()

    s3_paths = []
    for local_path in files:
        rel_path = os.path.basename(local_path)
        s3_path = f"results/{task_id}/{rel_path}"
        client.fput_object(BUCKET, s3_path, local_path)
        s3_paths.append(s3_path)
        logger.info(f"Uploaded {rel_path} -> s3://{BUCKET}/{s3_path}")

    return s3_paths


def get_file_stream(s3_path: str):
    """Get a file stream from MinIO."""
    client = get_minio_client()
    return client.get_object(BUCKET, s3_path)


def read_result_file_text(s3_path: str, max_bytes: int = 16_384) -> str | None:
    """Read up to ``max_bytes`` of a result file as text.

    Returns ``None`` for binary content (NUL byte in the sample) — used by the
    quality judge to put deliverable excerpts into the evaluation context.
    """
    client = get_minio_client()
    obj = client.get_object(BUCKET, s3_path, offset=0, length=max_bytes)
    try:
        data = obj.read()
    finally:
        obj.close()
        obj.release_conn()
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


# Hard ceiling on full-file reads handed to the artifact→Markdown converter
# (SPA-71): guards against OOM / zip-bomb artifacts (docx/pptx/xlsx are zip
# containers that can decompress huge). 25 MiB.
_MAX_CONVERT_BYTES = 25 * 1024 * 1024


def read_result_file_bytes(s3_path: str, max_bytes: int = _MAX_CONVERT_BYTES) -> bytes | None:
    """Read a result file as raw bytes for conversion (SPA-71).

    Returns ``None`` when the object is larger than ``max_bytes`` (refuse to
    buffer huge/zip-bomb artifacts) — the caller degrades to a note. Unlike
    ``read_result_file_text`` this is a full read with **no** NUL-byte check:
    binary container formats (docx/pptx/xlsx) must arrive intact.
    """
    client = get_minio_client()
    try:
        stat = client.stat_object(BUCKET, s3_path)
        if stat.size is not None and stat.size > max_bytes:
            return None
    except Exception:
        # stat failed — fall through and let get_object surface the real error,
        # but bound the read with ``length`` so a missing stat can't OOM us.
        pass
    obj = client.get_object(BUCKET, s3_path, offset=0, length=max_bytes)
    try:
        return obj.read()
    finally:
        obj.close()
        obj.release_conn()


def upload_log_archive(task_id: str, content: bytes) -> str:
    """Upload compacted agent log to MinIO. Returns the s3 path."""
    client = get_minio_client()
    ensure_bucket()
    import io

    s3_path = f"logs/{task_id}.log"
    client.put_object(
        BUCKET,
        s3_path,
        io.BytesIO(content),
        length=len(content),
        content_type="text/plain",
    )
    logger.info(f"Uploaded agent log -> s3://{BUCKET}/{s3_path} ({len(content)} bytes)")
    return s3_path


def read_log_archive(s3_path: str) -> bytes:
    """Read a compacted agent log blob. Returns raw bytes."""
    client = get_minio_client()
    obj = client.get_object(BUCKET, s3_path)
    try:
        return obj.read()
    finally:
        obj.close()
        obj.release_conn()


# Legacy delimiter (pre tool_name-preserving format): chunks joined by this, the
# tool_name was dropped. Kept so old archives still decode (without tool_name).
_LEGACY_LOG_SEP = "\n␞\n"


def encode_log_archive(chunks) -> bytes:
    """Serialize agent log chunks to the archive blob, **preserving the tool call**.

    JSON-lines, one object per chunk — so the cleaned trace (E-06) and trajectory
    matcher (E-09) keep the tool name, and now its arguments and call identity
    (SPA-86), its timestamp (SPA-113) and the model's reasoning (SPA-114),
    after compaction instead of going blind.
    `json.dumps` escapes newlines, so each chunk is one line."""
    import json

    lines = [
        json.dumps(
            {
                "tool_name": getattr(c, "tool_name", None),
                "content": getattr(c, "content", "") or "",
                "arguments": getattr(c, "arguments", None),
                "arguments_truncated": bool(getattr(c, "arguments_truncated", False)),
                "tool_call_id": getattr(c, "tool_call_id", None),
                "part_index": getattr(c, "part_index", 0) or 0,
                "part_total": getattr(c, "part_total", 1) or 1,
                # The model's own deliberation (SPA-114). Omitting it here would
                # repeat SPA-113's mistake exactly: a field the live trace has and
                # the archived one silently does not.
                "reasoning": getattr(c, "reasoning", None),
                # The chunk's own clock. Without it the archived trace cannot be
                # put back in the order it happened (SPA-113).
                "created_at": (
                    ts.isoformat() if (ts := getattr(c, "created_at", None)) is not None else None
                ),
            },
            ensure_ascii=False,
            default=str,
        )
        for c in chunks
    ]
    return "\n".join(lines).encode("utf-8")


def _decoded_chunk(o: dict) -> dict:
    """One archived chunk in the shape the cleaner expects. Archives written before
    SPA-86 carry no call fields; the defaults describe exactly what those rows are —
    a single-part output with no recorded arguments."""
    return {
        "content": o.get("content", ""),
        "tool_name": o.get("tool_name"),
        "arguments": o.get("arguments"),
        "arguments_truncated": bool(o.get("arguments_truncated", False)),
        "tool_call_id": o.get("tool_call_id"),
        "part_index": o.get("part_index", 0) or 0,
        "part_total": o.get("part_total", 1) or 1,
        # SPA-113: when this was absent, every tool step in an archived task's
        # trace sorted after every event — the cleaner puts undated steps last —
        # so the judge read a run whose tool calls all happened after it finished.
        # Archives written before this carry None and degrade to that old order.
        "created_at": o.get("created_at"),
        # SPA-114: absent in every archive written before the model's deliberation
        # was captured at all — those decode to None, which is what is true of
        # them, rather than to an empty string that would read as "it thought
        # nothing".
        "reasoning": o.get("reasoning"),
    }


def decode_log_archive(blob: str) -> list[dict]:
    """Decode an archive blob into ``[{content, tool_name, arguments, …}]``.

    Handles the JSON-lines format and the legacy `\\n␞\\n`-joined plain-text format
    (tool_name lost → ``None``), detected from the first line."""
    import json

    if not blob:
        return []
    first = blob.split("\n", 1)[0].strip()
    is_jsonl = False
    if first.startswith("{"):
        try:
            obj = json.loads(first)
            is_jsonl = isinstance(obj, dict) and "content" in obj
        except Exception:
            is_jsonl = False
    if is_jsonl:
        out: list[dict] = []
        for line in blob.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_decoded_chunk(json.loads(line)))
            except Exception:
                out.append(_decoded_chunk({"content": line}))
        return out
    # legacy plain-text format
    return [_decoded_chunk({"content": c}) for c in blob.split(_LEGACY_LOG_SEP)]


def upload_quality_record(workspace_id: str, task_id: str, content: bytes) -> str:
    """Upload a Quality Data Lake record blob (JSON). Returns the s3 path."""
    client = get_minio_client()
    ensure_bucket()
    import io

    s3_path = f"data-lake/{workspace_id}/{task_id}.json"
    client.put_object(
        BUCKET,
        s3_path,
        io.BytesIO(content),
        length=len(content),
        content_type="application/json",
    )
    logger.info(f"Uploaded quality record -> s3://{BUCKET}/{s3_path} ({len(content)} bytes)")
    return s3_path


def read_quality_record(s3_path: str) -> bytes:
    """Read a Quality Data Lake record blob. Returns raw bytes."""
    client = get_minio_client()
    obj = client.get_object(BUCKET, s3_path)
    try:
        return obj.read()
    finally:
        obj.close()
        obj.release_conn()


def delete_object(s3_path: str) -> None:
    """Delete a single object from the bucket (best-effort; used by retention)."""
    client = get_minio_client()
    client.remove_object(BUCKET, s3_path)
    logger.info(f"Deleted s3://{BUCKET}/{s3_path}")
