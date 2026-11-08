"""Validate source identities and verify bytes without importing native code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .protocol import ProtocolError, validate


def validate_source_lock(lock: dict) -> dict:
    validate("source_lock", lock)
    sources = {item["id"] for item in lock["sources"]}
    if len(sources) != len(lock["sources"]):
        raise ProtocolError("INVALID_SOURCE_LOCK", "Duplicate source identity")
    datasets = [item["id"] for item in lock["datasets"]]
    if len(set(datasets)) != len(datasets):
        raise ProtocolError("INVALID_SOURCE_LOCK", "Duplicate dataset identity")
    if any(item["source"] not in sources for item in lock["datasets"]):
        raise ProtocolError("INVALID_SOURCE_LOCK", "Dataset source is unavailable")
    for source in lock["sources"]:
        paths = set()
        for item in source["contract_files"]:
            path = PurePosixPath(item["path"])
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in item["path"]
                or item["path"] in paths
            ):
                raise ProtocolError("INVALID_SOURCE_LOCK", "Contract file path is invalid")
            paths.add(item["path"])
    for package in lock["packages"]:
        for item in package["distributions"]:
            endpoint = urlsplit(item["url"])
            if (
                endpoint.scheme != "https"
                or endpoint.hostname != "files.pythonhosted.org"
                or any((endpoint.username, endpoint.password, endpoint.query, endpoint.fragment))
            ):
                raise ProtocolError("INVALID_SOURCE_LOCK", "Distribution URL is invalid")
    return lock


def load_source_lock(path: str | Path) -> dict:
    data = Path(path).read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise ProtocolError("INVALID_SOURCE_LOCK", "Source lock exceeds size limit")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("INVALID_SOURCE_LOCK", "Source lock is invalid JSON") from None
    return validate_source_lock(value)


def verify_source_bytes(lock: dict, fetch, *, include_packages=False) -> dict:
    """The caller supplies transport and its resource policy; this SDK owns no HTTP client."""
    validate_source_lock(lock)
    verified = 0
    for source in lock["sources"]:
        repository = source["repository"].removeprefix("https://github.com/")
        for item in source["contract_files"]:
            data = fetch(
                f"https://raw.githubusercontent.com/{repository}/{source['revision']}/{item['path']}"
            )
            if (
                len(data) != item["size_bytes"]
                or hashlib.sha256(data).hexdigest() != item["sha256"]
            ):
                raise ProtocolError("SOURCE_INTEGRITY", "Native source bytes differ from the lock")
            verified += 1
    packages = 0
    if include_packages:
        for package in lock["packages"]:
            for item in package["distributions"]:
                if hashlib.sha256(fetch(item["url"])).hexdigest() != item["sha256"]:
                    raise ProtocolError(
                        "SOURCE_INTEGRITY", "Published distribution differs from the lock"
                    )
                packages += 1
    return {"sources": len(lock["sources"]), "contract_files": verified, "distributions": packages}
