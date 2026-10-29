"""Which phase each command arrives in (design section 16, command availability)."""

from __future__ import annotations

from relay.core.specs.common import CURRENT_PHASE

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
    "tui": 2,
    "serve": 5,
    "opencode-config": 5,
    "replay": 4,
}

IMPLEMENTED = {
    "graph",
    "plan",
    "cancel",
    "reply",
    "resume",
    "agents",
    "apply",
    "clean",
    "doctor",
    "explain",
    "result",
    "routes",
    "run",
    "status",
    "tui",
    "serve",
    "opencode-config",
}
"""Commands whose handler exists in this build; grows with the phase 1 tasks."""

EARLY_ACCESS = frozenset({"serve", "opencode-config"})
"""Later-phase commands shipped ahead of their phase gate.

The standalone phase-5 gateway (milestone M7) is independent of the phase 2b-4 work, so its
commands are usable now without raising `CURRENT_PHASE` for every other later-phase feature.
"""


def availability_error(command: str) -> str | None:
    """A clear error if `command` is not available yet, else None."""
    phase = COMMAND_PHASE[command]
    if phase > CURRENT_PHASE and command not in EARLY_ACCESS:
        return f"relay {command}: available from phase {phase}"
    if command not in IMPLEMENTED:
        return (
            f"relay {command}: not implemented yet in this build (phase {phase} work in progress)"
        )
    return None
