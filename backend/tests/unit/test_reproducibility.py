"""Unit tests for the Reproducibility Snapshot engine (E-20).

Pure assemble / fingerprint / diff — no DB. ``assemble_snapshot`` maps a task + its
Data Lake ``execution`` section into an experiment_snapshot: large text is hashed
into the fingerprinted ``determinism`` block and kept raw-capped under ``content``,
with an honest ``manifest`` of captured vs. missing fields. The fingerprint is
deterministic over the run-defining fields only (volatile/raw fields excluded).
"""

from types import SimpleNamespace

from app.quality.reproducibility import (
    assemble_snapshot,
    diff_snapshots,
    snapshot_fingerprint,
)


def _task(**kw):
    base = dict(
        run_config=None,
        model_used="gpt-4o",
        title="Build a CLI",
        description="make a thing",
        reference_answer="the answer",
        canonical_trajectory=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _execution(**kw):
    base = dict(
        template_id="11111111-1111-1111-1111-111111111111",
        template_name="Coder",
        model_api_name="gpt-4o",
        soul_md="You are a coder.",
        tools=["file_write", "bash"],
        mcp_servers=["srv-b", "srv-a"],
        memory_context="entity: x",
        flat_memory={"rules_md": "be nice", "memory_md": ""},
    )
    base.update(kw)
    return base


def test_assemble_hashes_in_determinism_raw_in_content():
    snap = assemble_snapshot(_task(), _execution())
    det = snap["determinism"]
    assert det["model_api_name"] == "gpt-4o"
    assert det["template_name"] == "Coder"
    # tools / mcp_servers are sorted (set semantics for determinism)
    assert det["tools"] == ["bash", "file_write"]
    assert det["mcp_servers"] == ["srv-a", "srv-b"]
    # large text hashed in determinism, kept raw in content
    assert det["soul_md_sha256"] is not None
    assert snap["content"]["soul_md"] == "You are a coder."
    assert det["task_input"]["title"] == "Build a CLI"
    assert det["task_input"]["description_sha256"] is not None
    # tool_versions keyed by name, all null today
    assert det["tool_versions"] == {"bash": None, "file_write": None}
    assert det["rag"]["vector_capture"] == "out_of_scope"
    # fingerprint is filled and recomputable
    assert snap["fingerprint"] == snapshot_fingerprint(snap)


def test_manifest_marks_gaps_without_run_config():
    snap = assemble_snapshot(_task(run_config=None), _execution())
    manifest = snap["manifest"]
    for gap in ("temperature", "seed", "tool_versions", "rag_vectors"):
        assert gap in manifest["missing"]
    assert "model_api_name" in manifest["captured"]
    assert "soul_md" in manifest["captured"]
    # notes are only kept for fields that are actually missing
    assert set(manifest["notes"]).issubset(set(manifest["missing"]))


def test_manifest_captures_temperature_and_seed_from_run_config():
    snap = assemble_snapshot(
        _task(run_config={"temperature": 0.2, "seed": 7}), _execution()
    )
    det = snap["determinism"]
    assert det["temperature"] == 0.2
    assert det["seed"] == 7
    captured = snap["manifest"]["captured"]
    assert "temperature" in captured and "seed" in captured
    assert "temperature" not in snap["manifest"]["missing"]
    assert "seed" not in snap["manifest"]["missing"]


def test_fingerprint_excludes_volatile_and_is_tool_order_insensitive():
    a = assemble_snapshot(_task(), _execution(tools=["bash", "file_write"]))
    b = assemble_snapshot(_task(), _execution(tools=["file_write", "bash"]))
    # permuted tool order (and possibly different captured_at) ⇒ same fingerprint
    assert a["fingerprint"] == b["fingerprint"]


def test_fingerprint_sensitive_to_run_defining_changes():
    base = assemble_snapshot(_task(), _execution())
    diff_model = assemble_snapshot(_task(), _execution(model_api_name="gpt-4o-mini"))
    diff_temp = assemble_snapshot(_task(run_config={"temperature": 0.9}), _execution())
    diff_soul = assemble_snapshot(_task(), _execution(soul_md="different prompt"))
    assert base["fingerprint"] != diff_model["fingerprint"]
    assert base["fingerprint"] != diff_temp["fingerprint"]
    assert base["fingerprint"] != diff_soul["fingerprint"]


def test_diff_reports_changed_and_identical():
    a = assemble_snapshot(_task(), _execution(model_api_name="gpt-4o", tools=["bash"]))
    b = assemble_snapshot(
        _task(run_config={"temperature": 0.7}),
        _execution(model_api_name="gpt-4o-mini", tools=["bash"]),
    )
    d = diff_snapshots(a, b)
    assert d["identical"] is False
    assert d["changed"]["model_api_name"] == {"from": "gpt-4o", "to": "gpt-4o-mini"}
    assert d["changed"]["temperature"] == {"from": None, "to": 0.7}
    assert d["summary"]

    same = diff_snapshots(a, a)
    assert same["identical"] is True
    assert same["changed"] == {} and same["added"] == {} and same["removed"] == {}
