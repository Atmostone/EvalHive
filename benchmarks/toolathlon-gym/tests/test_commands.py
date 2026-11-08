"""Native timestamp, command and loopback readiness bindings."""

import os

import pytest
from evalhive_toolathlon.commands import (
    LaunchTime,
    NativeConfigurationError,
    native_command,
    portal_urls,
    task_path,
)

TIME = LaunchTime("2026-11-06 10:15:30 Friday")


def command(phase):
    return f"python ${{TOOLATHLON_GYM_PATH}}/tasks/finalpool/native-case/{phase}/main.py --agent_workspace ${{AGENT_WORKSPACE}} --launch_time ${{LAUNCH_TIME}}"


def test_date_parser_fallback_and_checker_share_the_original_instant():
    fallback = TIME.fallback("ValueError: unconverted data remains: Friday")
    assert fallback == "2026-11-06 10:15:30"
    for phase in ("preprocess", "evaluation"):
        argv = native_command(
            command(phase),
            case_id="native-case",
            phase=phase,
            launch_time=TIME.verify_active(fallback),
        )
        assert argv[0] == "/opt/venv/bin/python"
        assert argv[-1] == fallback and len(argv) == 6
    assert TIME.fallback("Connection refused") is None
    with pytest.raises(NativeConfigurationError):
        TIME.verify_active("2026-11-07 10:15:30")


@pytest.mark.parametrize("value", ["2026-11-06 10:15:30 Monday", "2026-11-06 10:15:30", "", None])
def test_noncanonical_or_conflicting_timestamp_is_rejected(value):
    with pytest.raises(NativeConfigurationError):
        LaunchTime(value)


@pytest.mark.parametrize("case_id", ["../other", "a/b", "a\\b", ".", "", "x;id"])
def test_task_identity_cannot_select_a_foreign_native_path(case_id):
    with pytest.raises(NativeConfigurationError):
        task_path(case_id)


def test_cli_templates_are_argv_not_shell_commands_and_keep_spaces_in_timestamp():
    template = (
        command("evaluation")
        + " --groundtruth_workspace ${GROUNDTRUTH_WORKSPACE} --res_log_file ${RES_LOG_FILE}"
    )
    argv = native_command(
        template,
        case_id="native-case",
        phase="evaluation",
        launch_time=TIME.full,
        groundtruth_path="tasks/finalpool/native-case/groundtruth_workspace",
    )
    assert argv[5] == TIME.full
    assert argv[-1] == "/reports/evaluation.json"
    for invalid in ["../other", "tasks/finalpool/other/groundtruth_workspace", "/private"]:
        with pytest.raises(NativeConfigurationError):
            native_command(
                template,
                case_id="native-case",
                phase="evaluation",
                launch_time=TIME.full,
                groundtruth_path=invalid,
            )
    with pytest.raises(NativeConfigurationError):
        native_command(
            command("evaluation").replace("native-case", "another-case"),
            case_id="native-case",
            phase="evaluation",
            launch_time=TIME.full,
        )


@pytest.mark.parametrize(
    "suffix",
    [
        " --agent_workspace=/private",
        " ; true",
        " --unknown value",
        " --launch_time hidden",
        " --res_log_file /workspace/leaked",
    ],
)
def test_extra_flags_cannot_override_frozen_native_bindings(suffix):
    with pytest.raises(NativeConfigurationError):
        native_command(
            command("evaluation") + suffix,
            case_id="native-case",
            phase="evaluation",
            launch_time=TIME.full,
        )


def test_portal_readiness_uses_prompt_paths_in_owned_namespace_not_a_fixed_delay():
    assert portal_urls(
        "Read http://localhost:30325/api/data.json and http://127.0.0.1:30325/api/data.json. Ignore https://example.com."
    ) == ["http://127.0.0.1:30325/api/data.json"]
    assert portal_urls("No portal required") == []
    with pytest.raises(NativeConfigurationError):
        portal_urls("Read https://localhost:30325/private")


def test_existing_native_cli_case_templates_preserve_phase_bindings():
    if "CASE_CORPUS_PATH" not in os.environ:
        pytest.skip("Set CASE_CORPUS_PATH to verify existing native CLI templates")
    from evalhive_cases.format import load_suite

    cases = load_suite(os.environ["CASE_CORPUS_PATH"], "toolathlon")
    assert cases
    for case in cases:
        external = case.external_eval
        for phase, field in (("preprocess", "preprocess_command"), ("evaluation", "eval_command")):
            argv = native_command(
                external[field],
                case_id=case.id,
                phase=phase,
                launch_time=TIME.full,
                groundtruth_path=external.get("groundtruth_path"),
            )
            assert argv[argv.index("--launch_time") + 1] == TIME.full
            assert argv[1] == str(task_path(case.id) / phase / "main.py")
