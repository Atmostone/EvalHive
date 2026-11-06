"""String enums with the display behavior used by historical records and logs."""

from enum import Enum, StrEnum


class HistoricalStringEnum(StrEnum):
    __str__ = Enum.__str__
    __format__ = Enum.__format__
