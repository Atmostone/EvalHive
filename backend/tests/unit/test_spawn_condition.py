"""The spawn-time condition fingerprint (SPA-84, pure).

Recording what a run executed under is only meaningful if the hash covers
exactly the things that change behaviour and nothing else. Two failure modes
matter: leaking a secret into a value the API returns unauthenticated, and
moving on per-run noise, which would make every single run look like its own
condition and the signal useless.
"""

from app.orchestrator.engine import spawn_condition_fingerprint
from app.plugins.runtime import AgentSpec

MODEL = "vendor/model-x"


def _spec(**over) -> AgentSpec:
    base = dict(
        task_id="11111111-1111-1111-1111-111111111111",
        task_description="do the thing",
        template_name="Bench",
        template_id="22222222-2222-2222-2222-222222222222",
        soul_md="# soul",
        tools=["bash", "file_read"],
        mcp_servers=[{"name": "toolathlon-notion", "env": {"TOKEN": "secret-a"}}],
        env={"OPENAI_API_KEY": "sk-secret", "LLM_MODEL": MODEL},
        resource_limits={"max_ram": "2g", "max_cpu": 2},
        workspace_id="33333333-3333-3333-3333-333333333333",
        agent_token="tok-a",
    )
    base.update(over)
    return AgentSpec(**base)


def _fp(**over) -> str:
    return spawn_condition_fingerprint(_spec(**over), over.pop("_model", MODEL))


def test_identical_specs_hash_identically():
    assert _fp() == _fp()


def test_tool_and_server_order_does_not_matter():
    """Resolution order is not guaranteed; the condition is the same set."""
    assert _fp(tools=["file_read", "bash"]) == _fp()


def test_the_prompt_changes_the_condition():
    assert _fp(soul_md="# a different prompt") != _fp()


def test_the_model_changes_the_condition():
    assert spawn_condition_fingerprint(_spec(), "vendor/other-model") != _fp()


def test_the_resolved_tool_set_changes_the_condition():
    """The claim-time resolution could not see this at all — tools are resolved
    from the registry at spawn."""
    assert _fp(tools=["bash"]) != _fp()
    assert _fp(mcp_servers=[]) != _fp()


def test_a_rebuilt_image_under_the_same_tag_changes_the_condition():
    """The point of hashing the resolved id rather than the tag: rebuilding
    spawnhive-agent:latest has moved measured pass rates in this project, and the
    tag says nothing about it. Note spec.image is None on the default path."""
    before = spawn_condition_fingerprint(_spec(), MODEL, "sha256:aaa")
    after = spawn_condition_fingerprint(_spec(), MODEL, "sha256:bbb")
    assert before != after
    assert spawn_condition_fingerprint(_spec(), MODEL, "sha256:aaa") == before


def test_an_unknown_image_id_is_not_treated_as_unchanged():
    """A runtime that cannot answer reports None — missing evidence must not
    collide with a real id."""
    unknown = spawn_condition_fingerprint(_spec(), MODEL, None)
    assert unknown != spawn_condition_fingerprint(_spec(), MODEL, "sha256:aaa")


def test_resource_limits_change_the_condition():
    assert _fp(resource_limits={"max_ram": "8g", "max_cpu": 2}) != _fp()


def test_per_run_noise_does_not_change_the_condition():
    """Otherwise every run is its own condition and the comparison says nothing."""
    assert _fp(task_id="99999999-9999-9999-9999-999999999999") == _fp()
    assert _fp(task_description="a different case") == _fp()
    assert _fp(agent_token="tok-b") == _fp()
    assert _fp(memory_context="retrieved chunks for this run") == _fp()
    assert _fp(network_mode="container:tlpre-deadbeef") == _fp()


def test_credentials_never_reach_the_hash_input():
    """GET /api/experiments/{id} returns configurations verbatim and is not
    role-gated, so anything hashed here must be safe to expose."""
    assert _fp(env={"OPENAI_API_KEY": "sk-rotated", "LLM_MODEL": MODEL}) == _fp()
    assert (
        _fp(mcp_servers=[{"name": "toolathlon-notion", "env": {"TOKEN": "secret-b"}}])
        == _fp()
    )


def test_a_renamed_server_is_a_different_condition():
    assert _fp(mcp_servers=[{"name": "toolathlon-gcal", "env": {}}]) != _fp()
