"""Restore and grade a frozen invocation using the shared phase supervisor."""

from copy import deepcopy

from evalhive_benchmark import ProtocolError
from evalhive_benchmark.peer import identity

from app.benchmarks.driver import PhaseDriver


class RegradeDriver(PhaseDriver):
    purpose = "evaluation"

    def __init__(self, *args, **kwargs):
        if kwargs.get("agent_control") is not None:
            raise ValueError("Regrade cannot attach an agent controller")
        super().__init__(*args, **kwargs)
        if self.owner.control_id is None:
            raise ValueError("Regrade requires an independent evaluator controller")
        self.source_inputs = None

    async def _resolve_spec(self, spec):
        inputs = await self.store.launch_inputs(self.owner)
        if any(
            self.host.manifest.get(key) != inputs["evaluator"].get(key)
            for key in ("id", "version", "protocol_version", "image", "capabilities")
        ):
            raise ProtocolError(
                "EVALUATOR_MISMATCH", "Worker differs from the retained evaluator", "configuration"
            )
        self.source_inputs = inputs
        return deepcopy(inputs["spec"])

    def _check_effective_config(self, spec, effective):
        if identity(spec["config"]) != identity(effective):
            raise ProtocolError(
                "EVALUATION_CONFLICT",
                "Validation changed the frozen evaluator configuration",
                "configuration",
            )

    def _scoped(self, prepared=None, execution=None, capture=None):
        params = super()._scoped(prepared, None, capture)
        params["payload"]["evaluation_id"] = str(self.owner.control_id)
        if self.source_inputs is not None:
            params["payload"]["source_evaluation_id"] = self.source_inputs["source_evaluation_id"]
        return params

    async def _pipeline(self, spec, *, private_inputs, extra_demands, evaluate):
        _, channel = await self._prepare(
            spec, private_inputs=private_inputs, extra_demands=extra_demands
        )
        await self._wait(self.store.transition(self.owner, "ready", "capturing"))
        capture = await self._capture(channel)
        await self._wait(self.store.transition(self.owner, "capturing", "evaluating"))
        await self._grade(channel, capture, self.owner.control_id)

    async def run(self, *, private_inputs=None, extra_demands=()):
        result = await super().run(None, private_inputs=private_inputs, extra_demands=extra_demands)
        return {**result, "evaluation_id": str(self.owner.control_id)}
