"""Process exit codes (design section 14)."""

from __future__ import annotations

from collections.abc import Iterable
from enum import IntEnum


class Exit(IntEnum):
    OK = 0
    FAILURE = 1  # execution or store failure, or an apply dirty-overlap abort
    INVALID = 2  # invalid input or configuration
    NO_AGENT = 3
    CANCELLED = 4
    VERIFICATION_FAILED = 5
    BLOCKED = 6  # needs input or exhausted budget
    APPROVAL_REQUIRED = 7


# For mixed outcomes: cancellation, verification failure, execution/store failure, no eligible
# agent, blocked, approval required, in that order (section 14).
_PRECEDENCE = (
    Exit.CANCELLED,
    Exit.VERIFICATION_FAILED,
    Exit.FAILURE,
    Exit.NO_AGENT,
    Exit.BLOCKED,
    Exit.APPROVAL_REQUIRED,
)


def combine(codes: Iterable[int]) -> Exit:
    """The exit code for several outcomes at once. Invalid input trumps everything."""
    seen = set(codes)
    if Exit.INVALID in seen:
        return Exit.INVALID
    for code in _PRECEDENCE:
        if code in seen:
            return code
    return Exit.OK
