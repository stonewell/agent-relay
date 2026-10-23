"""`relay routes` and `relay explain` (design sections 4.4, 11 and 14).

Both commands read the canonical ``node.routed`` events from every run's
``events.jsonl`` rather than the derived ``routes.jsonl`` projection, so a missing or
stale projection never hides a decision. ``read_events`` discards only a torn trailing
record; corruption earlier in the log is reported as a store failure.

Privacy: with ``route_log.include_goal: false`` the canonical entry already has a null
``title`` and ``from.reason`` and a structured ``reason`` code. These commands print
whatever the entry holds and never reconstruct goal text from another source.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relay.core.events import EventLogCorrupt, read_events
from relay.core.specs.route import RouteEntry
from relay.core.store import EVENTS, runs_dir

from ..exitcodes import Exit


def add_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    routes = subparsers.add_parser("routes", help="list routing decisions across runs")
    routes.add_argument(
        "run_id", nargs="?", metavar="RUN", help="only this run (default: all runs)"
    )
    routes.add_argument(
        "--tail", type=int, default=None, metavar="N", help="keep the newest N decisions"
    )
    routes.add_argument("--node", metavar="ID", help="only this node id")
    routes.add_argument("--agent", metavar="A", help="only decisions routed to this agent")
    routes.add_argument("--model", metavar="M", help="only decisions resolved to this model")
    routes.add_argument("--event", metavar="E", help="only this route event")
    routes.add_argument("--category", metavar="C", help="only this task category")
    routes.add_argument(
        "--rule",
        metavar="ID",
        help="only decisions whose agent or model rule is ID",
    )
    routes.add_argument(
        "--order",
        choices=("oldest", "newest"),
        default="oldest",
        help="chronological order (default: oldest first)",
    )
    routes.add_argument("--json", action="store_true", help="print entries as JSON")

    explain = subparsers.add_parser("explain", help="explain the routing decisions for one node")
    explain.add_argument("run_id", metavar="RUN")
    explain.add_argument("node_id", metavar="NODE")
    explain.add_argument("--json", action="store_true", help="print the explanation as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    if args.command == "routes":
        return _routes(args, repo_root)
    if args.command == "explain":
        return _explain(args, repo_root)
    raise AssertionError(f"routes module has no handler for {args.command!r}")


# --- reading canonical events -----------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    run_id: str
    directory: Path


def _run_dirs(repo_root: Path) -> list[_Run]:
    parent = runs_dir(repo_root)
    if not parent.is_dir():
        return []
    return [
        _Run(child.name, child)
        for child in sorted(parent.iterdir(), key=lambda item: item.name)
        if child.is_dir()
    ]


def _entries_in(directory: Path) -> list[RouteEntry]:
    events_path = directory / EVENTS
    if not events_path.is_file():
        return []
    entries: list[RouteEntry] = []
    for event in read_events(events_path):
        if event.type != "node.routed":
            continue
        entries.append(RouteEntry.model_validate(event.data["decision"]))
    return entries


def _read_runs(runs: list[_Run], command: str) -> tuple[list[RouteEntry], str | None]:
    entries: list[RouteEntry] = []
    for item in runs:
        try:
            entries.extend(_entries_in(item.directory))
        except (EventLogCorrupt, KeyError, ValueError) as exc:
            return [], f"{command}: cannot read {item.directory / EVENTS}: {exc}"
    return entries, None


def _select_run(repo_root: Path, run_id: str | None, command: str) -> tuple[list[_Run], str | None]:
    runs = _run_dirs(repo_root)
    if run_id is None:
        return [item for item in runs if (item.directory / EVENTS).is_file()], None
    for item in runs:
        if item.run_id == run_id:
            return [item], None
    return [], f"{command}: run {run_id!r} not found under {runs_dir(repo_root)}"


# --- routes -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filters:
    node: str | None
    agent: str | None
    model: str | None
    event: str | None
    category: str | None
    rule: str | None

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> _Filters:
        return cls(
            node=args.node,
            agent=args.agent,
            model=args.model,
            event=args.event,
            category=args.category,
            rule=args.rule,
        )

    def matches(self, entry: RouteEntry) -> bool:
        if self.node is not None and entry.node_id != self.node:
            return False
        if self.agent is not None and entry.agent != self.agent:
            return False
        if self.model is not None and entry.model != self.model:
            return False
        if self.event is not None and entry.event != self.event:
            return False
        if self.category is not None and entry.category != self.category:
            return False
        return self.rule is None or self.rule in (entry.agent_rule, entry.model_rule)


def _sort_key(entry: RouteEntry) -> tuple[str, str, int, int]:
    return (entry.ts, entry.run_id, entry.seq, entry.attempt)


def _routes(args: argparse.Namespace, repo_root: Path) -> int:
    as_json = bool(args.json)
    tail: int | None = args.tail
    if tail is not None and tail < 0:
        return _error("relay routes: --tail must be zero or greater", Exit.INVALID, as_json)
    selected, problem = _select_run(repo_root, args.run_id, "relay routes")
    if problem is not None:
        return _error(problem, Exit.INVALID, as_json)
    entries, problem = _read_runs(selected, "relay routes")
    if problem is not None:
        return _error(problem, Exit.FAILURE, as_json)

    filters = _Filters.from_args(args)
    ordered = sorted((entry for entry in entries if filters.matches(entry)), key=_sort_key)
    if tail is not None:
        ordered = ordered[-tail:] if tail else []
    if args.order == "newest":
        ordered.reverse()

    if as_json:
        _print_json({"count": len(ordered), "entries": [_entry_json(entry) for entry in ordered]})
    elif ordered:
        print(_render_table(ordered))
    else:
        print("no routing decisions")
    return int(Exit.OK)


def _entry_json(entry: RouteEntry) -> dict[str, Any]:
    return entry.model_dump(mode="json", by_alias=True)


def _target(entry: RouteEntry) -> str:
    if entry.agent is None:
        return "-"
    if entry.model is None:
        return entry.agent
    if entry.effort is not None:
        return f"{entry.agent}/{entry.model}({entry.effort})"
    return f"{entry.agent}/{entry.model}"


def _render_table(entries: list[RouteEntry]) -> str:
    header = [
        "TIME",
        "RUN",
        "SEQ",
        "NODE",
        "ATTEMPT",
        "EVENT",
        "CATEGORY",
        "TARGET",
        "AGENT_RULE",
        "MODEL_RULE",
        "TITLE",
    ]
    rows: list[list[str]] = [
        [
            entry.ts,
            entry.run_id,
            str(entry.seq),
            entry.node_id,
            str(entry.attempt),
            entry.event,
            f"{entry.category}/{entry.complexity}",
            _target(entry),
            entry.agent_rule,
            entry.model_rule or "-",
            entry.title or "-",
        ]
        for entry in entries
    ]
    widths: list[int] = [
        max(len(row[index]) for row in [header, *rows]) for index in range(len(header))
    ]
    lines: list[str] = []
    for row in [header, *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


# --- explain ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Signals:
    """Flags the canonical entry carries, including the structured privacy reason code."""

    queued: bool = False
    rejected: bool = False
    exhausted: tuple[str, ...] = ()
    model_unavailable: bool = False
    structured: bool = False


def _signals(entry: RouteEntry) -> _Signals:
    reason = entry.reason or ""
    structured = reason.startswith("agent_rule=")
    if not structured:
        return _Signals(rejected=entry.event == "rejected")
    parts = reason.split(";")
    exhausted: tuple[str, ...] = ()
    for part in parts:
        if part.startswith("exhausted="):
            exhausted = tuple(name for name in part.removeprefix("exhausted=").split(",") if name)
    return _Signals(
        queued="queued" in parts,
        rejected=entry.event == "rejected" or "rejected" in parts,
        exhausted=exhausted,
        model_unavailable="model=unavailable" in parts,
        structured=True,
    )


def _explain(args: argparse.Namespace, repo_root: Path) -> int:
    as_json = bool(args.json)
    run_id: str = args.run_id
    node_id: str = args.node_id
    selected, problem = _select_run(repo_root, run_id, "relay explain")
    if problem is not None:
        return _error(problem, Exit.INVALID, as_json)
    entries, problem = _read_runs(selected, "relay explain")
    if problem is not None:
        return _error(problem, Exit.FAILURE, as_json)
    node_entries = sorted(
        (entry for entry in entries if entry.node_id == node_id), key=lambda entry: entry.seq
    )
    if not node_entries:
        return _error(
            f"relay explain: node {node_id!r} has no routing decisions in run {run_id!r}"
            f" (try: relay routes {run_id} --node {node_id})",
            Exit.INVALID,
            as_json,
        )

    current = node_entries[-1]
    if as_json:
        _print_json(_explain_json(run_id, node_id, node_entries, current))
    else:
        print(_render_explain(run_id, node_id, node_entries))
    return int(Exit.OK)


def _explain_json(
    run_id: str, node_id: str, entries: list[RouteEntry], current: RouteEntry
) -> dict[str, Any]:
    signals = _signals(current)
    return {
        "run_id": run_id,
        "node_id": node_id,
        "current": _entry_json(current),
        "entries": [_entry_json(entry) for entry in entries],
        "chain": [
            {
                "attempt": entry.attempt,
                "seq": entry.seq,
                "event": entry.event,
                "agent": entry.agent,
                "model": entry.model,
                "from": (
                    None
                    if entry.from_ is None
                    else {
                        "agent": entry.from_.agent,
                        "model": entry.from_.model,
                        "reason": entry.from_.reason,
                    }
                ),
            }
            for entry in entries
        ],
        "alternatives": [
            {"agent": alternative.agent, "score": alternative.score}
            for alternative in current.alternatives
        ],
        "signals": {
            "queued": signals.queued,
            "rejected": signals.rejected,
            "exhausted": list(signals.exhausted),
            "model_unavailable": signals.model_unavailable,
            "structured": signals.structured,
        },
    }


def _render_from(entry: RouteEntry) -> str:
    previous = entry.from_
    if previous is None:
        return "-"
    source = previous.agent if previous.agent is not None else "-"
    if previous.model is not None:
        source = f"{source}/{previous.model}"
    if previous.reason:
        return f"{source} ({previous.reason})"
    return source


def _render_signals(signals: _Signals) -> str:
    parts: list[str] = []
    if signals.queued:
        parts.append("queued (waiting for capacity)")
    if signals.rejected:
        parts.append("rejected")
    if signals.exhausted:
        parts.append("exhausted: " + ", ".join(signals.exhausted))
    if signals.model_unavailable:
        parts.append("model unavailable")
    return "; ".join(parts) if parts else "none"


def _render_explain(run_id: str, node_id: str, entries: list[RouteEntry]) -> str:
    current = entries[-1]
    signals = _signals(current)
    lines = [
        f"run {run_id} node {node_id}: {len(entries)} routing decision(s)",
        "",
        f"latest: attempt {current.attempt}, seq {current.seq}, {current.event} at {current.ts}",
        f"  task: {current.title if current.title is not None else '-'}",
        f"  category: {current.category}/{current.complexity}",
        f"  agent: {current.agent if current.agent is not None else '-'}"
        f" (rule {current.agent_rule})",
        f"  model: {current.model if current.model is not None else '-'}"
        f" (rule {current.model_rule if current.model_rule is not None else '-'}"
        f", effort {current.effort if current.effort is not None else '-'})",
        f"  permissions: {current.permissions}; executed: {_yes_no(current.executed)};"
        f" resumed: {_yes_no(current.resumed)}",
        f"  reason: {current.reason if current.reason is not None else '-'}",
        f"  from: {_render_from(current)}",
        f"  candidates: {_render_signals(signals)}",
    ]
    if current.alternatives:
        alternatives = ", ".join(
            f"{alternative.agent} {alternative.score:g}" for alternative in current.alternatives
        )
        lines.append(f"  alternatives: {alternatives}")
    else:
        lines.append("  alternatives: none")
    lines.append("")
    lines.append("history (oldest first):")
    for entry in entries:
        entry_signals = _signals(entry)
        parts = [
            f"  seq {entry.seq} attempt {entry.attempt} {entry.event}",
            f"-> {_target(entry)}",
            f"agent_rule={entry.agent_rule}",
        ]
        if entry.model_rule is not None:
            parts.append(f"model_rule={entry.model_rule}")
        if entry.from_ is not None:
            parts.append(f"from {_render_from(entry)}")
        if entry_signals.queued:
            parts.append("queued")
        if entry_signals.rejected:
            parts.append("rejected")
        if entry_signals.exhausted:
            parts.append("exhausted=" + ",".join(entry_signals.exhausted))
        if entry_signals.model_unavailable:
            parts.append("model=unavailable")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


# --- output and errors ------------------------------------------------------------------------


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _error(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
