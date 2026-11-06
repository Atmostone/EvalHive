"""Administrator-produced native dataset receipts, consumed without backend imports."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from evalhive_benchmark.installation import validate_installation

from .format import CaseFormatError, load_suite, resolve_case

CONNECTOR_ID = "evalhive-cases"
CONNECTOR_VERSION = "0.1.0"


def build_receipt(
    root,
    suite,
    revision,
    worker_image,
    *,
    dataset_id=None,
    connector_id=CONNECTOR_ID,
    connector_version=CONNECTOR_VERSION,
):
    cases = load_suite(root, suite)
    if not cases:
        raise CaseFormatError("An installed dataset must contain cases")
    descriptors = [case.descriptor(revision, dataset_id=dataset_id or suite) for case in cases]
    source = [{"case_id": case.id, "digest": case.source_digest} for case in cases]
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return validate_installation(
        {
            "format_version": "1.0",
            "connector_id": connector_id,
            "connector_version": connector_version,
            "dataset_id": dataset_id or suite,
            "revision": revision,
            "source_digest": "sha256:" + digest,
            "worker_image": worker_image,
            "cases": descriptors,
            "provenance": {"native_format": "evalhive.case.v1", "suite": suite},
        }
    )


class InstalledCases:
    def __init__(
        self, root, receipt=None, *, connector_id=CONNECTOR_ID, connector_version=CONNECTOR_VERSION
    ):
        self.root = Path(root)
        if receipt is None:
            path = self.root / "installation.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024**2:
                raise CaseFormatError("Native dataset installation is unavailable")
            with path.open("rb") as stream:
                data = stream.read(64 * 1024**2 + 1)
            if len(data) > 64 * 1024**2:
                raise CaseFormatError("Native dataset installation exceeds its byte limit")
            receipt = json.loads(data)
        self.receipt = deepcopy(validate_installation(receipt))
        receipt = self.receipt
        if (receipt["connector_id"], receipt["connector_version"]) != (
            connector_id,
            connector_version,
        ):
            raise CaseFormatError("Dataset belongs to another connector version")
        self.cases = {
            (case["ref"].get("split"), case["ref"]["case_id"]): case for case in receipt["cases"]
        }

    def descriptor(self, reference):
        if (reference["dataset_id"], reference["revision"]) != (
            self.receipt["dataset_id"],
            self.receipt["revision"],
        ):
            raise CaseFormatError("Dataset reference differs from the installed revision")
        result = self.cases.get((reference.get("split"), reference["case_id"]))
        if result is None or result["ref"] != reference:
            raise CaseFormatError("Case reference is not installed")
        return result

    def resolve(self, descriptor):
        expected = self.descriptor(descriptor["ref"])
        if descriptor != expected:
            raise CaseFormatError("Case descriptor differs from the frozen installation")
        return resolve_case(self.root, expected)
