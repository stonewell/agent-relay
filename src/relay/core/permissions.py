"""Permission level resolution, fail-closed checks and the run-start banner (design section 9).

Design section 9 defines three permission levels (9.1) and their precedence (9.2): all
explicit restrictions are cumulative, so the effective level is the *most restrictive* of
the task spec, the CLI/tool argument, the applicable agent config, the plugin option and
the global default (``readonly`` < ``workspace`` < ``full``). An omitted source adds no
restriction, and no source can widen an explicit restriction.

This module is the single home for that rule. It resolves the sources into a
:class:`PermissionDecision`, which the phase-1 ``relay run`` loop (task 1.19) consumes and
which carries everything the run-start banner needs. It never speaks to a CLI: the
adapters stay the only place that maps a level onto invocation flags (section 7.2), and the
only thing this module needs from an adapter is its honest ``probe().levels``.

Fail-closed behaviour (section 9.3): a level that an adapter does not advertise in
``probe().levels`` is refused with a clear error via :meth:`PermissionDecision.require_supported`.
It is never silently widened to ``full`` and never downgraded to a less restrictive level.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from .specs.common import PermissionLevel

__all__ = [
    "DEFAULT_LEVEL",
    "LEVELS",
    "PermissionDecision",
    "PermissionLevelError",
    "Resolution",
    "UnknownPermissionLevel",
    "UnsupportedPermissionLevel",
    "decide",
    "format_banner",
    "normalize_level",
    "resolve_level",
]

# The canonical level set, most permissive first (design section 9.1).
LEVELS: tuple[PermissionLevel, ...] = ("full", "workspace", "readonly")

# The default when nothing restricts a node (section 9.1: "Default: highest permission").
DEFAULT_LEVEL: PermissionLevel = "full"

# Restrictiveness rank: a lower value is more restrictive (section 9.2:
# `readonly` < `workspace` < `full`).
_RESTRICTION: dict[PermissionLevel, int] = {"readonly": 0, "workspace": 1, "full": 2}

# Source labels, from most to least explicit, used to name the source of the effective level
# when several sources tie at the same restriction (the design lists them as task, CLI/tool,
# agent config, plugin option, global default; section 9.2).
_SOURCE_ORDER: tuple[str, ...] = ("cli", "task", "plugin", "agent", "default", "planner")

# The inputs that count as an *explicit run/task request* (a per-run or per-task override).
# `agent` config and the global `default` are configuration, not an explicit request.
_EXPLICIT_SOURCES: tuple[str, ...] = ("cli", "task", "plugin")


class PermissionLevelError(ValueError):
    """A permission level is unknown or cannot be honoured (design sections 9.2, 9.3)."""


class UnknownPermissionLevel(PermissionLevelError):
    """A value is not one of the three canonical permission levels."""


class UnsupportedPermissionLevel(PermissionLevelError):
    """The resolved level is not in the adapter's advertised ``probe().levels`` (fail closed)."""


