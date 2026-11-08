"""Byte-pinned Harbor task metadata; native source and gold remain worker-private."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path, PurePosixPath

from evalhive_benchmark.installation import validate_installation

from .native import NATIVE_REVISION, NATIVE_VERSION, HarborConfigurationError, parse_task

CONNECTOR_ID, CONNECTOR_VERSION = "harbor", "0.1.0"
MAX_RECEIPT_BYTES = 64 * 1024**2
MAX_TASKS = 10000


def task_path(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise HarborConfigurationError("Native task path must be a relative POSIX path")
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != relative:
        raise HarborConfigurationError("Native task path must remain inside its installation")
    result = root / path
    if result.resolve(strict=True) != result or not result.is_relative_to(root):
        raise HarborConfigurationError("Native task path cannot traverse installation links")
    return result


def descriptor(native, relative, dataset_id, revision):
    result = {
        "ref": {"dataset_id": dataset_id, "revision": revision, "case_id": relative},
        "title": relative,
        "source_digest": native.source_digest,
        "native_ref": {"path": relative, "verifier_mode": native.verifier_mode},
        "requirements": native.requirements(),
    }
    if (requirements := native.evaluation_requirements()) is not None:
        result["evaluation_requirements"] = requirements
    return result


def build_receipt(root, revision, worker_image, *, dataset_id="terminal-bench-2", task_paths=None):
    root = Path(root).absolute()
    if root.resolve(strict=True) != root:
        raise HarborConfigurationError("Native dataset root cannot traverse links")
    if task_paths is None:
        task_paths = []
        for child in root.iterdir():
            if (child / "task.toml").is_file():
                task_paths.append(child.name)
            if len(task_paths) > MAX_TASKS:
                raise HarborConfigurationError("Native dataset exceeds its task limit")
    else:
        selected = []
        for relative in task_paths:
            selected.append(relative)
            if len(selected) > MAX_TASKS:
                raise HarborConfigurationError("Native dataset exceeds its task limit")
        task_paths = selected
    if not task_paths or len(set(task_paths)) != len(task_paths):
        raise HarborConfigurationError("Native installation requires distinct tasks")
    cases = [descriptor(parse_task(task_path(root, relative)), relative, dataset_id, revision)
             for relative in sorted(task_paths)]
    source = [{"path": case["native_ref"]["path"], "digest": case["source_digest"]} for case in cases]
    digest = "sha256:" + hashlib.sha256(json.dumps(
        source, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    return validate_installation({
        "format_version": "1.0", "connector_id": CONNECTOR_ID,
        "connector_version": CONNECTOR_VERSION, "dataset_id": dataset_id,
        "revision": revision, "source_digest": digest, "worker_image": worker_image,
        "cases": cases, "provenance": {
            "native_format": "harbor.task", "harbor_version": NATIVE_VERSION,
            "harbor_revision": NATIVE_REVISION,
        },
    })


class InstalledHarbor:
    def __init__(self, root, receipt):
        self.root = Path(root).absolute()
        if self.root.resolve(strict=True) != self.root:
            raise HarborConfigurationError("Native dataset root cannot traverse links")
        if len(json.dumps(receipt, ensure_ascii=False).encode()) > MAX_RECEIPT_BYTES:
            raise HarborConfigurationError("Native installation exceeds its metadata limit")
        self.receipt = deepcopy(validate_installation(receipt))
        provenance = self.receipt.get("provenance", {})
        if (
            (self.receipt["connector_id"], self.receipt["connector_version"])
            != (CONNECTOR_ID, CONNECTOR_VERSION)
            or (provenance.get("harbor_version"), provenance.get("harbor_revision"))
            != (NATIVE_VERSION, NATIVE_REVISION)
        ):
            raise HarborConfigurationError("Native installation requires its pinned connector")
        self.cases = {case["ref"]["case_id"]: case for case in self.receipt["cases"]}

    def resolve(self, selected):
        expected = self.cases.get(selected.get("ref", {}).get("case_id"))
        if expected is None or selected != expected:
            raise HarborConfigurationError("Native case differs from its frozen installation")
        relative = expected["native_ref"]["path"]
        native = parse_task(task_path(self.root, relative))
        current = descriptor(native, relative, self.receipt["dataset_id"], self.receipt["revision"])
        if current != expected:
            raise HarborConfigurationError("Native task source or requirements changed")
        return native
