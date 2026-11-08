"""Immutable, administrator-produced dataset metadata, separate from execution."""

from __future__ import annotations

import hashlib
import json

from .protocol import ProtocolError, validate


def dataset_key(dataset_id: str, revision: str) -> str:
    """A path-safe registry key; dataset/revision values are never filesystem paths."""
    validate("case_ref", {"dataset_id": dataset_id, "revision": revision, "case_id": "registry"})
    return hashlib.sha256((dataset_id + "\0" + revision).encode()).hexdigest()


def validate_installation(receipt: dict) -> dict:
    validate("dataset_installation", receipt)
    seen = set()
    for case in receipt["cases"]:
        ref = case["ref"]
        if ref["dataset_id"] != receipt["dataset_id"] or ref["revision"] != receipt["revision"]:
            raise ProtocolError("INVALID_INSTALLATION", "Case belongs to another dataset revision", "configuration")
        identity = (ref.get("split"), ref["case_id"])
        if identity in seen:
            raise ProtocolError("INVALID_INSTALLATION", "Dataset contains duplicate case identities", "configuration")
        seen.add(identity)
    return receipt


def installation_digest(receipt: dict) -> str:
    validate_installation(receipt)
    return "sha256:" + hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False).encode()).hexdigest()
