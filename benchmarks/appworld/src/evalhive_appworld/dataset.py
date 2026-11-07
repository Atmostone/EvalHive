"""Freeze native split membership, scenario grouping and private data identities."""

from __future__ import annotations

from copy import deepcopy

from evalhive_benchmark.installation import validate_installation

from .native import (
    DATA_VERSION,
    REVISION,
    VERSION,
    NativeConfigurationError,
    canonical,
    directory,
    fingerprint,
    inventory,
    load,
    task_ids,
)

CONNECTOR_ID, CONNECTOR_VERSION = "appworld", "0.1.0"


def build_receipt(root, revision, worker_image, *, split, dataset_id=None):
    root = directory(root)
    identities = task_ids(root, split)
    files = inventory(root, split, identities)
    source_digest = fingerprint(
        {"revision": REVISION, "data_version": DATA_VERSION, "files": files}
    )
    # Task.load uses native compatibility and documentation checks without gold.
    load(root)
    from appworld.task import Task

    dataset_id = dataset_id or "appworld-" + split
    cases = []
    for identity in identities:
        task = Task.load(identity, load_ground_truth=False)
        cases.append(
            {
                "ref": {
                    "dataset_id": dataset_id,
                    "revision": revision,
                    "case_id": identity,
                    "split": split,
                },
                "title": identity,
                "source_digest": fingerprint({"source": source_digest, "task_id": identity}),
                "native_ref": {
                    "task_id": identity,
                    "scenario_id": task.generator_id,
                    "split": split,
                },
                "requirements": {
                    "capabilities": ["host.agent_api_v1", "host.resource_finalizers_v1"]
                },
            }
        )
    if files != inventory(root, split, identities):
        raise NativeConfigurationError("Native dataset changed during catalog discovery")
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
                "native_format": "appworld.mcp",
                "native_revision": REVISION,
                "native_version": VERSION,
                "data_version": DATA_VERSION,
                "split": split,
                "files": files,
            },
        }
    )


class InstalledAppWorld:
    def __init__(self, root, receipt):
        self.root = directory(root)
        if len(canonical(receipt)) > 32 * 1024**2:
            raise NativeConfigurationError("Native dataset metadata exceeds its byte budget")
        self.receipt = deepcopy(validate_installation(receipt))
        provenance = self.receipt.get("provenance", {})
        if (
            self.receipt["connector_id"] != CONNECTOR_ID
            or self.receipt["connector_version"] != CONNECTOR_VERSION
            or provenance.get("native_revision") != REVISION
            or provenance.get("native_version") != VERSION
            or provenance.get("data_version") != DATA_VERSION
        ):
            raise NativeConfigurationError("Native dataset requires its pinned connector and data")
        rebuilt = build_receipt(
            self.root,
            self.receipt["revision"],
            self.receipt["worker_image"],
            split=provenance["split"],
            dataset_id=self.receipt["dataset_id"],
        )
        if rebuilt != self.receipt:
            raise NativeConfigurationError("Native dataset or split membership changed")
        self.cases = {case["ref"]["case_id"]: case for case in self.receipt["cases"]}

    def resolve(self, selected):
        expected = self.cases.get(selected.get("ref", {}).get("case_id"))
        if expected is None or selected != expected:
            raise NativeConfigurationError("Native case differs from its installed identity")
        provenance = self.receipt["provenance"]
        if inventory(self.root, provenance["split"], list(self.cases)) != provenance["files"]:
            raise NativeConfigurationError("Native data changed after installation")
        load(self.root)
        from appworld.task import Task

        return Task.load(expected["native_ref"]["task_id"], load_ground_truth=False)
