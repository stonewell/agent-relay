"""`relay run` (design sections 8.2 and 14, task 1.19 slice A).

One node, one attempt, in the foreground. The CLI builds a single-node task spec from the
request text, loads the merged configuration, and hands off to
:func:`relay.core.run.run_foreground`, which owns routing, the attempt state machine, the
durable run store and the interim run lock. Exit codes are the plain ints of design
section 14, mapped here onto :class:`relay.cli.exitcodes.Exit`.

Out of scope for slice A: `--concurrency`/`--dry-run` (graphs), `--non-interactive`/needs_input
(task 1.21), retries/escalation/fallback (task 1.19b).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from relay.core.classifier import detect_category
from relay.core.registry import ConfigError, load_config
from relay.core.run import RunResult, run_foreground
from relay.core.specs.common import PermissionLevel
from relay.core.specs.task import TaskSpec

from ..exitcodes import Exit

_NODE_ID = "n1"
"""The node id of a single-node run (the graph arrives with phase 2)."""

_LEVELS = ("full", "workspace", "readonly")


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("run", help="run one task in the foreground")
    parser.add_argument("request", metavar="REQUEST", help="the task to run")
    parser.add_argument("--agent", metavar="A", help="pin the node to this agent")
    parser.add_argument(
        "--category", metavar="C", help="task category (default: classified from the request)"
    )
    parser.add_argument(
        "--permissions",
        choices=_LEVELS,
        default=None,
        help="cap the node at this permission level",
    )
    parser.add_argument(
        "--agent-permissions",
        metavar="A=LEVEL,...",
        help="per-agent permission caps (for example codex=workspace,claude=full)",
    )
    parser.add_argument(
        "--model-tier",
        choices=("fast", "balanced", "deep"),
        help="select a model tier instead of the routed default",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="snapshot uncommitted changes as the run's base (section 8.3)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="omit paths matching GLOB from the snapshot (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print the run result as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    try:
        config = load_config(project_root=repo_root).config
        task = _task_spec(args)
        cli_permissions = _level(args.permissions)
        agent_permissions = _parse_agent_permissions(args.agent_permissions)
    except (ConfigError, ValueError) as exc:
        return _fail(f"relay run: {exc}", Exit.INVALID, args.json)

    try:
        result = asyncio.run(
            run_foreground(
                repo_root,
                task,
                config=config,
                cli_permissions=cli_permissions,
                agent_permissions=agent_permissions,
                model_tier=args.model_tier,
                allow_dirty=args.allow_dirty,
                exclude=tuple(args.exclude),
            )
        )
    except Exception as exc:  # noqa: BLE001 - the CLI never prints a traceback for a run
        return _fail(f"relay run: {exc}", Exit.FAILURE, args.json)

    _print_result(result, as_json=args.json)
    return int(Exit(result.exit_code))


def _task_spec(args: argparse.Namespace) -> TaskSpec:
    request: str = args.request
    if not request.strip():
        raise ValueError("request must not be empty")
    category = args.category if args.category is not None else detect_category(request)
    task = TaskSpec(run_id="run", id=_NODE_ID, category=category, goal=request)
    if args.agent is not None:
        task.routing.agent = args.agent
    return task


def _level(value: str | None) -> PermissionLevel | None:
    if value is None:
        return None
    if value not in _LEVELS:
        raise ValueError(f"unknown permission level {value!r}")
    return value


def _parse_agent_permissions(value: str | None) -> dict[str, PermissionLevel] | None:
    if value is None:
        return None
    parsed: dict[str, PermissionLevel] = {}
    for item in value.split(","):
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"agent permission {item!r} must be NAME=LEVEL")
        name, _, raw = item.partition("=")
        name = name.strip()
        if not name:
            raise ValueError(f"agent permission {item!r} has an empty agent name")
        level = raw.strip()
        if level not in _LEVELS:
            raise ValueError(f"agent permission {item!r} has unknown level {level!r}")
        parsed[name] = level
    return parsed or None


def _print_result(result: RunResult, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_json(), indent=2, ensure_ascii=False))
        return
    if result.exit_code == Exit.OK:
        print(
            f"run {result.run_id} node {result.node_id}: {result.node_status} "
            f"(output {result.output_commit}, integration {result.integration_commit})"
        )


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
