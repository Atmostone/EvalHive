"""Unit tests for the Benchmark Case Store loader (pre-E-23).

Covers parsing/validation of case files (YAML + JSON), suite/dup guards, and the
category→capability_spec carry. Materialization (DB) is in the integration tests.
"""

import json

import pytest

from app.quality import benchmark
from app.quality.benchmark import (
    BenchmarkCase,
    _capability_spec_for,
    load_cases,
)


def _write(dirpath, name, data):
    p = dirpath / name
    if name.endswith(".json"):
        p.write_text(json.dumps(data), encoding="utf-8")
    else:
        import yaml

        p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


@pytest.fixture
def suite_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "BENCHMARKS_DIR", tmp_path)
    d = tmp_path / "capability-isolation"
    d.mkdir()
    return d


def _case(cid, **over):
    base = {
        "id": cid,
        "suite": "capability-isolation",
        "category": "exact_compute",
        "input": {"title": f"t-{cid}", "description": "d"},
        "gold": {"capability_spec": {"required_tools": ["bash"]}, "reference_answer": "42"},
    }
    base.update(over)
    return base


def test_load_cases_yaml_and_json(suite_dir):
    _write(suite_dir, "a.yaml", _case("c1"))
    _write(suite_dir, "b.json", _case("c2"))
    _write(suite_dir, "ignored.txt", {"id": "x"})  # non-case file ignored
    cases = load_cases("capability-isolation")
    assert [c.id for c in cases] == ["c1", "c2"]
    assert cases[0].gold.capability_spec == {"required_tools": ["bash"]}
    assert cases[0].gold.reference_answer == "42"


def test_load_cases_rejects_duplicate_id(suite_dir):
    _write(suite_dir, "a.yaml", _case("dup"))
    _write(suite_dir, "b.yaml", _case("dup"))
    with pytest.raises(ValueError, match="duplicate case id"):
        load_cases("capability-isolation")


def test_load_cases_rejects_suite_mismatch(suite_dir):
    _write(suite_dir, "a.yaml", _case("c1", suite="other-suite"))
    with pytest.raises(ValueError, match="suite"):
        load_cases("capability-isolation")


def test_load_cases_missing_suite_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark, "BENCHMARKS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_cases("nope")


def test_case_requires_input_title():
    with pytest.raises(Exception):
        BenchmarkCase(id="x", suite="s", input={})


def test_capability_spec_carries_category():
    case = BenchmarkCase(**_case("c1", category="fresh_data",
                                 gold={"capability_spec": {"required_tools": ["web_search"]}}))
    spec = _capability_spec_for(case)
    assert spec == {"required_tools": ["web_search"], "category": "fresh_data"}


def test_capability_spec_none_without_spec():
    case = BenchmarkCase(**_case("c1", gold={"reference_answer": "x"}))
    assert _capability_spec_for(case) is None
