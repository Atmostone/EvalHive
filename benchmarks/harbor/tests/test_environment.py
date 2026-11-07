"""Native environment operations remain scoped broker calls with bounded time."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from evalhive_benchmark import ProtocolError
from evalhive_harbor.environment import BrokerEnvironment
from harbor.models.task.config import TaskOS


def setup(*, result=None, deadline=None, task_os=TaskOS.LINUX):
    context = SimpleNamespace(broker=AsyncMock(return_value=result or {
        "exit_code": 0, "stdout": "native stdout", "stderr": "native stderr",
    }))
    transfers = SimpleNamespace(upload=AsyncMock(), download=AsyncMock())
    environment = BrokerEnvironment(
        context, {"workspace_id": "workspace", "trial_id": "trial", "generation": 1},
        "registered.provider", "owned-handle", cwd="/native/project",
        deadline=deadline if deadline is not None else time.monotonic() + 60,
        transfers=transfers, task_os=task_os,
    )
    return environment, context, transfers


@pytest.mark.asyncio
async def test_native_command_user_environment_and_workdir_use_only_the_scoped_broker():
    environment, context, _ = setup()
    with environment.with_default_user(1000):
        result = await environment.exec(command="native-checker --fixture", env={"FIXTURE": "private-value"})
    assert environment.default_user is None
    assert result.return_code == 0 and result.stdout == "native stdout"
    arguments, options = context.broker.await_args
    assert arguments[0] == environment.scope
    assert arguments[1:3] == ("registered.provider", "exec")
    assert arguments[3]["argv"] == ["bash", "-c", "native-checker --fixture"]
    assert arguments[3]["cwd"] == "/native/project" and arguments[3]["user"] == "1000"
    assert arguments[3]["environment"] == {"FIXTURE": "private-value"}
    assert 0 < arguments[3]["timeout_seconds"] <= 60
    assert arguments[3]["deadline_policy"] == "inherit"
    assert options["handle"] == "owned-handle" and options["durable_host"]
    await environment.exec(command="next", user="root", timeout_sec=1)
    assert context.broker.await_args.args[3]["user"] == "root"
    assert context.broker.await_args.kwargs["request_id"] == environment.request_prefix + ".exec.2"


@pytest.mark.asyncio
async def test_truncated_native_diagnostics_preserve_the_result_and_mark_recording_incomplete():
    environment, _, _ = setup(result={
        "exit_code": 17, "stdout": "prefix", "stderr": "", "stdout_truncated": True,
    })
    result = await environment.exec(command="native-checker")
    assert result.return_code == 17 and not environment.recording_complete


@pytest.mark.asyncio
async def test_unconfirmed_exec_receipt_is_not_treated_as_native_success():
    environment, _, _ = setup(result={"exit_code": None, "stdout": "", "stderr": ""})
    with pytest.raises(ProtocolError) as caught:
        await environment.exec(command="native-checker")
    assert caught.value.code == "NATIVE_EXEC_RECEIPT_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize("task_os,deadline,code", [
    (TaskOS.WINDOWS, None, "NATIVE_OS_UNAVAILABLE"),
    (TaskOS.LINUX, -1, "NATIVE_VERIFIER_TIMEOUT"),
    (TaskOS.LINUX, 100000, "NATIVE_TIMEOUT_UNAVAILABLE"),
])
async def test_unsupported_or_expired_execution_fails_before_a_resource_call(task_os, deadline, code):
    environment, context, _ = setup(task_os=task_os, deadline=None if deadline is None else time.monotonic() + deadline)
    with pytest.raises(ProtocolError) as caught:
        await environment.exec(command="native-checker")
    assert caught.value.code == code
    context.broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_file_transfer_semantics_are_delegated_without_host_path_lookup(tmp_path):
    environment, _, transfers = setup()
    await environment.upload_dir(source_dir=tmp_path, target_dir="/tests")
    await environment.download_dir(source_dir="/logs/verifier", target_dir=tmp_path)
    await environment.download_dir_filtered(
        source_dir="/logs/verifier", target_dir=tmp_path, include=["*.txt"],
        exclude=["large.log"], protect=["reward.json", "reward.txt"],
    )
    transfers.upload.assert_awaited_once_with(source_dir=tmp_path, target_dir="/tests")
    assert transfers.download.await_count == 2
    assert transfers.download.await_args.kwargs["protect"] == ["reward.json", "reward.txt"]
    assert not environment.is_path_mounted("/logs/verifier", tmp_path)


@pytest.mark.asyncio
async def test_distinct_native_environments_do_not_reuse_the_same_durable_command_identity():
    first, context, transfers = setup()
    second = BrokerEnvironment(context, first.scope, first.provider, "other-owned-handle", cwd=first.cwd, deadline=first.deadline, transfers=transfers)
    await first.exec(command="first")
    await second.exec(command="second")
    identifiers = [call.kwargs["request_id"] for call in context.broker.await_args_list]
    assert len(set(identifiers)) == 2
