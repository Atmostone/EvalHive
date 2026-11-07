"""External cleanup never claims unrelated objects or hides uncertain mutations."""

from types import SimpleNamespace

import pytest
from evalhive_mcpmark.external_namespace import GitHubNamespace, NotionNamespace, notion_pages
from evalhive_mcpmark.world_rpc import WorldError
from evalhive_mcpmark.world_service import NativeService


def response(rows, status=200):
    return SimpleNamespace(status_code=status, json=lambda: rows)


class GitHubSession:
    def __init__(self, names=()):
        self.names, self.deleted = set(names), []

    def get(self, url, **kwargs):
        assert kwargs["timeout"] == 10
        return response([{"name": name, "owner": {"login": "dedicated"}} for name in self.names])

    def delete(self, url, **kwargs):
        name = url.rsplit("/", 1)[1]
        assert url.startswith("https://api.github.com/repos/dedicated/")
        self.names.remove(name)
        self.deleted.append(name)
        return response({}, 204)


def test_github_rejects_existing_data_before_any_mutation():
    session = GitHubSession(["existing"])
    with pytest.raises(WorldError, match="initially be empty"):
        GitHubNamespace(SimpleNamespace(eval_org="dedicated", session=session))
    assert session.deleted == []


def test_github_sweeps_extra_agent_created_repositories_and_confirms_absence():
    session = GitHubSession()
    namespace = GitHubNamespace(SimpleNamespace(eval_org="dedicated", session=session))
    session.names.update({"native-task", "agent-extra"})
    assert namespace.cleanup() == {"service": "github", "owner": "dedicated", "empty": True}
    assert sorted(session.deleted) == ["agent-extra", "native-task"]
    assert namespace.cleanup()["empty"] is True


def test_github_rejects_foreign_owner_even_if_api_returns_it():
    session = GitHubSession()
    namespace = GitHubNamespace(SimpleNamespace(eval_org="dedicated", session=session))
    session.get = lambda *a, **k: response([{"name": "foreign", "owner": {"login": "other"}}])
    with pytest.raises(WorldError, match="inconsistent"):
        namespace.cleanup()
    assert not session.deleted


def test_notion_repeated_cursor_cannot_create_an_unbounded_inventory():
    with pytest.raises(WorldError, match="cursor"):
        notion_pages(lambda **kw: {"results": [], "has_more": True, "next_cursor": "same"})


def test_notion_only_cleans_new_children_of_the_initially_empty_hub():
    rows, objects, deleted = [], {"hub"}, []

    def delete(*, block_id):
        deleted.append(block_id)
        rows[:] = [row for row in rows if row["id"] != block_id]
        objects.remove(block_id)
        return {"archived": True}

    client = SimpleNamespace(
        search=lambda **kw: {"results": [{"id": key} for key in objects], "has_more": False},
        blocks=SimpleNamespace(
            children=SimpleNamespace(list=lambda **kw: {"results": list(rows), "has_more": False}),
            delete=delete,
        ),
    )
    namespace = NotionNamespace(
        SimpleNamespace(eval_notion_client=client, _ensure_eval_parent_page_id=lambda: "hub")
    )
    rows.append({"id": "new-page"})
    objects.add("new-page")
    assert namespace.cleanup() == {"service": "notion", "hub": "hub", "empty": True}
    assert deleted == ["new-page"]
    objects.add("escaped-page")
    with pytest.raises(WorldError, match="not empty"):
        namespace.cleanup()


@pytest.mark.parametrize(
    "ready,success,complete", [(False, True, True), (True, False, True), (True, True, False)]
)
def test_external_cleanup_cannot_acknowledge_incomplete_mutation_evidence(
    tmp_path, ready, success, complete
):
    service = NativeService(None, records_root=tmp_path)
    service.state = SimpleNamespace(clean_up=lambda task: True)
    service.external_namespace = SimpleNamespace(cleanup=lambda: {"empty": True})
    service.external_mutations_started = True
    service.ready = ready
    service.captured = {"agent_success": success, "tool_effects_complete": complete}
    with pytest.raises(WorldError, match="complete mutation evidence"):
        service("cleanup", {})
    assert not service.closed
    assert '"cleanup_confirmed":false' in (tmp_path / "report/external-cleanup.json").read_text()


def test_external_cleanup_acknowledges_complete_evidence_once(tmp_path):
    service = NativeService(None, records_root=tmp_path)
    calls = []
    service.state = SimpleNamespace(clean_up=lambda task: calls.append("native") or True)
    service.external_namespace = SimpleNamespace(cleanup=lambda: {"empty": True})
    service.external_mutations_started, service.ready = True, True
    service.captured = {"agent_success": True, "tool_effects_complete": True}
    assert service("cleanup", {}) == service("cleanup", {}) == {"cleaned": True}
    assert calls == ["native"]
