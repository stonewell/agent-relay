"""Coordinator limits and selection (design 5.6, 7.4).

Any enabled, registered agent that offers a coordinate capability can coordinate. An explicit
request is honoured when that agent is a candidate; otherwise Claude is preferred and the first
sorted candidate is the fallback. The choice never silently switches providers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["CoordinationLimits", "Selection", "select_coordinator"]


@dataclass(frozen=True)
class CoordinationLimits:
    """Per-turn limits (section 5.6): positive integers; the context limits may be None (no cap)."""

    max_steps: int = 10
    max_duration_s: int = 300
    context_max_bytes: int | None = (
        None  # None: no relay-side cap; the agent enforces its own window
    )
    context_max_estimated_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if value is None and name.startswith("context_"):
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"coordination {name} must be a positive integer, got {value!r}")


@dataclass(frozen=True)
class Selection:
    """The outcome of choosing a coordinator: the chosen agent, or why none is available."""

    agent: str | None
    mode: str | None
    reason: str

    @property
    def available(self) -> bool:
        return self.agent is not None


def select_coordinator(agents: Iterable[str], *, requested: str | None = None) -> Selection:
    """Pick a coordinator from the enabled agents that offer the coordinate capability.

    An explicit request wins when it is a candidate; otherwise Claude is preferred, then the
    first candidate in sorted order. No candidates means no coordinator is installed or enabled.
    """
    candidates = sorted(agents)
    if not candidates:
        return Selection(None, None, "no coordinator installed or enabled")
    if requested is not None and requested in candidates:
        return Selection(requested, None, f"{requested} selected")
    name = "claude" if "claude" in candidates else candidates[0]
    return Selection(name, None, f"{name} selected")
