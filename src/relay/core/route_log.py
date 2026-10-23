"""Route log (design section 4.4).

Every routing decision is authored once, as the canonical ``node.routed`` event in
``events.jsonl``, and rendered into two per-run projections: ``routes.jsonl`` (one JSON
object per decision) and ``route.log`` (one human-readable line per decision). The canonical
event is written durably before the worker starts, so a crash still leaves the record; the
projections are rebuildable from it, idempotently, by event ``seq``.

Privacy: ``route_log.include_goal: false`` omits titles and free-text reasons (including
``from.reason``) while retaining operational ids, rules, scores, timestamps and permissions,
replacing the free-text ``reason`` with a deterministic structured code. Titles and reasons
always have strings matching known secret patterns redacted.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .env import REDACTED, redact_url
from .events import EventLog, read_events
from .model_router import ModelDecision
from .registry import RouteLogConfig
from .router import AgentDecision
from .specs.common import Category, Complexity, NodeId, RunId
from .specs.route import Alternative, PreviousRoute, RouteEntry, RouteEvent
from .store import atomic_write, iso

__all__ = [
    "RouteLog",
    "build_entry",
    "derive_title",
    "first_sentence",
    "rebuild_projections",
    "redact_secrets",
    "render_log_line",
    "structured_reason",
]

# --- secret redaction -------------------------------------------------------------------------

# Well-known API-key/token prefixes and JWTs; matched as whole tokens so a value like a
# commit SHA is not redacted.
_SECRET_TOKEN = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{16,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|ASIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|ya29\.[0-9A-Za-z._-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)

# `secret=...`, `token: ...` and friends: the key is kept, the value is masked.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(secret|token|password|passwd|pwd|api[_-]?key|access[_-]?key|"
    r"private[_-]?key)\b(\s*[:=]\s*)(\S+)"
)


def redact_secrets(text: str) -> str:
    """Redact known secret patterns from free text (design section 4.4).

    URL userinfo is always masked (via :func:`relay.core.env.redact_url`); well-known
    key/token shapes and ``key=value`` credential assignments are replaced with
    ``<redacted>``. Returns a new string.
    """
    result = redact_url(text)
    result = _SECRET_TOKEN.sub(REDACTED, result)
    result = _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", result)
    return result


# --- title derivation -------------------------------------------------------------------------

_SENTENCE_BOUNDARY = re.compile(r"[.!?]")


def first_sentence(text: str) -> str:
    """The text up to the first ``.``, ``!`` or ``?`` (the first sentence)."""
    match = _SENTENCE_BOUNDARY.search(text)
    return text[: match.start()] if match is not None else text


def derive_title(title: str | None, goal: str, max_chars: int) -> str | None:
    """Short description for the route log (design section 4.4).

    The explicit ``title`` wins; otherwise the first sentence of ``goal``. Whitespace is
    collapsed, secrets are redacted, then the result is truncated to ``max_chars``.
    """
    explicit = title if title is not None and title.strip() else None
    raw = explicit if explicit is not None else first_sentence(goal)
    collapsed = " ".join(raw.split())
    truncated = redact_secrets(collapsed)[:max_chars]
    return truncated or None


# --- structured reason ------------------------------------------------------------------------


def structured_reason(agent: AgentDecision, model: ModelDecision | None) -> str:
    """Deterministic, non-sensitive reason code for ``include_goal: false``.

    Captures which rules matched plus the queued/rejected/exhausted state, so
    ``relay explain`` can still explain the decision without the goal text.
    """
    parts = [f"agent_rule={agent.agent_rule}"]
    if model is not None and model.model_rule:
        parts.append(f"model_rule={model.model_rule}")
    if agent.queued:
        parts.append("queued")
    if agent.rejected:
        parts.append("rejected")
    if agent.exhausted:
        parts.append("exhausted=" + ",".join(excluded.reason for excluded in agent.exhausted))
    if model is not None and model.warning:
        parts.append("model=unavailable")
    return ";".join(parts)


def _sanitize_previous(previous: PreviousRoute | None, include_goal: bool) -> PreviousRoute | None:
    if previous is None:
        return None
    if not include_goal:
        return PreviousRoute(agent=previous.agent, model=previous.model, reason=None)
    reason = redact_secrets(previous.reason) if previous.reason else None
    return PreviousRoute(agent=previous.agent, model=previous.model, reason=reason)


# --- entry construction -----------------------------------------------------------------------


def build_entry(
    *,
    run_id: RunId,
    node_id: NodeId,
    attempt: int,
    event: RouteEvent,
    category: Category,
    complexity: Complexity,
    agent_decision: AgentDecision,
    model_decision: ModelDecision | None = None,
    title: str | None = None,
    goal: str = "",
    include_goal: bool = True,
    title_max_chars: int = 120,
    previous: PreviousRoute | None = None,
    executed: bool = True,
    resumed: bool = False,
    ts: str = "",
    seq: int = 0,
) -> RouteEntry:
    """Build a complete :class:`RouteEntry` from an agent and a model decision.

    The single writer owns ``ts`` and ``seq``: :meth:`RouteLog.route` fills them in. The
    wrapper the scheduler calls (task 1.19) is :meth:`RouteLog.route`, which delegates here.
    """
    rejected = event == "rejected"
    if rejected:
        model: str | None = None
        effort: str | None = None
        model_rule: str | None = None
    else:
        model = model_decision.model if model_decision is not None else None
        effort = model_decision.effort if model_decision is not None else None
        model_rule = model_decision.model_rule if model_decision is not None else None

    alternatives = [
        Alternative(agent=alternative.agent, score=alternative.score)
        for alternative in agent_decision.alternatives
    ]

    if include_goal:
        entry_title = derive_title(title, goal, title_max_chars)
        reason = redact_secrets(agent_decision.reason) if agent_decision.reason else None
    else:
        entry_title = None
        reason = structured_reason(agent_decision, model_decision)

    return RouteEntry.model_validate(
        {
            "ts": ts,
            "run_id": run_id,
            "seq": seq,
            "node_id": node_id,
            "attempt": attempt,
            "event": event,
            "title": entry_title,
            "category": category,
            "complexity": complexity,
            "agent": agent_decision.agent,
            "model": model,
            "effort": effort,
            "agent_rule": agent_decision.agent_rule,
            "model_rule": model_rule,
            "reason": reason,
            "alternatives": alternatives,
            "from": _sanitize_previous(previous, include_goal),
            "permissions": agent_decision.permissions,
            "executed": executed,
            "resumed": resumed,
        }
    )


# --- human-readable rendering -----------------------------------------------------------------


def _render_target(agent: str | None, model: str | None, effort: str | None) -> str:
    if agent is None:
        return "-"
    if model is None:
        return agent
    if effort is not None:
        return f"{agent}/{model}({effort})"
    return f"{agent}/{model}"


def _render_from(previous: PreviousRoute) -> str:
    source = previous.agent if previous.agent is not None else "-"
    if previous.reason:
        return f"(from {source}: {previous.reason})"
    return f"(from {source})"


def render_log_line(entry: RouteEntry) -> str:
    """One ``route.log`` line for a decision, in the shape of the design section 4.4 example."""
    parts = [
        entry.ts,
        f"#{entry.seq}",
        entry.node_id,
        f"{entry.category}/{entry.complexity}",
        f"-> {_render_target(entry.agent, entry.model, entry.effort)}",
        f"agent_rule={entry.agent_rule}",
    ]
    if entry.model_rule is not None:
        parts.append(f"model_rule={entry.model_rule}")
    if entry.title:
        parts.append(f'"{entry.title}"')
    if entry.from_ is not None:
        parts.append(_render_from(entry.from_))
    return " ".join(parts)


# --- writer -----------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


class RouteLog:
    """Single-writer route log for one run (design section 4.4)."""

    def __init__(
        self,
        event_log: EventLog,
        routes_path: Path,
        route_log_path: Path,
        config: RouteLogConfig,
        *,
        run_id: RunId,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._events = event_log
        self._routes_path = routes_path
        self._route_log_path = route_log_path
        self._config = config
        self._run_id = run_id
        self._now = now if now is not None else _now
        self._next_seq = self._scan_max_route_seq() + 1

    def _scan_max_route_seq(self) -> int:
        maximum = 0
        for event in read_events(self._events.path):
            if event.type == "node.routed":
                maximum = max(maximum, int(event.data["route_seq"]))
        return maximum

    @property
    def next_seq(self) -> int:
        """The route ``seq`` the next decision will receive (monotonic per run)."""
        return self._next_seq

    def close(self) -> None:
        """Release the underlying event log handle (idempotent)."""
        self._events.close()

    def route(
        self,
        *,
        node_id: NodeId,
        attempt: int,
        event: RouteEvent,
        category: Category,
        complexity: Complexity,
        agent_decision: AgentDecision,
        model_decision: ModelDecision | None = None,
        title: str | None = None,
        goal: str = "",
        previous: PreviousRoute | None = None,
        executed: bool = True,
        resumed: bool = False,
    ) -> RouteEntry:
        """Persist one routing decision and return the completed entry.

        The canonical ``node.routed`` event is appended to ``events.jsonl`` first and is
        durable before this call returns; a failure there propagates and prevents dispatch.
        Projection failures emit ``route.log.write_failed`` and never block execution.
        """
        seq = self._next_seq
        ts = iso(self._now())
        entry = build_entry(
            run_id=self._run_id,
            node_id=node_id,
            attempt=attempt,
            event=event,
            category=category,
            complexity=complexity,
            agent_decision=agent_decision,
            model_decision=model_decision,
            title=title,
            goal=goal,
            include_goal=self._config.include_goal,
            title_max_chars=self._config.title_max_chars,
            previous=previous,
            executed=executed,
            resumed=resumed,
            ts=ts,
            seq=seq,
        )
        payload = entry.model_dump(mode="json", by_alias=True)
        # Canonical event: durable, written before dispatch. Failure prevents dispatch.
        self._events.append(
            "node.routed",
            {"node_id": entry.node_id, "route_seq": seq, "decision": payload},
        )
        self._next_seq += 1
        if self._config.enabled:
            self._write_projections(entry, payload)
        return entry

    def _write_projections(self, entry: RouteEntry, payload: dict[str, Any]) -> None:
        try:
            self._append_routes(payload)
            self._append_route_log(entry)
        except OSError as exc:
            with contextlib.suppress(Exception):
                self._events.append(
                    "route.log.write_failed",
                    {
                        "node_id": entry.node_id,
                        "route_seq": entry.seq,
                        "error": str(exc),
                    },
                )

    def _append_routes(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        with open(self._routes_path, "ab") as handle:  # noqa: SIM115 - append-and-close
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

    def _append_route_log(self, entry: RouteEntry) -> None:
        line = render_log_line(entry) + "\n"
        with open(self._route_log_path, "ab") as handle:  # noqa: SIM115 - append-and-close
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())


# --- rebuild ----------------------------------------------------------------------------------


def rebuild_projections(
    event_log_path: Path,
    routes_path: Path,
    route_log_path: Path,
    *,
    enabled: bool = True,
) -> int:
    """Rewrite both projections from the canonical events, idempotently by event ``seq``.

    Only a torn trailing record in ``events.jsonl`` is discarded (by :func:`read_events`);
    a valid earlier entry is never skipped. With ``enabled`` false the projections are left
    untouched and nothing is written. Returns how many routing decisions were found.
    """
    decisions: list[dict[str, Any]] = []
    entries: list[RouteEntry] = []
    for event in read_events(event_log_path):
        if event.type != "node.routed":
            continue
        decision = event.data["decision"]
        decisions.append(decision)
        entries.append(RouteEntry.model_validate(decision))
    if not enabled:
        return len(entries)
    routes_text = "".join(
        json.dumps(decision, separators=(",", ":"), ensure_ascii=False) + "\n"
        for decision in decisions
    )
    log_text = "".join(render_log_line(entry) + "\n" for entry in entries)
    atomic_write(routes_path, routes_text)
    atomic_write(route_log_path, log_text)
    return len(entries)
