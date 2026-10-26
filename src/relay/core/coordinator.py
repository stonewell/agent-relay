"""Coordinator limits and the restricted-capability qualification matrix (design 5.6, 7.4).

Coordination is available for a CLI only when a *qualification record* matches its exact
profile: agent, CLI version, OS, authentication mode and effective-configuration digest. An
unknown, changed or unqualified profile makes coordination unavailable; there is no `full`
fallback and no silent switch to another provider (section 7.4). A version probe or an
offline flag fixture is never a record; records come only from reviewed, opt-in canaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "CoordinationLimits",
    "CoordinatorProfile",
    "Qualification",
    "Selection",
    "QUALIFICATIONS",
    "select_coordinator",
    "qualification_for",
]

Mode = Literal["tool-free", "read-only"]
Status = Literal["qualified", "unqualified"]


@dataclass(frozen=True)
class CoordinationLimits:
    """Per-turn limits (section 5.6); every value is a positive integer."""

    max_steps: int = 6
    max_duration_s: int = 300
    context_max_bytes: int = 131072
    context_max_estimated_tokens: int = 16000

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"coordination {name} must be a positive integer, got {value!r}")


@dataclass(frozen=True)
class CoordinatorProfile:
    """What qualification is specific to (section 7.4)."""

    agent: str
    cli_version: str
    os: str
    auth_mode: str
    config_digest: str  # effective configuration, startup hooks, MCP/apps, managed policy


@dataclass(frozen=True)
class Qualification:
    profile: CoordinatorProfile
    mode: Mode
    status: Status
    evidence: str  # the reviewed canary run or review that established it


# Reviewed records only. Intentionally empty until spike 2.31 and the opt-in canaries of
# task 2.32 produce evidence: with no record, every coordinator is unavailable.
QUALIFICATIONS: tuple[Qualification, ...] = ()


def qualification_for(
    profile: CoordinatorProfile, records: Iterable[Qualification] = QUALIFICATIONS
) -> Qualification | None:
    """The record matching `profile` exactly, or None (unknown or changed profile)."""
    for record in records:
        if record.profile == profile:
            return record
    return None


@dataclass(frozen=True)
class Selection:
    """The outcome of choosing a coordinator: a qualified one, or why none is available."""

    agent: str | None
    mode: Mode | None
    reason: str

    @property
    def available(self) -> bool:
        return self.agent is not None


def select_coordinator(
    profiles: dict[str, CoordinatorProfile],
    *,
    requested: str | None = None,
    records: Iterable[Qualification] = QUALIFICATIONS,
) -> Selection:
    """Pick a coordinator from the probed `profiles` (agent name to its current profile).

    Claude is the default only when its profile qualifies; otherwise the operator must choose
    explicitly. An explicit request is honoured only if that agent qualifies; the choice never
    silently switches providers.
    """
    kept = tuple(records)
    if requested is not None:
        profile = profiles.get(requested)
        if profile is None:
            return Selection(None, None, f"coordinator {requested!r} is not installed or enabled")
        return _decide(requested, profile, kept)
    default = profiles.get("claude")
    if default is not None:
        chosen = _decide("claude", default, kept)
        if chosen.available:
            return chosen
    others = sorted(name for name in profiles if name != "claude")
    hint = f"; select one explicitly ({', '.join(others)})" if others else ""
    return Selection(None, None, f"no qualified default coordinator{hint}")


def _decide(
    agent: str, profile: CoordinatorProfile, records: tuple[Qualification, ...]
) -> Selection:
    record = qualification_for(profile, records)
    if record is None:
        return Selection(
            None,
            None,
            f"{agent} {profile.cli_version} on {profile.os} ({profile.auth_mode}) has no "
            "qualification record; coordination is unavailable (no full fallback)",
        )
    if record.status != "qualified":
        return Selection(None, None, f"{agent} profile is unqualified: {record.evidence}")
    return Selection(agent, record.mode, f"{agent} qualified ({record.mode}): {record.evidence}")
