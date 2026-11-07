"""Bounded cleanup proofs for explicitly dedicated external evaluation targets."""

from __future__ import annotations

import re

from .world_rpc import WorldError

MAX_PAGES = 10


class GitHubNamespace:
    def __init__(self, state):
        self.state = state
        self.owner = state.eval_org
        self.endpoint = f"https://api.github.com/orgs/{self.owner}/repos"
        self.params = {"per_page": 100, "type": "all"}
        response = state.session.get(self.endpoint, params=self.params, timeout=10)
        if response.status_code == 404:
            user = state.session.get("https://api.github.com/user", timeout=10)
            if user.status_code != 200 or user.json().get("login") != self.owner:
                raise WorldError("Dedicated GitHub owner could not be confirmed")
            self.endpoint = "https://api.github.com/user/repos"
            self.params = {"per_page": 100, "affiliation": "owner"}
        elif response.status_code != 200:
            raise WorldError("Dedicated GitHub owner could not be inspected")
        if self.repositories():
            raise WorldError("Dedicated GitHub evaluation owner must initially be empty")

    def repositories(self):
        names = set()
        for page in range(1, MAX_PAGES + 1):
            response = self.state.session.get(
                self.endpoint, params={**self.params, "page": page}, timeout=10
            )
            if response.status_code != 200:
                raise WorldError("Dedicated GitHub inventory could not be confirmed")
            rows = response.json()
            if not isinstance(rows, list) or len(rows) > 100:
                raise WorldError("Dedicated GitHub inventory is invalid")
            for row in rows:
                name = row.get("name")
                if (
                    row.get("owner", {}).get("login") != self.owner
                    or not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                    or name in {".", ".."}
                    or name in names
                ):
                    raise WorldError("Dedicated GitHub inventory is inconsistent")
                names.add(name)
            if len(rows) < 100:
                return sorted(names)
        raise WorldError("Dedicated GitHub inventory exceeds its bounded scope")

    def cleanup(self):
        for name in self.repositories():
            response = self.state.session.delete(
                f"https://api.github.com/repos/{self.owner}/{name}", timeout=10
            )
            if response.status_code not in {200, 204, 404}:
                raise WorldError("Dedicated GitHub repository cleanup was not confirmed")
        if self.repositories():
            raise WorldError("Dedicated GitHub evaluation owner is not empty")
        return {"service": "github", "owner": self.owner, "empty": True}


def notion_pages(call, **parameters):
    cursor, seen = None, set()
    rows = []
    for _ in range(MAX_PAGES):
        response = call(**parameters, **({"start_cursor": cursor} if cursor else {}))
        values = response.get("results")
        if not isinstance(values, list) or len(values) > 100:
            raise WorldError("Dedicated Notion inventory is invalid")
        rows.extend(values)
        if response.get("has_more") is False:
            return rows
        cursor = response.get("next_cursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise WorldError("Dedicated Notion inventory cursor is inconsistent")
        seen.add(cursor)
    raise WorldError("Dedicated Notion inventory exceeds its bounded scope")


class NotionNamespace:
    def __init__(self, state):
        self.state = state
        self.client = state.eval_notion_client
        self.hub = state._ensure_eval_parent_page_id()
        if not self.hub or self.children() or self.objects() != {self.hub}:
            raise WorldError("Dedicated Notion evaluation integration must expose one empty hub")

    def objects(self):
        rows = notion_pages(self.client.search, page_size=100)
        identifiers = [row.get("id") for row in rows]
        if any(not isinstance(item, str) or not item for item in identifiers):
            raise WorldError("Dedicated Notion inventory identity is unavailable")
        if len(set(identifiers)) != len(identifiers):
            raise WorldError("Dedicated Notion inventory repeated an object")
        return set(identifiers)

    def children(self):
        return notion_pages(self.client.blocks.children.list, block_id=self.hub, page_size=100)

    def cleanup(self):
        for child in self.children():
            identifier = child.get("id")
            if not isinstance(identifier, str) or not identifier or identifier == self.hub:
                raise WorldError("Dedicated Notion child identity is invalid")
            result = self.client.blocks.delete(block_id=identifier)
            if result.get("archived") is not True and result.get("in_trash") is not True:
                raise WorldError("Dedicated Notion child cleanup was not confirmed")
        if self.children() or self.objects() != {self.hub}:
            raise WorldError("Dedicated Notion evaluation integration is not empty")
        return {"service": "notion", "hub": self.hub, "empty": True}


def prepare(service, state):
    return {"github": GitHubNamespace, "notion": NotionNamespace}[service](state)
