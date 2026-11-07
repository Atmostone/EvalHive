"""Discover complete native classic subsets without exposing evaluator-only patches."""

from __future__ import annotations

import re
from copy import deepcopy

from evalhive_benchmark.installation import validate_installation

from .native import (
    REVISION,
    SUBSETS,
    TASK_REVISION,
    VERSION,
    NativeConfigurationError,
    directory,
    fingerprint,
    inventory,
    verified_code,
)

CONNECTOR_ID, CONNECTOR_VERSION = "swebench", "0.1.0"


def native_cases(root, subset, split):
    if subset not in SUBSETS or split not in {"dev", "test"}:
        raise NativeConfigurationError("Native classic subset and split must be explicit")
    root = directory(root)
    verified_code()
    from swebench.task.repo import published_tasks, task_dirs

    # Validate paths before native discovery reads any protected task contents.
    inventory(root, [path.name for path in task_dirs(root)])
    published = published_tasks(root, datasets=[SUBSETS[subset]])
    rows = published[SUBSETS[subset]].get(split)
    if not rows:
        raise NativeConfigurationError("The native subset does not publish this split")
    identities = set()
    for row in rows:
        identity = row["instance_id"]
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[1-9][0-9]*", identity)
            or identity in identities
            or row.get("split") != split
            or not re.fullmatch(r"[a-f0-9]{40}", row.get("base_commit", ""))
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row.get("repo", ""))
            or not isinstance(row.get("image"), str)
            or not isinstance(row.get("problem_statement"), str)
            or not isinstance(row.get("test_patch"), str)
            or not isinstance(row.get("patch"), str)
        ):
            raise NativeConfigurationError("Native task repository has inconsistent identities")
        identities.add(identity)
    return sorted(rows, key=lambda row: row["instance_id"])


def build_receipt(
    root, revision, worker_image, *, subset="verified", split="test", dataset_id=None
):
    root = directory(root)
    rows = native_cases(root, subset, split)
    identities = [row["instance_id"] for row in rows]
    records = inventory(root, identities)
    source_digest = fingerprint(
        {
            "native_revision": REVISION,
            "task_revision": TASK_REVISION,
            "subset": subset,
            "split": split,
            "files": records,
        }
    )
    dataset_id = dataset_id or "swebench-classic-" + subset
    cases = [
        {
            "ref": {
                "dataset_id": dataset_id,
                "revision": revision,
                "case_id": row["instance_id"],
                "split": split,
            },
            "title": row["instance_id"],
            "description": row["problem_statement"],
            "source_digest": fingerprint(
                {"source": source_digest, "instance_id": row["instance_id"]}
            ),
            "native_ref": {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "base_commit": row["base_commit"],
                "subset": subset,
                "split": split,
                "original_image": row["image"],
            },
            "requirements": {
                "capabilities": ["host.agent_api_v1", "host.ephemeral_exec_v1"],
                "platform": {"os": "linux", "architecture": "amd64", "libc": "glibc"},
            },
        }
        for row in rows
    ]
    if records != inventory(root, identities):
        raise NativeConfigurationError("Native task repository changed during discovery")
    return validate_installation(
        {
            "format_version": "1.0",
            "connector_id": CONNECTOR_ID,
            "connector_version": CONNECTOR_VERSION,
            "dataset_id": dataset_id,
            "revision": revision,
            "source_digest": source_digest,
            "worker_image": worker_image,
            "cases": cases,
            "provenance": {
                "native_format": "swebench.classic",
                "native_revision": REVISION,
                "native_version": VERSION,
                "task_revision": TASK_REVISION,
                "subset": subset,
                "split": split,
                "files": records,
            },
        }
    )


class InstalledSWEbench:
    def __init__(self, root, receipt):
        self.root, self.receipt = directory(root), deepcopy(validate_installation(receipt))
        provenance = self.receipt.get("provenance", {})
        if (
            self.receipt["connector_id"] != CONNECTOR_ID
            or self.receipt["connector_version"] != CONNECTOR_VERSION
            or provenance.get("native_revision") != REVISION
            or provenance.get("native_version") != VERSION
            or provenance.get("task_revision") != TASK_REVISION
        ):
            raise NativeConfigurationError(
                "Native installation requires its pinned evaluator and tasks"
            )
        rebuilt = build_receipt(
            self.root,
            self.receipt["revision"],
            self.receipt["worker_image"],
            subset=provenance["subset"],
            split=provenance["split"],
            dataset_id=self.receipt["dataset_id"],
        )
        if rebuilt != self.receipt:
            raise NativeConfigurationError(
                "Native dataset or membership differs from its installation"
            )
        self.cases = {case["ref"]["case_id"]: case for case in self.receipt["cases"]}

    def resolve(self, selected):
        expected = self.cases.get(selected.get("ref", {}).get("case_id"))
        if expected is None or expected != selected:
            raise NativeConfigurationError("Native case differs from its installed identity")
        if inventory(self.root, list(self.cases)) != self.receipt["provenance"]["files"]:
            raise NativeConfigurationError("Native task inputs changed after installation")
        verified_code()
        from swebench.task.repo import load_task

        return load_task(self.root / "tasks" / expected["native_ref"]["instance_id"])
