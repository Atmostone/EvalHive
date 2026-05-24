"""Unit tests for the deterministic Trajectory Matcher (E-09).

Covers the three reference forms (list / sequence / DAG), the three metrics
(exact / edit / dag), normalization, and the never-raises contract of
match_trajectory. The DB-backed evaluate_task_trajectory_match is exercised in
the integration tests.
"""

from app.quality.trajectory_match import (
    dag_consistency,
    edit_similarity,
    exact_match,
    extract_tool_sequence,
    match_trajectory,
    parse_reference,
    _topological_order,
)


def _trace(tools):
    """A cleaned-trace-like dict with the given tool steps (plus a reasoning step)."""
    steps = [{"kind": "reasoning", "tool_name": None, "content": "think"}]
    for i, t in enumerate(tools):
        steps.append({"kind": "tool", "tool_name": t, "content": f"out {i}"})
    return {"steps": steps, "stats": {"steps_total": len(steps)}}


# --- extract_tool_sequence ------------------------------------------------


def test_extract_tool_sequence_only_named_tools():
    trace = {
        "steps": [
            {"kind": "tool", "tool_name": "search", "content": "x"},
            {"kind": "reasoning", "tool_name": None, "content": "y"},
            {"kind": "tool", "tool_name": None, "content": "archive chunk lost name"},
            {"kind": "tool", "tool_name": "write_file", "content": "z"},
        ]
    }
    assert extract_tool_sequence(trace) == ["search", "write_file"]


# --- parse_reference ------------------------------------------------------


def test_parse_reference_bare_list():
    ref = parse_reference(["search", "write_file", "run_tests"])
    assert ref["form"] == "sequence"
    assert ref["tools"] == ["search", "write_file", "run_tests"]
    assert ref["linear"] == ["search", "write_file", "run_tests"]
    # edges are node-index pairs (chain): 0->1->2
    assert ref["edges"] == [(0, 1), (1, 2)]
    assert ref["match_mode"] == "edit"


def test_parse_reference_sequence_dict_with_mode():
    ref = parse_reference({"sequence": ["a", "b"], "match_mode": "exact", "match_threshold": 0.8})
    assert ref["form"] == "sequence"
    assert ref["match_mode"] == "exact"
    assert ref["match_threshold"] == 0.8


def test_parse_reference_dag_topo_order():
    canonical = {
        "nodes": [
            {"id": "n1", "tool": "search"},
            {"id": "n2", "tool": "write_file"},
            {"id": "n3", "tool": "run_tests"},
        ],
        "edges": [["n1", "n2"], ["n1", "n3"], ["n2", "n3"]],
        "match_mode": "dag",
    }
    ref = parse_reference(canonical)
    assert ref["form"] == "dag"
    assert ref["match_mode"] == "dag"
    # search before write_file before run_tests is the only valid order here.
    assert ref["linear"] == ["search", "write_file", "run_tests"]
    # edges are node-index pairs (n1=0, n2=1, n3=2)
    assert (0, 1) in ref["edges"] and (1, 2) in ref["edges"]


