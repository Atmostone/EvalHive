import copy

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.installation import dataset_key, installation_digest, validate_installation


def installation():
    return {"format_version": "1.0", "connector_id": "example", "connector_version": "0.1.0",
            "dataset_id": "cases", "revision": "refs/versions:one", "source_digest": "sha256:" + "a" * 64,
            "worker_image": "example@sha256:" + "b" * 64,
            "cases": [{"ref": {"dataset_id": "cases", "revision": "refs/versions:one", "case_id": "one"},
                       "title": "Example", "source_digest": "sha256:" + "c" * 64,
                       "native_ref": {"path": "/private/data"}, "requirements": {}}]}


def test_installation_freezes_exact_native_metadata_with_safe_registry_identity():
    receipt = installation()
    assert validate_installation(receipt) == receipt
    assert installation_digest(receipt) == installation_digest(dict(reversed(list(receipt.items()))))
    assert len(dataset_key("cases", "../../data")) == 64
    changed = copy.deepcopy(receipt)
    changed["cases"][0]["native_ref"]["path"] = "/private/other"
    assert installation_digest(receipt) != installation_digest(changed)


@pytest.mark.parametrize("mutation", ["floating_image", "duplicate_case", "wrong_revision"])
def test_invalid_installations_fail_closed(mutation):
    receipt = installation()
    if mutation == "floating_image":
        receipt["worker_image"] = "example:latest"
    elif mutation == "duplicate_case":
        receipt["cases"].append(copy.deepcopy(receipt["cases"][0]))
    else:
        receipt["cases"][0]["ref"]["revision"] = "different"
    with pytest.raises(ProtocolError):
        validate_installation(receipt)
