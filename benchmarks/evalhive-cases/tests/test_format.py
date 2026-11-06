"""Native format compatibility, private gold and installed path boundaries."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from evalhive_cases.format import CaseFormatError, load_suite, parse_case

DOCUMENT = {
    "id": "case-1",
    "suite": "example",
    "category": "engineering",
    "input": {
        "title": "Fixture task",
        "description": "Produce the requested local file.",
        "context": [],
    },
    "gold": {
        "reference_answer": "private-reference",
        "capability_spec": {"required_tools": ["terminal"]},
        "rubric": {"dimensions": [{"id": "fixture"}]},
        "canonical_trajectory": [{"tool": "terminal"}],
    },
    "environment": {"required_services": [], "mcp_servers": ["terminal"]},
    "repro": {"seed": 7},
    "meta": {"source": "fixture", "unknown_native_field": "retained"},
}


def encoded(value=DOCUMENT):
    return json.dumps(value).encode()


def test_private_gold_is_preserved_without_entering_agent_input_or_public_descriptor():
    data = encoded()
    case = parse_case(data, suffix=".json")
    assert case.source_digest == "sha256:" + hashlib.sha256(data).hexdigest()
    assert case.native_data() == DOCUMENT
    assert case.agent_input() == {
        "title": "Fixture task",
        "prompt": "Produce the requested local file.",
    }
    descriptor = case.descriptor("revision-1", relative_path="example/case.json")
    assert descriptor["native_ref"] == {
        "suite": "example",
        "case_id": "case-1",
        "path": "example/case.json",
    }
    assert "private-reference" not in json.dumps(case.agent_input())
    assert "private-reference" not in json.dumps(descriptor)
    copy = case.native_data()
    copy["gold"]["reference_answer"] = "changed"
    assert case.native_data()["gold"]["reference_answer"] == "private-reference"
    assert "private-reference" not in repr(case)
    assert "app" not in sys.modules


@pytest.mark.parametrize(
    "data,suffix",
    [
        (b'{"id":"one","id":"two"}', ".json"),
        (b"id: one\nid: two\n", ".yaml"),
        (b'key: !!python/object/apply:os.system ["false"]', ".yaml"),
        (b"{}", ".json"),
        (b"[]", ".json"),
        (b'{"x":NaN}', ".json"),
        (b"x" * (1024**2 + 1), ".yaml"),
        (b'{"id":"one"}', ".txt"),
    ],
)
def test_invalid_unsafe_or_ambiguous_documents_are_rejected(data, suffix):
    with pytest.raises(CaseFormatError):
        parse_case(data, suffix=suffix)


def test_external_commands_and_timestamp_placeholders_are_preserved_verbatim():
    value = json.loads(encoded())
    value["gold"]["external_eval"] = {
        "preprocess_command": "python ${TOOLATHLON_GYM_PATH}/prepare.py --launch_time ${LAUNCH_TIME}",
        "eval_command": "python ${TOOLATHLON_GYM_PATH}/evaluate.py --launch_time ${LAUNCH_TIME}",
        "groundtruth_path": "tasks/fixture/groundtruth_workspace",
    }
    case = parse_case(encoded(value), suffix=".json")
    assert case.external_eval == value["gold"]["external_eval"]
    assert case.descriptor("revision-1")["metadata"]["has_external_eval"]
    assert "groundtruth_workspace" not in json.dumps(case.agent_input())


@pytest.mark.parametrize(
    "suite", ["..", ".", "../example", "/example", "example/child", "example\\child", ""]
)
def test_suite_selection_cannot_escape_its_installed_root(tmp_path, suite):
    with pytest.raises(CaseFormatError):
        load_suite(tmp_path, suite)


def test_suite_order_duplicates_and_file_links_are_checked(tmp_path):
    root = tmp_path / "example"
    root.mkdir()
    value = json.loads(encoded())
    value["id"] = "case-2"
    (root / "b.json").write_bytes(encoded(value))
    (root / "a.json").write_bytes(encoded())
    assert [case.id for case in load_suite(tmp_path, "example")] == ["case-1", "case-2"]
    (root / "b.json").write_bytes(encoded())
    with pytest.raises(CaseFormatError, match="identity conflicts"):
        load_suite(tmp_path, "example")
    (root / "b.json").unlink()
    (root / "b.json").symlink_to(root / "a.json")
    with pytest.raises(CaseFormatError, match="regular"):
        load_suite(tmp_path, "example")


def test_existing_committed_case_corpus_remains_readable():
    if "CASE_CORPUS_PATH" not in os.environ:
        pytest.skip("Set CASE_CORPUS_PATH to check the installed legacy corpus")
    root = Path(os.environ["CASE_CORPUS_PATH"])
    suites = sorted(path.name for path in root.iterdir() if path.is_dir())
    count, external = 0, 0
    for suite in suites:
        for case in load_suite(root, suite):
            assert case.suite == suite and case.agent_input()["title"]
            assert case.native_data().get("gold", {}) == json.loads(
                json.dumps(case.native_data().get("gold", {}))
            )
            external += case.external_eval is not None
            count += 1
    assert count > 100 and external > 100


def test_frozen_case_resolution_rejects_changed_source_and_foreign_identity(tmp_path):
    from evalhive_cases.format import resolve_case

    directory = tmp_path / "example"
    directory.mkdir()
    path = directory / "case.json"
    path.write_bytes(encoded())
    descriptor = parse_case(encoded(), suffix=".json").descriptor("revision-1")
    assert resolve_case(tmp_path, descriptor).id == "case-1"
    changed = json.loads(encoded())
    changed["input"]["title"] = "Changed fixture title"
    path.write_bytes(encoded(changed))
    with pytest.raises(CaseFormatError, match="frozen descriptor"):
        resolve_case(tmp_path, descriptor)
    descriptor["native_ref"]["suite"] = "../foreign"
    with pytest.raises(CaseFormatError):
        resolve_case(tmp_path, descriptor)


def test_yaml_aliases_cannot_expand_an_unbounded_document():
    with pytest.raises(CaseFormatError, match="aliases"):
        parse_case(b"value: &v [one, two]\ncopy: *v\n")


def test_installed_locator_selects_only_its_frozen_case_and_checks_changed_bytes(tmp_path):
    from evalhive_cases.format import load_suite, resolve_case

    directory = tmp_path / "example"
    directory.mkdir()
    target = directory / "case.json"
    target.write_bytes(encoded())
    descriptor = load_suite(tmp_path, "example")[0].descriptor("revision-1")
    assert descriptor["native_ref"]["path"] == "example/case.json"
    (directory / "unselected.json").write_bytes(b"not a selected case")
    assert resolve_case(tmp_path, descriptor).id == "case-1"
    changed = json.loads(encoded())
    changed["input"]["title"] = "Changed fixture title"
    target.write_bytes(encoded(changed))
    with pytest.raises(CaseFormatError, match="frozen descriptor"):
        resolve_case(tmp_path, descriptor)


@pytest.mark.parametrize(
    "locator",
    [
        "../outside.json",
        "/example/case.json",
        "other/case.json",
        "example//case.json",
        "example/./case.json",
        "example/../case.json",
        "example/case.py",
        None,
    ],
)
def test_installed_case_locator_cannot_select_foreign_or_ambiguous_files(tmp_path, locator):
    from evalhive_cases.format import resolve_case

    (tmp_path / "example").mkdir()
    descriptor = parse_case(encoded(), suffix=".json").descriptor("revision-1")
    descriptor["native_ref"]["path"] = locator
    with pytest.raises(CaseFormatError):
        resolve_case(tmp_path, descriptor)


def test_installed_case_locator_rejects_links_and_fifo_without_opening_a_stream(tmp_path):
    import os

    from evalhive_cases.format import resolve_case

    directory = tmp_path / "example"
    directory.mkdir()
    target = directory / "case.json"
    descriptor = parse_case(encoded(), suffix=".json").descriptor(
        "revision-1", relative_path="example/case.json"
    )
    (tmp_path / "outside.json").write_bytes(encoded())
    target.symlink_to(tmp_path / "outside.json")
    with pytest.raises(CaseFormatError):
        resolve_case(tmp_path, descriptor)
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(CaseFormatError, match="regular file"):
        resolve_case(tmp_path, descriptor)


@pytest.mark.parametrize(
    "field,value", [("reference_answer", 7), ("capability_spec", []), ("external_eval", {})]
)
def test_gold_fields_keep_their_native_schema_types(field, value):
    case = json.loads(encoded())
    case["gold"][field] = value
    with pytest.raises(CaseFormatError, match="schema"):
        parse_case(encoded(case), suffix=".json")