def normalize_level(value: object) -> PermissionLevel:
    """Validate a raw value against the canonical level set and reject unknown values.

    Accepts the three canonical level names (as a string or already-typed literal) and
    nothing else, so a misspelled value cannot silently fall back to ``full``.
    """
    if isinstance(value, str) and value in _RESTRICTION:
        return value
    known = ", ".join(LEVELS)
    raise UnknownPermissionLevel(f"unknown permission level {value!r}; expected one of {known}")


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving the permission sources for one node (design section 9.2).

    ``requested`` is the explicit run/task request (the most restrictive of the CLI,
    task-spec and plugin sources, or ``None`` when nothing was requested). ``effective`` is
    the cumulative, most-restrictive level including agent config, the global default and a
    planner read-only marking. ``source`` names the source that determined ``effective``.
    """

    requested: PermissionLevel | None
    effective: PermissionLevel
    source: str


def _most_restrictive(
    sources: dict[str, PermissionLevel | None],
) -> tuple[PermissionLevel | None, str | None]:
    """The most restrictive non-null level and the winning source (deterministic tie-break)."""
    winner: PermissionLevel | None = None
    winner_rank = _RESTRICTION["full"] + 1  # more permissive than any real level
    winner_source: str | None = None
    for name in _SOURCE_ORDER:
        value = sources.get(name)
        if value is None:
            continue
        rank = _RESTRICTION[value]
        # A tie keeps the earlier (more explicit) source; a stronger restriction replaces it.
        if winner is None or rank < winner_rank:
            winner = value
            winner_rank = rank
            winner_source = name
    return winner, winner_source


def resolve_level(
    *,
    task: PermissionLevel | None = None,
    cli: PermissionLevel | None = None,
    plugin: PermissionLevel | None = None,
    agent: PermissionLevel | None = None,
    default: PermissionLevel = DEFAULT_LEVEL,
    planner_read_only: bool = False,
) -> Resolution:
    """Resolve the effective permission level for one node (design section 9.2).

    All inputs are optional and each names one source from section 9.2. An omitted value
    adds no restriction; ``default`` is the global ``defaults.permissions`` (``full`` when
    the configuration does not set it); ``planner_read_only`` marks a node the planner made
    read-only, which imposes ``readonly`` even when the run default is ``full``. The result
    is the most restrictive of every provided source — source order never widens an explicit
    restriction.
    """
    sources: dict[str, PermissionLevel | None] = {
        "task": task,
        "cli": cli,
        "plugin": plugin,
        "agent": agent,
        "default": normalize_level(default),
        "planner": "readonly" if planner_read_only else None,
    }
    requested, _ = _most_restrictive({name: sources[name] for name in _EXPLICIT_SOURCES})
    effective, source = _most_restrictive(sources)
    # `default` is never None, so `effective`/`source` are always set.
    return Resolution(
        requested=requested,
        effective=cast(PermissionLevel, effective),
        source=cast(str, source),
    )


@dataclass(frozen=True)
class PermissionDecision:
    """A resolved permission for one adapter/attempt, consumed by the 1.19 run loop.

    ``advertised`` is the adapter's honest ``probe().levels``; ``supported`` reports whether
    ``effective`` is among them. A decision is never automatically widened to ``full`` when
    unsupported: the run loop must call :meth:`require_supported`, which raises a clear
    error, or handle the unsupported decision explicitly.
    """

    requested: PermissionLevel | None
    effective: PermissionLevel
    source: str
    adapter: str
    advertised: tuple[PermissionLevel, ...]
    supported: bool
    reason: str | None = None

    def require_supported(self) -> None:
        """Fail closed: raise when ``effective`` is not in the adapter's advertised levels.

        This is the one place the run loop turns an unsupported decision into a hard error,
        never into a silent downgrade to ``full`` (design section 9.3).
        """
        if self.supported:
            return
        raise UnsupportedPermissionLevel(self.reason or self._unsupported_message())

    def _unsupported_message(self) -> str:
        known = ", ".join(self.advertised) or "none"
        return (
            f"adapter {self.adapter!r} cannot honour permission level {self.effective!r} "
            f"(advertised levels: {known}); refusing to run unconfined (design section 9.3)"
        )


def decide(
    adapter: str,
    advertised: Sequence[PermissionLevel],
    resolution: Resolution,
) -> PermissionDecision:
    """Combine a :class:`Resolution` with an adapter's advertised levels into a decision.

    Never raises: an unsupported level is returned as ``supported=False`` with a reason, so
    callers can render it in a banner and then fail closed with :meth:`require_supported`. An
    advertised value that is not a canonical permission level is also returned as
    ``supported=False`` (with a reason naming the bad value) rather than raising; a bogus
    *requested* level still raises in :func:`normalize_level` / :func:`resolve_level`, which
    is the fail-closed input error.
    """
    normalized: list[PermissionLevel] = []
    bogus: list[str] = []
    for level in advertised:
        try:
            normalized.append(normalize_level(level))
        except UnknownPermissionLevel:
            bogus.append(repr(level))
    if bogus:
        reason = (
            f"adapter {adapter!r} advertised an unknown permission level "
            f"{', '.join(bogus)}; treating the adapter as unsupported (fail closed)"
        )
        return PermissionDecision(
            requested=resolution.requested,
            effective=resolution.effective,
            source=resolution.source,
            adapter=adapter,
            advertised=tuple(normalized),
            supported=False,
            reason=reason,
        )
    supported = resolution.effective in normalized
    reason = None if supported else _unsupported_message(adapter, normalized, resolution.effective)
    return PermissionDecision(
        requested=resolution.requested,
        effective=resolution.effective,
        source=resolution.source,
        adapter=adapter,
        advertised=tuple(normalized),
        supported=supported,
        reason=reason,
    )


def _unsupported_message(
    adapter: str, advertised: Sequence[PermissionLevel], level: PermissionLevel
) -> str:
    known = ", ".join(advertised) or "none"
    return (
        f"adapter {adapter!r} cannot honour permission level {level!r} "
        f"(advertised levels: {known}); refusing to run unconfined (design section 9.3)"
    )


def _source_label(source: str) -> str:
    return {
        "cli": "CLI/tool argument",
        "task": "task spec",
        "plugin": "plugin option",
        "agent": "agent config",
        "default": "global default",
        "planner": "planner read-only",
    }.get(source, source)


def format_banner(
    decisions: Sequence[PermissionDecision],
    *,
    run_id: str | None = None,
) -> str:
    """Render the run-start permission banner (design sections 9.4, 14).

    Pure: it takes resolved decisions and returns lines, one run-start line stating the
    active level per agent, plus a warning when ``full`` is active or when a requested level
    was rejected. It performs no I/O and does not decide anything itself.
    """
    lines: list[str] = []
    header = "permissions" if run_id is None else f"permissions for run {run_id}"
    lines.append(f"{header}:")
    for decision in decisions:
        if decision.supported:
            requested = "" if decision.requested is None else f" (requested {decision.requested})"
            lines.append(
                f"  {decision.adapter}: {decision.effective} [{_source_label(decision.source)}]"
                f"{requested}"
            )
        else:
            lines.append(
                f"  {decision.adapter}: {decision.effective} REJECTED "
                f"[{_source_label(decision.source)}] — {decision.reason}"
            )
    warnings = _warnings(decisions)
    if warnings:
        lines.append("")
        lines.extend(f"warning: {warning}" for warning in warnings)
    return "\n".join(lines)


def _warnings(decisions: Sequence[PermissionDecision]) -> list[str]:
    warnings: list[str] = []
    for decision in decisions:
        if decision.supported and decision.effective == "full":
            warnings.append(
                f"{decision.adapter} runs at full permission: it may edit files, run any "
                "shell command and use the network without approval"
            )
        elif not decision.supported:
            warnings.append(
                f"{decision.adapter} requested {decision.effective}, which it does not "
                f"advertise; the run will fail closed unless another agent is chosen"
            )
    return warnings
