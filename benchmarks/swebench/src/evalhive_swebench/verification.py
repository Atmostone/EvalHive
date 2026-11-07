"""Use native test construction and patch policy over an owned exec transport."""

from __future__ import annotations

import ast
import shlex

from .native import NativeConfigurationError, verified_code


def native_spec(instance):
    verified_code()
    from swebench.harness.utils import make_test_spec

    result = make_test_spec(instance)
    if result.image_assets:
        raise NativeConfigurationError("Native binary assets require their installed adapter")
    return result


def patch_commands():
    root = verified_code()
    tree = ast.parse((root / "harness" / "run_evaluation.py").read_text())
    assignments = [
        statement
        for statement in tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "GIT_APPLY_CMDS"
            for target in statement.targets
        )
    ]
    if len(assignments) != 1:
        raise NativeConfigurationError("Native patch application policy is unavailable")
    commands = ast.literal_eval(assignments[0].value)
    if (
        not isinstance(commands, list)
        or not 1 <= len(commands) <= 16
        or any(not isinstance(command, str) or not command for command in commands)
    ):
        raise NativeConfigurationError("Native patch application policy is invalid")
    return [shlex.split(command) for command in commands]


async def apply_patch(execute):
    """Apply the native fallback policy inside a fresh evaluator container.

    The callback is scoped to the evaluator and enforces deadlines. Patch bytes
    must already be sealed at the native path. Agent worktrees are never used.
    """
    commands = patch_commands()
    from swebench.harness.constants import CONTAINER_PATCH_FILE

    attempts = []
    for index, command in enumerate(commands):
        if index:
            reset = await execute(["/bin/bash", "-c", "git checkout -- . ; git clean -fd"])
            if reset.get("exit_code") != 0:
                raise NativeConfigurationError("Fresh native evaluator could not reset its tree")
        result = await execute([*command, str(CONTAINER_PATCH_FILE)])
        attempts.append({"command": command, "exit_code": result.get("exit_code")})
        if result.get("exit_code") == 0:
            return {"applied": True, "attempts": attempts, "reverse_check": False}
    result = await execute(["git", "apply", "--check", "--reverse", str(CONTAINER_PATCH_FILE)])
    return {
        "applied": result.get("exit_code") == 0,
        "attempts": attempts,
        "reverse_check": True,
    }
