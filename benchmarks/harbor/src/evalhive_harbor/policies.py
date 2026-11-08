"""Resolve native role policies without starting Harbor's own agent runner."""

from harbor.models.task.config import NetworkMode
from harbor.models.task.verifier_mode import resolve_task_verifier_mode
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, VerifierConfig
from harbor.trial.network_policy import resolve_trial_network_plan


def network_plan(task):
    return resolve_trial_network_plan(
        task.config,
        AgentConfig(),
        EnvironmentConfig(),
        VerifierConfig(),
        None,
        verifier_mode=resolve_task_verifier_mode(task.config),
    )


def network_requirements(task):
    plan = network_plan(task)
    policies = [plan.agent_env_baseline, plan.agent_phase, plan.verifier_phase]
    if plan.verifier_env_baseline is not None:
        policies.append(plan.verifier_env_baseline)
    required = set()
    if any(policy.network_mode == NetworkMode.ALLOWLIST for policy in policies):
        required.add("network.allowlist")
    if plan.agent_phase.network_mode != NetworkMode.PUBLIC:
        required.add("host.model_gateway_v1")
    if (
        plan.agent_env_baseline != plan.agent_phase
        or plan.verifier_phase_baseline != plan.verifier_phase
    ):
        required.add("network.dynamic_policy")
    return sorted(required)