def test_parse_reference_rejects_garbage():
    for bad in [42, "just a string", {}, {"foo": "bar"}, {"nodes": [{"id": "x"}]}]:
        try:
            parse_reference(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


def test_topological_order_detects_cycle():
    assert _topological_order(["a", "b"], [("a", "b"), ("b", "a")]) is None


# --- metrics --------------------------------------------------------------


def test_exact_match_normalizes_case_and_space():
    assert exact_match(["Search", " write_file "], ["search", "write_file"]) == 1.0
    assert exact_match(["search"], ["search", "write_file"]) == 0.0


def test_edit_similarity_partial_and_perfect():
    assert edit_similarity(["a", "b", "c"], ["a", "b", "c"]) == 1.0
    # one extra call out of a close sequence → high but < 1.0
    r = edit_similarity(["a", "b", "x", "c"], ["a", "b", "c"])
    assert 0.5 < r < 1.0
    assert edit_similarity([], []) == 1.0


def test_dag_consistency_respects_precedence():
    # chain: node0 search -> node1 write_file -> node2 run_tests
    tools = ["search", "write_file", "run_tests"]
    edges = [(0, 1), (1, 2)]
    ok, _ = dag_consistency(["search", "write_file", "run_tests"], tools, edges)
    assert ok == 1.0
    # reordered: write_file before search is not a valid topological order
    bad, note = dag_consistency(["write_file", "search", "run_tests"], tools, edges)
    assert bad == 0.0 and "topological" in note
    # missing a tool: multiset differs
    miss, note2 = dag_consistency(["search", "run_tests"], tools, edges)
    assert miss == 0.0 and "multiset" in note2


def test_dag_consistency_allows_independent_reorder():
    # node1 and node2 both depend only on node0 → either order is valid
    tools = ["a", "b", "c"]
    edges = [(0, 1), (0, 2)]
    ok1, _ = dag_consistency(["a", "b", "c"], tools, edges)
    ok2, _ = dag_consistency(["a", "c", "b"], tools, edges)
    assert ok1 == 1.0 and ok2 == 1.0


def test_dag_consistency_chain_with_repeated_tools():
    # a chain with a repeated tool: only the exact chain order is valid, and the
    # node-instance simulation must accept it (the old first-occurrence heuristic
    # produced false violations here).
    tools = ["bash", "file_read", "file_read", "bash", "file_read"]
    ref = parse_reference(tools)
    ok, _ = dag_consistency(tools, ref["tools"], ref["edges"])
    assert ok == 1.0
    # swapping the trailing bash/file_read breaks the chain order
    bad, _ = dag_consistency(
        ["bash", "file_read", "file_read", "file_read", "bash"], ref["tools"], ref["edges"]
    )
    assert bad == 0.0


# --- match_trajectory -----------------------------------------------------


def test_match_perfect_all_metrics():
    out = match_trajectory(_trace(["search", "write_file", "run_tests"]),
                           ["search", "write_file", "run_tests"])
    assert out["status"] == "scored"
    assert out["metrics"] == {"exact": 1.0, "edit": 1.0, "dag": 1.0}
    assert out["matched"] is True
    assert out["actual_sequence"] == ["search", "write_file", "run_tests"]
    assert out["reference_sequence"] == ["search", "write_file", "run_tests"]


def test_match_extra_call_edit_headline():
    # default mode = edit; one redundant call
    out = match_trajectory(_trace(["search", "search", "write_file"]),
                           ["search", "write_file"])
    assert out["mode"] == "edit"
    assert out["metrics"]["exact"] == 0.0
    assert out["metrics"]["dag"] == 0.0  # multiset differs (extra search)
    assert 0.5 < out["metrics"]["edit"] < 1.0


def test_match_reorder_dag_mode_fails_but_edit_partial():
    out = match_trajectory(
        _trace(["write_file", "search"]),
        {"sequence": ["search", "write_file"], "match_mode": "dag"},
    )
    assert out["mode"] == "dag"
    assert out["metrics"]["dag"] == 0.0  # precedence violated
    assert out["matched"] is False
    # same multiset, just reordered → edit ratio is non-trivial
    assert out["metrics"]["edit"] > 0.0


def test_match_dag_independent_branches_matches():
    canonical = {
        "nodes": [{"id": "n1", "tool": "a"}, {"id": "n2", "tool": "b"}, {"id": "n3", "tool": "c"}],
        "edges": [["n1", "n2"], ["n1", "n3"]],
        "match_mode": "dag",
    }
    out = match_trajectory(_trace(["a", "c", "b"]), canonical)
    assert out["metrics"]["dag"] == 1.0
    assert out["matched"] is True


def test_match_bad_reference_is_error_not_raise():
    out = match_trajectory(_trace(["a"]), {"foo": "bar"})
    assert out["status"] == "error"
    assert "error" in out
