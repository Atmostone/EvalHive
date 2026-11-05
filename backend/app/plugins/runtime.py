"""Compatibility management API backed by the harness-neutral process runtime.

Execution and control commands belong to attempts, not to this facade. The
historical import path remains available to the existing management endpoints.
"""

import asyncio

from app.runtime import get_process_runtime

_runtime = None


class RuntimeManagement:
    def list_active(self, workspace_id=None):
        return get_process_runtime().list_active(workspace_id)

    def _scope(self, container_id, workspace_id):
        if workspace_id is not None:
            return str(workspace_id)
        matches = [row for row in self.list_active() if row["container_id"] == container_id]
        return matches[0]["workspace_id"] if len(matches) == 1 else None

    def stats(self, container_id, workspace_id=None):
        scope = self._scope(container_id, workspace_id)
        return get_process_runtime().inspect(container_id, scope) if scope else None

    def kill(self, container_id, workspace_id=None):
        scope = self._scope(container_id, workspace_id)
        return bool(scope and get_process_runtime().stop(container_id, scope, grace_seconds=0))

    def kill_all(self, workspace_id=None):
        return sum(self.kill(row["container_id"], row["workspace_id"]) for row in self.list_active(workspace_id))

    async def health(self, container_id):
        return await asyncio.to_thread(self.stats, container_id)


def get_agent_runtime():
    return _runtime if _runtime is not None else RuntimeManagement()


def set_agent_runtime(runtime):
    global _runtime
    _runtime = runtime
