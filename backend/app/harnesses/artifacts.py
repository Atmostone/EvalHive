"""Collect environment-owned files after connector shutdown, for every outcome."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
from pathlib import Path, PurePosixPath


def artifact_policy(spec):
    supplied = spec.get("environment", {}).get("artifact_policy", {})
    if not isinstance(supplied, dict) or set(supplied) - {
        "root",
        "recursive",
        "include_hidden",
        "max_files",
        "max_bytes",
        "preserve_on",
    }:
        raise ValueError("Unsupported artifact policy")
    policy = {
        "root": ".",
        "recursive": True,
        "include_hidden": False,
        "max_files": 1024,
        "max_bytes": 256 * 1024 * 1024,
        "preserve_on": ["success", "failed", "cancelled"],
        **supplied,
    }
    if not isinstance(policy["root"], str) or not policy["root"]:
        raise ValueError("Artifact root must be a nonempty relative path")
    root = PurePosixPath(policy["root"])
    if root.is_absolute() or ".." in root.parts:
        raise ValueError("Artifact root must stay within the task workspace")
    if any(type(policy[key]) is not bool for key in ("recursive", "include_hidden")):
        raise ValueError("Invalid artifact traversal policy")
    for key, maximum in (("max_files", 10000), ("max_bytes", 1024 * 1024 * 1024)):
        if type(policy[key]) is not int or not 1 <= policy[key] <= maximum:
            raise ValueError("Artifact limit is outside server policy")
    if not isinstance(policy["preserve_on"], list) or any(
        value not in ("success", "failed", "cancelled") for value in policy["preserve_on"]
    ):
        raise ValueError("Invalid artifact retention outcomes")
    return policy


def collect_artifacts(layout, spec, result, *, client=None):
    from app.storage.minio_client import BUCKET, get_minio_client

    policy = artifact_policy(spec)
    files, errors, announcements = [], [], []
    root = layout.workspace / policy["root"]
    if result["status"] not in policy["preserve_on"]:
        return {
            **result,
            "metadata": {
                **result.get("metadata", {}),
                "artifacts": [],
                "artifact_delivery": "excluded_by_policy",
            },
        }, []

    def failure(path, code):
        errors.append({"path": path, "code": code})
        announcements.append({"path": path, "metadata": {"delivery": "failed", "error_code": code}})

    # A policy root may itself contain several path components. None can be a
    # symlink, including a dangling link which Path.exists would overlook.
    current = layout.workspace
    for part in Path(policy["root"]).parts:
        current = current / part
        if current.is_symlink():
            failure(policy["root"], "SYMLINK")
            break
    if not errors and root.exists() and not root.is_dir():
        failure(policy["root"], "NOT_DIRECTORY")
    candidates = []
    if not errors and root.is_dir():

        def unreadable(error):
            failure(policy["root"], "DIRECTORY_UNREADABLE")

        for directory, directories, names in os.walk(root, followlinks=False, onerror=unreadable):
            for name in list(directories):
                path = Path(directory) / name
                if path.is_symlink():
                    failure(str(path.relative_to(layout.workspace)), "SYMLINK")
                    directories.remove(name)
                elif not policy["include_hidden"] and name.startswith("."):
                    directories.remove(name)
            if not policy["recursive"]:
                directories.clear()
            for name in sorted(names):
                if not policy["include_hidden"] and name.startswith("."):
                    continue
                candidates.append(Path(directory) / name)
                if len(candidates) > policy["max_files"]:
                    failure(policy["root"], "FILE_LIMIT")
                    break
            if len(candidates) > policy["max_files"]:
                break
    size_total = 0
    for path in sorted(candidates[: policy["max_files"]]):
        relative = path.relative_to(layout.workspace).as_posix()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    failure(relative, "NOT_REGULAR_FILE")
                    continue
                if size_total + info.st_size > policy["max_bytes"]:
                    failure(relative, "BYTE_LIMIT")
                    continue
                size_total += info.st_size
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
                stream.seek(0)
                uri = f"results/{spec['task_id']}/attempts/{spec['attempt_id']}/{relative}"
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                client = client or get_minio_client()
                if not client.bucket_exists(BUCKET):
                    client.make_bucket(BUCKET)
                client.put_object(
                    BUCKET,
                    uri,
                    stream,
                    info.st_size,
                    content_type=media_type,
                    metadata={"sha256": digest},
                )
                record = {
                    "path": relative,
                    "s3_path": uri,
                    "size_bytes": info.st_size,
                    "sha256": digest,
                    "media_type": media_type,
                }
                files.append(record)
                announcements.append(
                    {
                        "path": relative,
                        "media_type": media_type,
                        "metadata": {"delivery": "complete", **record},
                    }
                )
        except OSError:
            failure(relative, "FILE_UNREADABLE")
        except Exception:
            failure(relative, "UPLOAD_FAILED")
    saved = {record["path"] for record in files}
    failed = {record["path"] for record in errors}
    for declared in result.get("metadata", {}).get("declared_artifacts", []):
        path = PurePosixPath(declared)
        if path.is_absolute():
            if not path.is_relative_to(spec.get("cwd", "/workspace")):
                failure(declared, "OUTSIDE_WORKSPACE")
                continue
            path = path.relative_to(spec.get("cwd", "/workspace"))
        relative = path.as_posix()
        if ".." in path.parts:
            failure(relative, "OUTSIDE_WORKSPACE")
        elif not path.is_relative_to(PurePosixPath(policy["root"])):
            announcements.append({"path": relative, "metadata": {"delivery": "excluded_by_policy"}})
        elif relative not in saved and relative not in failed:
            failure(relative, "DECLARED_FILE_NOT_PRESERVED")
    metadata = {
        **result.get("metadata", {}),
        "artifacts": files,
        "artifact_errors": errors,
        "artifact_delivery": "partial" if errors else "complete",
    }
    if errors:
        return {
            "status": "failed",
            "summary": "Artifact delivery failed",
            "error": {
                "code": "ARTIFACT_DELIVERY",
                "message": "Some task files could not be preserved",
            },
            "metadata": {**metadata, "connector_result": result},
        }, announcements
    return {**result, "metadata": metadata}, announcements
