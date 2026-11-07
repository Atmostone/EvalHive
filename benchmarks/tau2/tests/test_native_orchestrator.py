"""The real orchestrator owns effects and the real evaluator owns reward basis."""

import asyncio
import os
import time

import pytest
from evalhive_tau2.agent import ExternalSession, create_agent, thread_exchange
from evalhive_tau2.native import load_domain
from test_agent import Harness


@pytest.mark.asyncio
async def test_scripted_harness_matches_native_trajectory_reward_and_termination():
    root = os.environ.get("EVALHIVE_NATIVE_TAU2_DATA")
    if not root:
        pytest.skip("Requires the pinned native tau2 wheel and private baseline data")
    domain = load_domain(root, "airline")
    from tau2.agent.base_agent import HalfDuplexAgent
    from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
    from tau2.data_model.tasks import Task
    from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
    from tau2.orchestrator.orchestrator import Orchestrator
    from tau2.user.user_simulator_base import HalfDuplexUser

    class User(HalfDuplexUser):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            return UserMessage(
                role="user", content="Calculate" if state == 0 else "###STOP###"
            ), state + 1

    class ReferenceAgent(HalfDuplexAgent):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            if state == 0:
                output = AssistantMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id="a", name="calculate", arguments={"expression": "2+2"}),
                        ToolCall(id="b", name="calculate", arguments={"expression": "3+3"}),
                    ],
                )
            else:
                output = AssistantMessage(role="assistant", content="Done")
            return output, state + 1

    task = Task(
        id="engineering-calculator",
        user_scenario={"instructions": "Ask for arithmetic"},
        evaluation_criteria={
            "actions": [
                {"action_id": "reference", "name": "calculate", "arguments": {"expression": "9+9"}}
            ],
            "communicate_info": ["Done"],
            "reward_basis": ["DB", "COMMUNICATE"],
        },
    )
    environment = domain.get_environment()
    reference = Orchestrator(
        domain="airline",
        agent=ReferenceAgent(environment.get_tools(), environment.get_policy()),
        user=User(),
        environment=environment,
        task=task,
        max_steps=20,
        timeout=20,
    ).run()
    harness = Harness()
    harness.initial_history = [{"role": "assistant", "content": "Hi! How can I help you today?"}]
    session = ExternalSession(
        harness,
        {"trial_id": "trial"},
        deadline=time.monotonic() + 30,
        schemas={"calculate": "a" * 64},
    )
    environment = domain.get_environment()
    agent = create_agent(
        root,
        "airline",
        environment.get_tools(),
        environment.get_policy(),
        thread_exchange(session, asyncio.get_running_loop()),
    )
    orchestrator = Orchestrator(
        domain="airline",
        agent=agent,
        user=User(),
        environment=environment,
        task=task,
        max_steps=20,
        timeout=20,
    )
    actual = await asyncio.to_thread(orchestrator.run)
    assert actual.termination_reason == reference.termination_reason
    assert actual.termination_reason.value == "user_stop"
    for simulation in (actual, reference):
        reward = evaluate_simulation(simulation, task, EvaluationType.ALL, False, "airline")
        # Different arithmetic calls preserve the DB state. Gold actions are
        # not a hard call-by-call requirement under the native DB basis.
        assert reward.reward == 1.0
    assert {row["call_id"] for row in harness.results} == {"a", "b"}
    assert len([message for message in actual.messages if message.role == "tool"]) == 2
    assert (await session.close())["status"] == "success"
