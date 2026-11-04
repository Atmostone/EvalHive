"""Modern string enums preserve the existing public formatting and JSON values."""

import enum
import json

from app.models.experiment import ExperimentRunStatus, ExperimentStatus
from app.models.task import TaskPriority, TaskStatus


def test_task_enum_values_and_formatting_match_historical_string_enums():
    for current in (TaskStatus, TaskPriority, ExperimentStatus, ExperimentRunStatus):
        historical = enum.Enum(current.__name__, {member.name: member.value for member in current}, type=str)
        for member in current:
            old = historical[member.name]
            assert isinstance(member, str) and member == member.value
            assert str(member) == str(old)
            assert format(member, ">30") == format(old, ">30")
            assert repr(member) == repr(old)
            assert json.dumps({"value": member}) == json.dumps({"value": old})
