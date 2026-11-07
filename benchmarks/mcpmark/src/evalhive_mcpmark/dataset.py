"""Freeze native catalog identities and private checker inputs without flattening."""

from __future__ import annotations

from copy import deepcopy

from evalhive_benchmark.installation import validate_installation

from .native import REVISION, NativeConfigurationError, catalog, directory, fingerprint, inventory

CONNECTOR_ID, CONNECTOR_VERSION = "mcpmark", "0.1.0"


def descriptor(task, root, service, suite, dataset_id, revision, source_digest):
    return {
        "ref": {
            "dataset_id": dataset_id,
            "revision": revision,
            "case_id": service + "/" + suite + "/" + task.name,
        },
        "title": task.name,
        "source_digest": fingerprint(
            {
                "source": source_digest,
                "service": service,
                "suite": suite,
                "category_id": task.category_id,
                "task_id": str(task.task_id),
            }
        ),
        "native_ref": {
            "service": service,
            "suite": suite,
            "category_id": task.category_id,
            "task_id": str(task.task_id),
            "instruction_path": task.task_instruction_path.relative_to(root).as_posix(),
            "verification_path": task.task_verification_path.relative_to(root).as_posix(),
        },
        "requirements": {"capabilities": ["host.agent_api_v1"]},
    }


def build_receipt(root, revision, worker_image, *, service, suite="standard", dataset_id=None):
    root = directory(root)
    files = inventory(root, service, suite)
    source_digest = fingerprint({"native_revision": REVISION, "files": files})
    _, tasks = catalog(root, service, suite)
    if files != inventory(root, service, suite):
        raise NativeConfigurationError("Native source changed while discovering tasks")
    dataset_id = dataset_id or "mcpmark-verified-" + service + "-" + suite
    return validate_installation(
        {
            "format_version": "1.0",
            "connector_id": CONNECTOR_ID,
            "connector_version": CONNECTOR_VERSION,
            "dataset_id": dataset_id,
            "revision": revision,
            "source_digest": source_digest,
            "worker_image": worker_image,
            "cases": [
                descriptor(task, root, service, suite, dataset_id, revision, source_digest)
                for task in tasks
            ],
            "provenance": {
                "native_format": "mcpmark.verified",
                "native_revision": REVISION,
                "service": service,
                "suite": suite,
                "files": files,
            },
        }
    )


class InstalledMCPMark:
    def __init__(self, root, receipt):
        self.root = directory(root)
        self.receipt = deepcopy(validate_installation(receipt))
        provenance = self.receipt["provenance"]
        if (
            receipt["connector_id"],
            receipt["connector_version"],
            provenance.get("native_revision"),
        ) != (CONNECTOR_ID, CONNECTOR_VERSION, REVISION):
            raise NativeConfigurationError("Native installation requires the pinned connector")
        current = build_receipt(
            self.root,
            receipt["revision"],
            receipt["worker_image"],
            service=provenance["service"],
            suite=provenance["suite"],
            dataset_id=receipt["dataset_id"],
        )
        if current != self.receipt:
            raise NativeConfigurationError("Native catalog differs from its frozen installation")
        self.manager, tasks = catalog(self.root, provenance["service"], provenance["suite"])
        self.tasks = {task.name: task for task in tasks}
        self.cases = {case["ref"]["case_id"]: case for case in self.receipt["cases"]}

    def resolve(self, selected):
        expected = self.cases.get(selected.get("ref", {}).get("case_id"))
        provenance = self.receipt["provenance"]
        if (
            selected != expected
            or inventory(self.root, provenance["service"], provenance["suite"])
            != provenance["files"]
        ):
            raise NativeConfigurationError("Native case or source differs from its installation")
        return deepcopy(self.tasks[selected["title"]])
