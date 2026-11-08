import copy
import hashlib
from pathlib import Path

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_benchmark.sources import load_source_lock, validate_source_lock, verify_source_bytes

ROOT = Path(__file__).parents[2]


def test_committed_source_revisions_and_distribution_hashes():
    lock = load_source_lock(ROOT / "sources.lock.json")
    assert {source["id"] for source in lock["sources"]} == {
        "harbor",
        "terminal-bench-2",
        "tau2",
        "mcpmark",
        "appworld",
        "swebench",
        "swebench-tasks",
        "toolathlon-gym",
    }
    assert (
        next(source for source in lock["sources"] if source["id"] == "tau2")["revision"]
        == "fc0055dc4e0a316c3f83133267fbd6faaa770992"
    )
    assert lock["installation_policy"]["dataset_content_digest_required"] is True


def test_source_verifier_rejects_changed_bytes():
    lock = load_source_lock(ROOT / "sources.lock.json")
    fixture = copy.deepcopy(lock)
    for source in fixture["sources"]:
        for item in source["contract_files"]:
            item["size_bytes"] = 7
            item["sha256"] = hashlib.sha256(b"fixture").hexdigest()
    result = verify_source_bytes(fixture, lambda _: b"fixture")
    assert result["contract_files"] > 8
    with pytest.raises(ProtocolError) as caught:
        verify_source_bytes(fixture, lambda _: b"tampered")
    assert caught.value.code == "SOURCE_INTEGRITY"


@pytest.mark.parametrize("mutation", ["floating", "path", "missing", "credentials"])
def test_invalid_source_lock(mutation):
    lock = load_source_lock(ROOT / "sources.lock.json")
    if mutation == "floating":
        lock["sources"][0]["revision"] = "main"
    elif mutation == "path":
        lock["sources"][0]["contract_files"][0]["path"] = "../private"
    elif mutation == "missing":
        lock["datasets"][0]["source"] = "absent"
    else:
        lock["packages"][0]["distributions"][0]["url"] += "?api_key=private"
    with pytest.raises(ProtocolError):
        validate_source_lock(lock)
