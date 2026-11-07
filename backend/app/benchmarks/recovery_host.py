"""A recovery context deliberately has no worker, package or agent process."""

import uuid
from types import SimpleNamespace

from app.benchmarks.retained_inputs import RetainedInputs


class RecoveryHost:
    channel = None
    agent_gateway = None

    def __init__(self, store, owner, runtime, provider_id, evidence):
        self.store, self.owner = store, owner
        self.host_actor = uuid.uuid5(owner.controller_id, "host.actor")
        self.assets = SimpleNamespace(runtime=runtime, provider_id=provider_id)
        self.retained_inputs = RetainedInputs(store, owner, runtime.policy, evidence)

    async def release_inputs(self):
        await self.retained_inputs.release()

    async def detach(self, *, grace=3):
        """There is no attached transport to close in this recovery context."""
