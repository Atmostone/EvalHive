"""Recorded conditions ignore transport identity and preserve semantic changes."""

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

from app.harnesses.provenance import condition_fingerprints, execution_context
from app.quality.reproducibility import assemble_snapshot


def attempt(identity="first"):
    return SimpleNamespace(
        connector_id="pi", connector_version="0.1.0", protocol_version="1.0",
        manifest={"requirements": {"harness_version": "1.5.0"}}, prepared_at=datetime(2026, 11, 4),
        run_spec={
            "task": {"title": "Fixture", "prompt": "Exact model input"},
            "config": {"temperature": 0}, "model": {"name": "fixture", "endpoint": "http://fixture/v1", "credential_ref": "private-model"},
            "instructions": ["Original role"], "limits": {"max_turns": 2},
            "metadata": {"image_id": "sha256:" + "1" * 64, "execution_mode": "direct"},
            "environment": {"id": "workspace", "resources": [
                {"id": "evalhive_knowledge", "kind": "mcp", "config": {"transport": "http", "url": f"http://api/attempts/{identity}/knowledge/mcp"}},
                {"id": "memory", "kind": "memory", "config": {"mode": "off"}},
            ]},
        },
    )


def test_condition_identity_ignores_attempt_urls_but_detects_settings_image_and_model_changes():
    first, second = attempt(), attempt("second")
    assert condition_fingerprints(first) == condition_fingerprints(second)
    for path, value in [("config", {"temperature": 1}), ("model", {"name": "different"}),
                        ("instructions", ["Different role"]), ("limits", {"max_turns": 3})]:
        changed = deepcopy(first)
        changed.run_spec[path] = value
        assert condition_fingerprints(changed) != condition_fingerprints(first)
    changed = deepcopy(first)
    changed.run_spec["metadata"]["image_id"] = "sha256:" + "2" * 64
    assert condition_fingerprints(changed) != condition_fingerprints(first)


def test_snapshot_captures_applied_harness_and_names_remote_state_gaps():
    task = SimpleNamespace(title="UI label", description="Fixture", model_used="fixture", run_config={})
    execution = execution_context(attempt())
    snap = assemble_snapshot(task, execution)
    assert snap["schema_version"] == 2
    assert snap["content"]["harness"]["config"] == {"temperature": 0}
    assert snap["content"]["harness"]["configuration_state"] == "prepared"
    assert snap["determinism"]["task_input"]["title"] == "Fixture"
    assert "harness.applied_config" in snap["manifest"]["captured"]
    assert "provider_model_revision" in snap["manifest"]["missing"]
    assert "private-model" not in str(snap)
    changed = deepcopy(execution)
    changed["harness"]["prepared_at"] = "2027-01-01"
    assert assemble_snapshot(task, changed)["fingerprint"] == snap["fingerprint"]
    changed["harness"]["config"]["temperature"] = 1
    assert assemble_snapshot(task, changed)["fingerprint"] != snap["fingerprint"]


def test_matrix_identity_and_child_configuration_include_harness_settings():
    from app.quality.experiments import child_run_config, expand_matrix

    base = {"template_id": "role", "harness_profile_id": "profile-a", "harness_config": {"temperature": 0}}
    configs = expand_matrix([base, {**base, "harness_profile_id": "profile-b"},
                             {**base, "harness_config": {"temperature": 1}}])
    assert len(configs) == 3 and len({row["fingerprint"] for row in configs}) == 3
    rc = child_run_config(SimpleNamespace(id="experiment"), configs[0], case_key="fixture", run_index=0)
    assert rc["harness_profile_id"] == "profile-a"
    assert rc["harness_config"] == {"temperature": 0}
    assert rc["execution_mode"] == "direct"
