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


@pytest.mark.asyncio
async def test_native_telecom_user_effects_and_agent_tool_errors_preserve_reference_trajectory():
    root = os.environ.get("EVALHIVE_NATIVE_TAU2_DATA")
    if not root:
        pytest.skip("Requires the pinned native tau2 wheel and private baseline data")
    domain = load_domain(root, "telecom")
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
            if state == 0:
                output = UserMessage(
                    role="user",
                    tool_calls=[
                        ToolCall(
                            id="user-device",
                            name="turn_airplane_mode_on",
                            arguments={},
                            requestor="user",
                        )
                    ],
                )
            else:
                output = UserMessage(
                    role="user", content="Check my account" if state == 1 else "###STOP###"
                )
            return output, state + 1

    class ReferenceAgent(HalfDuplexAgent):
        def get_init_state(self, message_history=None):
            return 0

        def generate_next_message(self, message, state):
            if state == 0:
                output = AssistantMessage(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id=name,
                            name="get_customer_by_phone",
                            arguments={"phone_number": "missing-" + name},
                        )
                        for name in ("a", "b")
                    ],
                )
            else:
                output = AssistantMessage(role="assistant", content="Done")
            return output, state + 1

    class ErrorHarness(Harness):
        async def agent(self, scope, operation, payload, **options):
            result = await super().agent(scope, operation, payload, **options)
            if operation == "control" and payload["name"] == "turn.submit":
                for event in self.events:
                    calls = event["data"].get("calls") or event["data"].get("output", {}).get(
                        "tool_calls", []
                    )
                    for call in calls:
                        call.update(
                            name="chosen_lookup",
                            arguments={"phone_number": "missing-" + call["call_id"]},
                        )
            return result

    task = Task(
        id="engineering-user-effects",
        user_scenario={"instructions": "Check account after turning airplane mode on"},
        user_tools=["turn_airplane_mode_on"],
        evaluation_criteria={"reward_basis": ["DB"]},
    )
    environment = domain.get_environment()
    reference = Orchestrator(
        domain="telecom",
        agent=ReferenceAgent(environment.get_tools(), environment.get_policy()),
        user=User(tools=environment.get_user_tools()),
        environment=environment,
        task=task,
        max_steps=20,
    ).run()
    harness = ErrorHarness()
    harness.initial_history = [{"role": "assistant", "content": "Hi! How can I help you today?"}]
    harness.mapping = [
        {
            "native_name": "get_customer_by_phone",
            "exposed_name": "chosen_lookup",
            "schema_sha256": "a" * 64,
        }
    ]
    session = ExternalSession(
        harness,
        {"trial_id": "trial"},
        deadline=time.monotonic() + 30,
        schemas={"get_customer_by_phone": "a" * 64},
    )
    environment = domain.get_environment()
    agent = create_agent(
        root,
        "telecom",
        environment.get_tools(),
        environment.get_policy(),
        thread_exchange(session, asyncio.get_running_loop()),
    )
    actual = await asyncio.to_thread(
        Orchestrator(
            domain="telecom",
            agent=agent,
            user=User(tools=environment.get_user_tools()),
            environment=environment,
            task=task,
            max_steps=20,
        ).run
    )
    assert actual.termination_reason == reference.termination_reason
    assert actual.termination_reason.value == "user_stop"
    assert len(harness.results) == 2 and all(result["is_error"] for result in harness.results)
    assert any(
        message.role == "tool" and message.requestor == "user" for message in actual.messages
    )
    for simulation in (reference, actual):
        errors = [
            message
            for message in simulation.messages
            if message.role == "tool" and message.requestor == "assistant"
        ]
        assert len(errors) == 2 and all(message.error for message in errors)
    assert (
        evaluate_simulation(actual, task, EvaluationType.ALL, False, "telecom").reward
        == evaluate_simulation(reference, task, EvaluationType.ALL, False, "telecom").reward
    )
    assert (await session.close())["status"] == "success"
