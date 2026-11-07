"""Frozen native split identities without publishing scenarios or gold."""

from __future__ import annotations

from copy import deepcopy

from evalhive_benchmark.installation import validate_installation

from .native import (
    REVISION,
    VERSION,
    NativeConfigurationError,
    canonical,
    catalog,
    data_root,
    fingerprint,
    inventory,
)

CONNECTOR_ID, CONNECTOR_VERSION = "tau2", "0.1.0"
MAX_RECEIPT_BYTES = 32 * 1024**2


def descriptor(task, domain, split, dataset_id, revision, source_digest):
    judge = bool(
        task.evaluation_criteria and "NL_ASSERTION" in task.evaluation_criteria.reward_basis
    )
    return {
        "ref": {"dataset_id": dataset_id, "revision": revision, "case_id": domain + "/" + task.id},
        "title": domain + "/" + task.id,
        "source_digest": fingerprint(
            {"source": source_digest, "task": task.model_dump(mode="json")}
        ),
        "native_ref": {"domain": domain, "split": split, "task_id": task.id},
        "requirements": {
            "capabilities": ["host.agent_api_v1", "host.model_api_v1"],
            "resources": {},
        },
        "evaluation_requirements": {
            "capabilities": ["host.model_api_v1"] if judge else [],
            "resources": {},
        },
    }


def build_receipt(root, revision, worker_image, *, domain, split="base", dataset_id=None):
    root = data_root(root)
    files = inventory(root, domain)
    source_digest = fingerprint(files)
    dataset_id = dataset_id or "tau2-" + domain + "-" + split
    tasks = catalog(root, domain, split)
    if files != inventory(root, domain):
        raise NativeConfigurationError("Native source changed while parsing the selected split")
    return validate_installation(
        {
            "format_version": "1.0",
            "connector_id": CONNECTOR_ID,
            "connector_version": CONNECTOR_VERSION,
            "dataset_id": dataset_id,
            "revision": revision,
            "source_digest": fingerprint({"files": files, "split": split}),
            "worker_image": worker_image,
            "cases": [
                descriptor(task, domain, split, dataset_id, revision, source_digest)
                for task in tasks
            ],
            "provenance": {
                "native_format": "tau2.half_duplex",
                "native_version": VERSION,
                "native_revision": REVISION,
                "domain": domain,
                "split": split,
                "files": files,
                "domain_source_digest": source_digest,
            },
        }
    )


class InstalledTau2:
    def __init__(self, root, receipt):
        self.root = data_root(root)
        if len(canonical(receipt)) > MAX_RECEIPT_BYTES:
            raise NativeConfigurationError("Native installation exceeds its metadata budget")
        self.receipt = deepcopy(validate_installation(receipt))
        provenance = self.receipt.get("provenance", {})
        if (self.receipt["connector_id"], self.receipt["connector_version"]) != (
            CONNECTOR_ID,
            CONNECTOR_VERSION,
        ) or (provenance.get("native_version"), provenance.get("native_revision")) != (
            VERSION,
            REVISION,
        ):
            raise NativeConfigurationError("Native installation requires its pinned connector")
        current = build_receipt(
            self.root,
            self.receipt["revision"],
            self.receipt["worker_image"],
            domain=provenance["domain"],
            split=provenance["split"],
            dataset_id=self.receipt["dataset_id"],
        )
        if current != self.receipt:
            raise NativeConfigurationError("Native source or selected split changed")
        self.cases = {case["ref"]["case_id"]: case for case in self.receipt["cases"]}
        self.tasks = {
            task.id: task for task in catalog(self.root, provenance["domain"], provenance["split"])
        }

    def resolve(self, selected):
        expected = self.cases.get(selected.get("ref", {}).get("case_id"))
        if expected is None or selected != expected:
            raise NativeConfigurationError("Native case differs from its frozen installation")
        provenance = self.receipt["provenance"]
        if inventory(self.root, provenance["domain"]) != provenance["files"]:
            raise NativeConfigurationError("Native source changed after installation")
        return self.tasks[expected["native_ref"]["task_id"]].model_copy(deep=True)
