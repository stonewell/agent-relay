"""Foreground graph planning, execution and cancellation (design section 14)."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

import yaml

from relay.core.graph import GraphResult, run_graph
from relay.core.planner import TEMPLATES, plan
from relay.core.registry import Config, load_config
from relay.core.sessions import SessionError, session_guard
from relay.core.specs import Graph, load_spec
from relay.core.store import RunStore, atomic_write, read_text

from ..errors import report_error
from .inspect_common import check_run_id
from .run import _positive_int  # pyright: ignore[reportPrivateUsage]


def add_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    planner = subparsers.add_parser("plan", help="generate a foreground graph without executing")
    planner.add_argument("request")
    planner.add_argument("--template", choices=TEMPLATES, default="feature")
    planner.add_argument("--check", action="append", default=[])
    planner.add_argument("--output", default="graph.yaml", metavar="FILE")
    planner.add_argument("--json", action="store_true")
    graph = subparsers.add_parser("graph", help="execute a foreground task graph")
    commands = graph.add_subparsers(dest="graph_command", required=True)
    run = commands.add_parser("run")
    run.add_argument("file", metavar="FILE")
    run.add_argument("--concurrency", type=_positive_int)
    run.add_argument("--allow-dirty", action="store_true")
    run.add_argument("--non-interactive", action="store_true")
    run.add_argument("--json", action="store_true")
    cancel = subparsers.add_parser("cancel", help="request foreground graph cancellation")
    cancel.add_argument("run_id")
    cancel.add_argument("node", nargs="?")
    cancel.add_argument("--yes", action="store_true")
    cancel.add_argument("--json", action="store_true")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    try:
        if args.command == "plan":
            graph = plan(args.request, template=args.template, checks=tuple(args.check))
            target = repo_root / args.output
            if target.exists():
                raise ValueError("output file already exists; choose another --output")
            text = yaml.safe_dump(
                graph.model_dump(mode="json", by_alias=True, exclude_unset=True), sort_keys=False
            )
            # Exclusive create: planning never overwrites a user-edited graph.
            with target.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            print(
                json.dumps(
                    {"file": args.output, "graph": graph.model_dump(mode="json", by_alias=True)}
                )
                if args.json
                else f"Wrote {args.output}; review routing, scopes and verification before running."
            )
            return 0
        if args.command == "cancel":
            return _cancel(args, repo_root)
        graph = load_spec(Graph, repo_root / args.file)
        config = load_config(repo_root).config
        outcome = asyncio.run(
            run_graph(
                repo_root,
                graph,
                config=config,
                concurrency=args.concurrency,
                allow_dirty=args.allow_dirty,
            )
        )
        _display(outcome, args.json)
        return outcome.exit_code
    except (ValueError, OSError, yaml.YAMLError) as exc:
        return report_error(f"relay {args.command}: {exc}", 2, args.json)


def resume(args: argparse.Namespace, repo_root: Path, store: RunStore) -> int:
    record = store.read_run()
    if record.graph is None:
        raise ValueError("run has no recorded graph")
    config = Config.model_validate_json(read_text(store.path("config.snapshot.json")))
    outcome = asyncio.run(
        run_graph(
            repo_root,
            record.graph,
            config=config,
            resume_run=store.run_id,
            max_attempts=getattr(args, "max_attempts", None),
        )
    )
    _display(outcome, args.json)
    return outcome.exit_code


def _display(outcome: GraphResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(outcome.to_json()))
        return
    print(f"Run {outcome.run_id}: {outcome.state}")
    for key, value in outcome.nodes.items():
        print(f"  {key}: {value.state}" + (f" ({value.reason})" if value.reason else ""))
    if outcome.error:
        print(outcome.error)


def _cancel(args: argparse.Namespace, repo: Path) -> int:
    check_run_id(args.run_id)
    store = RunStore(repo, args.run_id)
    if not store.run_json.exists():
        raise ValueError(f"run {args.run_id!r} not found")
    if not store.path("graph.marker").exists():
        raise ValueError(
            "relay cancel supports foreground graphs; use Ctrl-C for a single-node run"
        )
    record = store.read_run()
    if args.node is not None and args.node not in record.nodes:
        raise ValueError("unknown node")
    if args.node is not None:
        if record.graph is None or args.node not in {node.id for node in record.graph.nodes}:
            raise ValueError(
                "implicit workers cannot be cancelled individually; "
                "cancel the owning graph node or the whole run"
            )
        if record.nodes[args.node].state not in {"pending", "running"}:
            raise ValueError(
                f"node is {record.nodes[args.node].state}; "
                "cancel an active graph node or the whole run"
            )
    if not args.yes:
        confirmed = False
        if not args.json and sys.stdin.isatty():
            confirmed = input(f"Cancel run {store.run_id}? [y/N] ").strip().lower() in {"y", "yes"}
        if not confirmed:
            import shlex

            command = ["relay", "-C", args.repo, "cancel", args.run_id]
            if args.node:
                command.append(args.node)
            command.append("--yes")
            if args.json:
                command.append("--json")
            return report_error(
                "Approval required; confirm with: " + shlex.join(command), 7, args.json
            )
    if record.state != "running":
        raise ValueError(f"run is {record.state}; no active foreground run to cancel")
    try:
        with session_guard(repo, store.run_id):
            directory = store.path("cancel-requests")
            directory.mkdir(exist_ok=True)
            atomic_write(
                directory / f"{secrets.token_hex(8)}.json", json.dumps({"node": args.node})
            )
    except SessionError as exc:
        raise ValueError(str(exc)) from exc
    print(
        json.dumps({"run_id": store.run_id, "node": args.node, "cancellation_requested": True})
        if args.json
        else f"Cancellation requested for {store.run_id}"
    )
    return 0
