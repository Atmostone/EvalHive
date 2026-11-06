"""Native CLI adaptation preserves one launch instant across reset and evaluation."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit


class NativeConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class LaunchTime:
    full: str

    def __post_init__(self):
        try:
            parsed = datetime.strptime(self.full, "%Y-%m-%d %H:%M:%S %A")
        except (TypeError, ValueError):
            raise NativeConfigurationError(
                "Launch time must use the native canonical format"
            ) from None
        if parsed.strftime("%Y-%m-%d %H:%M:%S %A") != self.full:
            raise NativeConfigurationError("Launch time weekday differs from its date")

    @classmethod
    def now(cls):
        return cls(datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S %A"))

    @property
    def short(self):
        return self.full.rsplit(" ", 1)[0]

    def fallback(self, native_output):
        """A native parser quirk changes representation, never the launch instant."""
        return self.short if "unconverted data remains" in native_output else None

    def verify_active(self, value):
        if value not in {self.full, self.short}:
            raise NativeConfigurationError("Active launch time differs from the frozen instant")
        return value


def task_path(case_id):
    if not isinstance(case_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", case_id
    ):
        raise NativeConfigurationError("Native task identity must be a single installed name")
    return PurePosixPath("/gym/tasks/finalpool") / case_id


def native_command(template, *, case_id, phase, launch_time, groundtruth_path=None):
    if phase not in {"preprocess", "evaluation"}:
        raise NativeConfigurationError("Unsupported native phase")
    root = task_path(case_id)
    try:
        argv = shlex.split(template)
    except (ValueError, TypeError):
        raise NativeConfigurationError("Native command template is invalid") from None
    if len(argv) < 2 or argv[0] not in {
        "python",
        "python3",
        "/opt/venv/bin/python",
        "/opt/venv/bin/python3",
    }:
        raise NativeConfigurationError("Native command must invoke the installed Python CLI")
    groundtruth = root / "groundtruth_workspace"
    if groundtruth_path is not None:
        path = PurePosixPath(groundtruth_path)
        if path.is_absolute() or ".." in path.parts or "\\" in groundtruth_path:
            raise NativeConfigurationError(
                "Groundtruth must remain inside its installed native task"
            )
        groundtruth = PurePosixPath("/gym") / path
        if not groundtruth.is_relative_to(root):
            raise NativeConfigurationError("Groundtruth belongs to another native task")
    values = {
        "TOOLATHLON_GYM_PATH": "/gym",
        "AGENT_WORKSPACE": "/workspace",
        "GROUNDTRUTH_WORKSPACE": str(groundtruth),
        "LAUNCH_TIME": launch_time,
        "RES_LOG_FILE": "/reports/" + phase + ".json",
    }
    for index, argument in enumerate(argv):
        for key, value in values.items():
            argument = argument.replace("${" + key + "}", value)
        if "${" in argument or "\0" in argument:
            raise NativeConfigurationError("Native command has unresolved arguments")
        argv[index] = argument
    if argv[1] != str(root / phase / "main.py"):
        raise NativeConfigurationError("Native command targets another installed task or phase")
    if argv.count("--launch_time") != 1 or argv.count("--agent_workspace") != 1:
        raise NativeConfigurationError("Native phase must bind workspace and launch time once")
    for key, expected in (("--launch_time", launch_time), ("--agent_workspace", "/workspace")):
        index = argv.index(key)
        if index + 1 == len(argv) or argv[index + 1] != expected:
            raise NativeConfigurationError(
                "Native phase arguments differ from their frozen bindings"
            )
    expected = {"--launch_time": launch_time, "--agent_workspace": "/workspace"}
    if phase == "evaluation":
        expected.update(
            {
                "--groundtruth_workspace": str(groundtruth),
                "--res_log_file": "/reports/evaluation.json",
            }
        )
    pairs = list(zip(argv[2::2], argv[3::2], strict=False))
    if (
        len(argv[2:]) % 2
        or len({name for name, _ in pairs}) != len(pairs)
        or any(name not in expected or value != expected[name] for name, value in pairs)
    ):
        raise NativeConfigurationError(
            "Native CLI arguments contain unsupported or conflicting bindings"
        )
    argv[0] = "/opt/venv/bin/python"
    return argv


def portal_urls(prompt):
    """Extract the exact public loopback resources the task requires to be ready."""
    result = []
    for candidate in re.findall(r"https?://(?:localhost|127\.0\.0\.1):\d+[^\s<>]*", prompt):
        parsed = urlsplit(candidate.rstrip("'\".,);]"))
        try:
            port = parsed.port
        except ValueError:
            raise NativeConfigurationError("Native portal port is invalid") from None
        if parsed.scheme != "http" or not port or parsed.username or parsed.password:
            raise NativeConfigurationError("Native portal requires a local HTTP endpoint")
        value = urlunsplit(("http", "127.0.0.1:" + str(port), parsed.path or "/", parsed.query, ""))
        if value not in result:
            result.append(value)
    return result
