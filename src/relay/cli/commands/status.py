"""``relay status`` (design sections 6, 11 and 14; task 1.22).

With a run id it renders that run's record and event log. Without one it lists every run under
``.agents/runs``. Nothing is inferred from the derived ``routes.jsonl`` projection, so a missing
projection never hides a run or a state. The command is an observer: it reads ``run.json`` and
``events.jsonl`` directly and never takes the run lock or mutates the store.

A missing run is invalid input (exit 2). A run whose ``run.json`` or event log cannot be parsed
is a store failure (exit 1). Exit codes are those of design section 14.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relay.core.runrecord import NodeRecord, RunRecord
from relay.core.store import RUN_JSON, runs_dir

from ..exitcodes import Exit
from .inspect_common import (
    InspectError,
    RunView,
    age_seconds,
    display_path,
    format_age,
    last_event,
    load_run,
    node_ids,
)

_COLUMNS = ("RUN", "STATE", "NODES", "FINAL", "AGE")


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("status", help="show a run or list recent runs")
    parser.add_argument(
        "run_id", nargs="?", metavar="RUN", help="the run to show (default: all runs)"
    )
    parser.add_argument("--json", action="store_true", help="print the status as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    as_json = bool(args.json)
    run_id: str | None = args.run_id
    if run_id is not None:
        try:
            view = load_run(repo_root, run_id)
        except InspectError as exc:
            return _fail(f"relay status: {exc}", _exit_for(exc), as_json)
        payload = _run_payload(view, now=datetime.now(UTC))
        if as_json:
            _print_json({"count": 1, "runs": [payload]})
        else:
            print(_render_run(view, now=datetime.now(UTC)))
        return int(Exit.OK)

    entries = _list_runs(repo_root, now=datetime.now(UTC))
    if as_json:
        _print_json({"count": len(entries), "runs": entries})
    elif not entries:
        print(f"no runs under {display_path(runs_dir(repo_root), repo_root)}")
    else:
        print(_render_list(entries))
    return int(Exit.OK)


# --- single run -------------------------------------------------------------------------------


def _run_payload(view: RunView, *, now: datetime) -> dict[str, Any]:
    record = view.record
    events = view.events
    newest = events[-1] if events else None
    nodes = [_node_payload(record, node_id, view) for node_id in node_ids(view)]
    return {
        "run_id": record.run_id,
        "state": record.state,
        "created_at": record.created_at,
        "age_s": age_seconds(record.created_at, now=now),
        "owner_generation": record.owner_generation,
        "config_hash": record.config_hash,
        "final_commit": record.final_commit,
        "nodes": nodes,
        "reasons": _reasons(view),
        "last_event_ts": newest.ts if newest is not None else None,
        "last_event_age_s": age_seconds(newest.ts, now=now) if newest is not None else None,
    }


def _node_payload(record: RunRecord, node_id: str, view: RunView) -> dict[str, Any]:
    node = record.nodes.get(node_id)
    if node is None:
        return {"node_id": node_id, "state": "unknown", "attempts": 0}
    return {
        "node_id": node_id,
        "state": node.state,
        "attempts": node.attempts,
        "blocked_reason": node.blocked_reason,
        "output_commit": node.output_commit,
        "dir": node.dir,
    }


def _reasons(view: RunView) -> list[str]:
    """Blocked/failure reasons from the record and the canonical event log, de-duplicated."""
    reasons: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value and value not in reasons:
            reasons.append(value)

    for node_id in node_ids(view):
        node = view.record.nodes.get(node_id)
        if node is not None and node.blocked_reason:
            add(f"{node_id}: {node.blocked_reason}")
    for event in view.events:
        if event.type == "node.state":
            reason = event.data.get("reason")
            if reason:
                add(f"{event.data.get('node_id', '?')}: {reason}")
        elif event.type == "run.finished" and view.record.state in (
            "failed",
            "blocked",
            "cancelled",
            "interrupted",
        ):
            add(f"run: {event.data.get('state', view.record.state)}")
    if not reasons and view.record.state in ("failed", "blocked", "cancelled", "interrupted"):
        add(f"run: {view.record.state}")
    return reasons


def _render_run(view: RunView, *, now: datetime) -> str:
    record = view.record
    newest = last_event(view)
    age = format_age(age_seconds(record.created_at, now=now))
    lines = [
        f"run {record.run_id}: {record.state}",
        f"  created: {record.created_at} ({age} ago)",
        f"  final integration commit: {record.final_commit or '-'}",
        (
            f"  last event: {newest.ts} ({format_age(age_seconds(newest.ts, now=now))} ago)"
            if newest is not None
            else "  last event: -"
        ),
    ]
    reasons = _reasons(view)
    if reasons:
        lines.append(f"  reason: {'; '.join(reasons)}")
    ids = node_ids(view)
    if not ids:
        lines.append("  nodes: none recorded")
        return "\n".join(lines)
    header = ("NODE", "STATE", "ATTEMPTS", "OUTPUT", "BLOCKED")
    rows: list[list[str]] = []
    for node_id in ids:
        node = record.nodes.get(node_id)
        rows.append(_node_row(node_id, node))
    lines.append("")
    lines.append(_table(header, rows))
    return "\n".join(lines)


def _node_row(node_id: str, node: NodeRecord | None) -> list[str]:
    if node is None:
        return [node_id, "unknown", "0", "-", "-"]
    return [
        node_id,
        node.state,
        str(node.attempts),
        node.output_commit[:12] if node.output_commit else "-",
        node.blocked_reason or "-",
    ]


# --- listing ----------------------------------------------------------------------------------


def _list_runs(repo_root: Path, *, now: datetime) -> list[dict[str, Any]]:
    parent = runs_dir(repo_root)
    if not parent.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for child in parent.iterdir():
        if not child.is_dir():
            continue
        if not (child / RUN_JSON).is_file():
            # An incomplete directory (a crash before `run.json`) is still inspectable.
            entries.append(
                {
                    "run_id": child.name,
                    "state": "unreadable",
                    "created_at": None,
                    "age_s": None,
                    "owner_generation": None,
                    "config_hash": None,
                    "final_commit": None,
                    "nodes": [],
                    "reasons": [f"missing {RUN_JSON}"],
                    "last_event_ts": None,
                    "last_event_age_s": None,
                }
            )
            continue
        try:
            view = load_run(repo_root, child.name)
        except InspectError as exc:
            entries.append(
                {
                    "run_id": child.name,
                    "state": "unreadable",
                    "created_at": None,
                    "age_s": None,
                    "owner_generation": None,
                    "config_hash": None,
                    "final_commit": None,
                    "nodes": [],
                    "reasons": [str(exc)],
                    "last_event_ts": None,
                    "last_event_age_s": None,
                }
            )
            continue
        entries.append(_run_payload(view, now=now))

    def newest_first(entry: dict[str, Any]) -> tuple[str, str]:
        created = entry.get("created_at")
        stamp = created if isinstance(created, str) else ""
        run_id = entry.get("run_id")
        return (stamp, run_id if isinstance(run_id, str) else "")

    entries.sort(key=newest_first, reverse=True)
    return entries


def _render_list(entries: list[dict[str, Any]]) -> str:
    rows = [
        [
            str(entry["run_id"]),
            str(entry["state"]),
            str(len(entry["nodes"])),
            (str(entry["final_commit"])[:12] if entry["final_commit"] else "-"),
            format_age(entry["age_s"] if isinstance(entry["age_s"], int) else None),
        ]
        for entry in entries
    ]
    return _table(_COLUMNS, rows)


# --- output -----------------------------------------------------------------------------------


def _table(header: tuple[str, ...], rows: list[list[str]]) -> str:
    widths = [max(len(row[index]) for row in [list(header), *rows]) for index in range(len(header))]
    lines: list[str] = []
    for row in [list(header), *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _exit_for(exc: InspectError) -> Exit:
    text = str(exc)
    if "not found" in text or "no run.json" in text or "not a valid run id" in text:
        return Exit.INVALID
    return Exit.FAILURE


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
