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


# Reviewed records only; with no matching record a coordinator is unavailable. A record binds
# to the exact CLI version, OS, auth mode and digest, and is redone when any of them changes.
QUALIFICATIONS: tuple[Qualification, ...] = (
    Qualification(
        profile=CoordinatorProfile(
            agent="claude",
            cli_version="2.1.311",
            os="win32",
            auth_mode="subscription",
            config_digest="e2a8912cdaf30e6cd6952b12b877e64f295cb8dcb0362cc72728b6dcf1fb73ee",
        ),
        mode="tool-free",
        status="qualified",
        evidence=(
            "`relay doctor --check coordinator.claude` returned enforced on Claude Code 2.1.311 "
            "(2026-10-27; project hooks, MCP server and CLAUDE.md directive not honoured, secret "
            "not read); also observed on 2.1.310 in spike 2.31; Windows 11 10.0.26100, "
            "subscription auth, one observation each; operator-directed qualification"
        ),
    ),
    Qualification(
        profile=CoordinatorProfile(
            agent="claude",
            cli_version="2.1.312",
            os="win32",
            auth_mode="subscription",
            config_digest="e2a8912cdaf30e6cd6952b12b877e64f295cb8dcb0362cc72728b6dcf1fb73ee",
        ),
        mode="tool-free",
        status="qualified",
        evidence=(
            "`relay doctor --check coordinator.claude` returned enforced on Claude Code 2.1.312 "
            "(2026-10-27; tool-free profile ignored hooks, MCP, CLAUDE.md and files); Windows 11 "
            "10.0.26100, subscription auth, one observation; operator-directed qualification"
        ),
    ),
    # Codex has no coordinate capability, so no adapter produces a profile that could match
    # this record yet; it documents the spike 2.31 no-go and the fail-closed outcome.
    Qualification(
        profile=CoordinatorProfile(
            agent="codex",
            cli_version="0.164.0",
            os="win32",
            auth_mode="unspecified",
            config_digest="none",
        ),
        mode="read-only",
        status="unqualified",
        evidence=(
            "spike 2.31: `-s read-only` read a file outside the workspace, so it cannot "
            "enforce the context boundary; no tool-free flag found"
        ),
    ),
)


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
