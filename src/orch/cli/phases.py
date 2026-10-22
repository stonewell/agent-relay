"""Which phase each command arrives in (design section 16, command availability)."""

from __future__ import annotations

from orch.core.specs.common import CURRENT_PHASE

COMMAND_PHASE: dict[str, int] = {
    "run": 1,
    "status": 1,
    "result": 1,
    "apply": 1,
    "reply": 1,
    "resume": 1,
    "explain": 1,
    "routes": 1,
    "agents": 1,
    "doctor": 1,
    "clean": 1,
    "plan": 2,
    "graph": 2,
    "cancel": 2,
    "opencode-config": 3,
    "replay": 4,
}

IMPLEMENTED = {"doctor"}
"""Commands whose handler exists in this build; grows with the phase 1 tasks."""


def availability_error(command: str) -> str | None:
    """A clear error if `command` is not available yet, else None."""
    phase = COMMAND_PHASE[command]
    if phase > CURRENT_PHASE:
        return f"orch {command}: available from phase {phase}"
    if command not in IMPLEMENTED:
        return f"orch {command}: not implemented yet in this build (phase {phase} work in progress)"
    return None
