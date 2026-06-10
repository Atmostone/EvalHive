"""Unit tests for the Tool & MCP Registry pure helpers (SPA-41).

No DB, no network. Covers the migration dedup (builtin/MCP dedup, name-collision
suffixing, per-template ordering), the spawn-time override resolution (disable wins,
enable appends), materialization shapes, and secret masking.
"""

from types import SimpleNamespace

from app.registry.resolver import _apply_override, _materialize
from app.registry.service import _mask_secret, dedupe_for_migration, mask_secrets


# --------------------------------------------------------------------------- #
# Migration dedup
# --------------------------------------------------------------------------- #
def test_dedupe_builtins_distinct_by_name():
    tmpls = [
        {"id": "t1", "tools": ["bash", "grep"], "mcp_servers": []},
        {"id": "t2", "tools": ["bash"], "mcp_servers": []},
    ]
    entries, per = dedupe_for_migration(tmpls)
    builtins = [e for e in entries if e["kind"] == "builtin"]
    assert sorted(e["name"] for e in builtins) == ["bash", "grep"]  # bash deduped
    # t2 references only the shared bash entry.
    bash_key = next(e["key"] for e in builtins if e["name"] == "bash")
    assert per["t2"] == [bash_key]
    assert set(per["t1"]) == {e["key"] for e in builtins}


def test_dedupe_mcp_by_config_and_name_collision():
    same = {"name": "gh", "command": "x", "args": ["a"], "env": {"T": "s"}}
    other = {"name": "gh", "command": "y", "args": [], "env": {}}
    tmpls = [
        {"id": "t1", "tools": [], "mcp_servers": [same]},
        {"id": "t2", "tools": [], "mcp_servers": [dict(same)]},  # identical → reused
        {"id": "t3", "tools": [], "mcp_servers": [other]},  # different config → new + suffix
    ]
    entries, per = dedupe_for_migration(tmpls)
    mcps = [e for e in entries if e["kind"] == "mcp"]
    assert len(mcps) == 2
    names = sorted(e["name"] for e in mcps)
    assert names == ["gh", "gh-2"]  # name collision suffixed
    assert per["t1"] == per["t2"]  # same config → same single entry
    assert per["t3"] != per["t1"]
    # secrets and config split correctly.
    same_entry = next(e for e in mcps if e["secrets"] == {"T": "s"})
    assert same_entry["config"] == {"command": "x", "args": ["a"]}


def test_dedupe_orders_builtins_before_mcp():
    tmpls = [{"id": "t1", "tools": ["bash"], "mcp_servers": [{"name": "gh", "command": "x"}]}]
    _, per = dedupe_for_migration(tmpls)
    keys = per["t1"]
    assert keys[0].startswith("b:") and keys[1].startswith("m:")


def test_dedupe_empty():
    entries, per = dedupe_for_migration([{"id": "t1", "tools": [], "mcp_servers": []}])
    assert entries == []
    assert per == {"t1": []}


# --------------------------------------------------------------------------- #
# Override resolution
# --------------------------------------------------------------------------- #
def test_override_disable_wins_over_enable():
    # 'd' is in both enable and disable → stays out (finest restriction wins).
    assert _apply_override(["a", "b", "c"], {"enable": ["c", "d"], "disable": ["b", "d"]}) == ["a", "c"]


def test_override_enable_appends_new():
    assert _apply_override(["a"], {"enable": ["b", "a"]}) == ["a", "b"]  # 'a' not duplicated


def test_override_none_is_identity():
    assert _apply_override(["a", "b"], None) == ["a", "b"]
    assert _apply_override(["a", "b"], {}) == ["a", "b"]


# --------------------------------------------------------------------------- #
# Materialization + masking
# --------------------------------------------------------------------------- #
def test_materialize_builtin_is_name():
    assert _materialize(SimpleNamespace(kind="builtin", name="bash")) == "bash"


def test_materialize_mcp_is_server_dict():
    entry = SimpleNamespace(
        kind="mcp", name="gh", config={"command": "npx", "args": ["-y"], "url": "http://x"},
        secrets={"TOKEN": "v"},
    )
    assert _materialize(entry) == {
        "name": "gh",
        "command": "npx",
        "args": ["-y"],
        "env": {"TOKEN": "v"},
        "url": "http://x",
    }


def test_mask_secret():
    assert _mask_secret("secret123") == "***t123"
    assert _mask_secret("ab") == "***"
    assert _mask_secret("") == "***"
    assert mask_secrets({"K": "longvalue", "S": "x"}) == {"K": "***alue", "S": "***"}
