"""Exercise the official verifier rather than duplicating its reward parser."""

import asyncio
from contextlib import contextmanager
from pathlib import Path

import pytest
from evalhive_harbor.native import HarborConfigurationError, parse_task
from evalhive_harbor.verification import NativeVerificationError, run_verifier
from harbor.environments.base import ExecResult
from harbor.models.task.config import TaskOS
from harbor.models.trial.paths import TrialPaths
from test_native import task


class Environment:
    os = TaskOS.LINUX

    def __init__(self, outputs, *, exit_code=0):
        self.files = {"reward.txt": "999", "reward.json": '{"stale":999}'}
        self.outputs, self.exit_code = outputs, exit_code
        self.order, self.uploads = [], []
        self.default_user = None
        self.executed_user = None

    @contextmanager
    def with_default_user(self, user):
        previous, self.default_user = self.default_user, user
        try:
            yield
        finally:
            self.default_user = previous

    def is_path_mounted(self, *args):
        return False

    async def exec(self, *, command, **options):
        if command.startswith("rm -f --"):
            self.order.append("clear")
            self.files.pop("reward.txt", None)
            self.files.pop("reward.json", None)
        elif command.startswith("chmod "):
            self.order.append("chmod")
        else:
            self.order.append("verify")
            self.executed_user = options.get("user", self.default_user)
            self.files.update(self.outputs)
            return ExecResult(return_code=self.exit_code, stdout="native stdout", stderr="")
        return ExecResult(return_code=0, stdout="", stderr="")

    async def upload_dir(self, *, source_dir, target_dir):
        self.order.append("upload")
        self.uploads.append((target_dir, sorted(p.name for p in Path(source_dir).iterdir())))

    async def download_dir(self, *, source_dir, target_dir):
        self.order.append("download")
        target_dir.mkdir(parents=True, exist_ok=True)
        for name, value in self.files.items():
            (target_dir / name).write_text(value)


def prepared(tmp_path, outputs, *, extra="", exit_code=0):
    native = parse_task(task(tmp_path, extra))
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    return native, Environment(outputs, exit_code=exit_code), paths


@pytest.mark.asyncio
async def test_native_verifier_clears_stale_rewards_uploads_tests_and_preserves_native_exit_semantics(tmp_path):
    native, environment, paths = prepared(tmp_path, {"reward.txt": "1"}, exit_code=17)
    result = await run_verifier(native, environment, paths)
    assert result.rewards == {"reward": 1.0}
    assert environment.order[0:2] == ["clear", "upload"]
    assert environment.order[-2:] == ["verify", "download"]
    assert environment.uploads == [("/tests", ["test.sh"])]
    assert not paths.reward_json_path.exists()


@pytest.mark.asyncio
async def test_native_named_continuous_rewards_are_not_converted_to_binary_success(tmp_path):
    native, environment, paths = prepared(tmp_path, {"reward.json": '{"quality":0.625,"latency_ms":17}'})
    result = await run_verifier(native, environment, paths)
    assert result.rewards == {"quality": 0.625, "latency_ms": 17}


@pytest.mark.asyncio
async def test_native_verifier_user_is_applied_and_previous_agent_user_restored(tmp_path):
    native, environment, paths = prepared(tmp_path, {"reward.txt": "0"}, extra='[verifier]\nuser="1000"\n')
    environment.default_user = "agent"
    await run_verifier(native, environment, paths)
    assert environment.executed_user == "1000" and environment.default_user == "agent"


@pytest.mark.asyncio
async def test_native_separate_image_with_bundled_tests_skips_upload(tmp_path):
    native, environment, paths = prepared(tmp_path, {"reward.txt": "1"}, extra=(
        '[verifier.environment]\ndocker_image="fixture/native-grader:locked"\n'
    ))
    assert native.verifier_mode == "separate"
    assert (await run_verifier(native, environment, paths)).rewards == {"reward": 1.0}
    assert environment.uploads == []


@pytest.mark.asyncio
async def test_changed_checker_is_rejected_before_any_native_environment_action(tmp_path):
    native, environment, paths = prepared(tmp_path, {"reward.txt": "1"})
    native.task.paths.test_path_for(TaskOS.LINUX).write_text("Changed checker")
    with pytest.raises(HarborConfigurationError, match="source changed"):
        await run_verifier(native, environment, paths)
    assert environment.order == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outputs,code", [
    ({}, "NATIVE_REWARD_MISSING"),
    ({"reward.txt": ""}, "NATIVE_REWARD_EMPTY"),
    ({"reward.txt": "NaN"}, "NATIVE_REWARD_INVALID"),
    ({"reward.json": '{"reward":"NaN"}'}, "NATIVE_REWARD_INVALID"),
    ({"reward.json": '{"reward":1e309}'}, "NATIVE_REWARD_INVALID"),
])
async def test_native_missing_empty_or_invalid_reward_is_an_error_not_a_wrong_answer(tmp_path, outputs, code):
    native, environment, paths = prepared(tmp_path, outputs)
    with pytest.raises(NativeVerificationError) as error:
        await run_verifier(native, environment, paths)
    assert error.value.code == code


@pytest.mark.asyncio
async def test_native_verifier_timeout_cancels_the_environment_operation(tmp_path):
    native, environment, paths = prepared(tmp_path, {}, extra="[verifier]\ntimeout_sec=0.05\n")
    stopped = asyncio.Event()

    async def blocked(**options):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    environment.exec = blocked
    with pytest.raises(NativeVerificationError) as error:
        await run_verifier(native, environment, paths, remaining_seconds=1)
    assert error.value.code == "NATIVE_VERIFIER_TIMEOUT" and stopped.is_set()
