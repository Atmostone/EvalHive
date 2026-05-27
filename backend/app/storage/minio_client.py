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


def get_file_stream(s3_path: str):
    """Get a file stream from MinIO."""
    client = get_minio_client()
    return client.get_object(BUCKET, s3_path)


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
    """Serialize agent log chunks to the archive blob, **preserving `tool_name`**.

    JSON-lines, one object per chunk (`{"tool_name", "content"}`) — so the cleaned
    trace (E-06) and trajectory matcher (E-09) keep the tool name after compaction,
    instead of going blind. `json.dumps` escapes newlines, so each chunk is one line."""
    import json

    lines = [
        json.dumps(
            {"tool_name": getattr(c, "tool_name", None), "content": getattr(c, "content", "") or ""},
            ensure_ascii=False,
        )
        for c in chunks
    ]
    return "\n".join(lines).encode("utf-8")


def decode_log_archive(blob: str) -> list[dict]:
    """Decode an archive blob into ``[{content, tool_name}]``.

    Handles the JSON-lines format (with `tool_name`) and the legacy `\\n␞\\n`-joined
    plain-text format (tool_name lost → ``None``), detected from the first line."""
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
                o = json.loads(line)
                out.append({"content": o.get("content", ""), "tool_name": o.get("tool_name")})
            except Exception:
                out.append({"content": line, "tool_name": None})
        return out
    # legacy plain-text format
    return [{"content": c, "tool_name": None} for c in blob.split(_LEGACY_LOG_SEP)]


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
