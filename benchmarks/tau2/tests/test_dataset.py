"""Real native splits remain private, immutable and independently selectable."""

import os
from copy import deepcopy
from pathlib import Path

import pytest
from evalhive_tau2.dataset import InstalledTau2, build_receipt
from evalhive_tau2.native import NativeConfigurationError, inventory, load_domain, source_files

IMAGE = "sha256:" + "a" * 64


def test_native_import_requires_explicit_data_and_disabled_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("TAU2_DATA_DIR", raising=False)
    with pytest.raises(NativeConfigurationError, match="disabled dotenv"):
        load_domain(tmp_path, "airline")
    with pytest.raises(NativeConfigurationError, match="unsupported"):
        load_domain(tmp_path, "voice")


def test_inventory_rejects_links_instead_of_reading_foreign_data(tmp_path):
    relative = source_files("airline")[0]
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("unrelated")
    path.symlink_to(outside)
    # Inventory sorts paths, so provide regular files for preceding entries.
    for name in source_files("airline"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target != path:
            target.write_text("{}")
    with pytest.raises(NativeConfigurationError, match="links"):
        inventory(tmp_path, "airline")


@pytest.fixture
def native_data():
    value = os.environ.get("EVALHIVE_NATIVE_TAU2_DATA")
    if not value:
        pytest.skip("Requires the pinned native tau2 wheel and private baseline data")
    return Path(value)


@pytest.mark.parametrize("domain,count", [("airline", 50), ("retail", 114), ("telecom", 114)])
def test_complete_native_base_split_uses_native_models_without_gold_in_catalog(
    native_data, domain, count
):
    receipt = build_receipt(native_data, "native-base", IMAGE, domain=domain)
    assert len(receipt["cases"]) == count
    assert receipt["provenance"]["split"] == "base"
    assert receipt == build_receipt(native_data, "native-base", IMAGE, domain=domain)
    installed = InstalledTau2(native_data, receipt)
    case = receipt["cases"][0]
    native = installed.resolve(case)
    assert native.id == case["native_ref"]["task_id"]
    assert set(case) == {
        "ref",
        "title",
        "source_digest",
        "native_ref",
        "requirements",
        "evaluation_requirements",
    }
    assert "user_scenario" not in str(receipt)
    assert "evaluation_criteria" not in str(receipt)
    native.id = "changed local copy"
    assert installed.resolve(case).id == case["native_ref"]["task_id"]
    for field in ("revision", "source_digest", "native_ref"):
        altered = deepcopy(case)
        if field == "revision":
            altered["ref"]["revision"] = "foreign"
        elif field == "source_digest":
            altered[field] = "sha256:" + "b" * 64
        else:
            altered[field]["domain"] = "foreign"
        with pytest.raises(NativeConfigurationError, match="frozen"):
            installed.resolve(altered)


def test_telecom_full_is_explicit_and_distinct_from_baseline(native_data):
    base = build_receipt(native_data, "native-base", IMAGE, domain="telecom")
    full = build_receipt(native_data, "native-full", IMAGE, domain="telecom", split="full")
    assert len(base["cases"]) == 114 and len(full["cases"]) == 2285
    assert base["dataset_id"] != full["dataset_id"]
    assert base["source_digest"] != full["source_digest"]
    with pytest.raises(NativeConfigurationError, match="unavailable"):
        build_receipt(native_data, "unknown", IMAGE, domain="telecom", split="invented")
