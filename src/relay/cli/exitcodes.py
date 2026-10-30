"""Gateway process exit codes."""

from enum import IntEnum


class Exit(IntEnum):
    OK = 0
    FAILURE = 1
    INVALID = 2
    CANCELLED = 4
