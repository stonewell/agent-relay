"""Single-node request/spec intake (design sections 8.2, 10 and 14; task 1.31)."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
from pathlib import Path

import yaml

from relay.core.classifier import detect_category
from relay.core.registry import ConfigError, load_config
from relay.core.run import RunResult, run_foreground
from relay.core.specs import load_spec
from relay.core.specs.common import PermissionLevel
from relay.core.specs.task import Commands, RunCheck, ScopeCheck, TaskSpec

from ..exitcodes import Exit

_NODE_ID = "n1"
"""The node id of a single-node run (the graph arrives with phase 2)."""

_LEVELS = ("full", "workspace", "readonly")


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("run", help="run one task in the foreground")
    parser.add_argument("request", nargs="?", metavar="REQUEST", help="the task to run")
    parser.add_argument("--task", metavar="FILE", help="load a YAML or JSON task spec")
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
    parser.add_argument(
        "--non-interactive", action="store_true", help="persist questions without prompting"
    )
    parser.add_argument("--json", action="store_true", help="print the run result as JSON")
    parser.add_argument(
        "--check",
        action="append",
        metavar="COMMAND",
        help="executable acceptance check (repeatable)",
    )
    for name in ("build", "test", "lint"):
        parser.add_argument(f"--{name}", metavar="COMMAND", help=f"default {name} command")
    parser.add_argument("--verify", choices=("default", "none"), help="verification policy")
    parser.add_argument(
        "--verification-skip-reason", metavar="TEXT", help="reason for --verify none"
    )
    parser.add_argument(
        "--max-attempts", type=_positive_int, metavar="N", help="total attempt limit"
    )
    parser.add_argument(
        "--timeout", type=_positive_int, metavar="SECONDS", help="per-attempt timeout"
    )


def run(args: argparse.Namespace, repo_root: Path) -> int:
    try:
        config = load_config(project_root=repo_root).config
        task = _task_spec(args, repo_root)
        cli_permissions = _level(args.permissions)
        agent_permissions = _parse_agent_permissions(args.agent_permissions)
    except (ConfigError, ValueError, OSError, yaml.YAMLError) as exc:
        return _fail(f"relay run: {exc}", Exit.INVALID, args.json)

    diagnostics = io.StringIO()
    try:
        result = asyncio.run(
            run_foreground(
                repo_root,
                task,
                config=config,
                err=diagnostics,
                non_interactive=args.non_interactive or args.json,
                cli_permissions=cli_permissions,
                agent_permissions=agent_permissions,
                model_tier=args.model_tier,
                allow_dirty=args.allow_dirty,
                exclude=tuple(args.exclude),
                out=sys.stderr if args.json else sys.stdout,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the CLI never prints a traceback for a run
        return _fail(f"relay run: {exc}", Exit.FAILURE, args.json)

    _print_result(result, as_json=args.json, diagnostics=diagnostics.getvalue())
    return int(Exit(result.exit_code))


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _task_spec(args: argparse.Namespace, repo_root: Path) -> TaskSpec:
    request: str | None = args.request
    if (request is None) == (args.task is None):
        raise ValueError("provide exactly one REQUEST or --task FILE")
    if args.task is not None:
        task = load_spec(TaskSpec, repo_root / args.task)
    else:
        if request is None or not request.strip():
            raise ValueError("request must not be empty")
        task = TaskSpec(run_id="run", id=_NODE_ID, category=detect_category(request), goal=request)
    if args.category is not None:
        task.category = args.category
    if args.agent is not None:
        task.routing.agent = args.agent
    if args.check is not None:
        task.acceptance = [entry for entry in task.acceptance if isinstance(entry, ScopeCheck)]
        task.acceptance.extend(RunCheck(run=command) for command in args.check)
    if any(getattr(args, name) is not None for name in ("build", "test", "lint")):
        commands = task.commands.model_copy() if task.commands is not None else Commands()
        for name in ("build", "test", "lint"):
            value: str | None = getattr(args, name)
            if value is not None:
                setattr(commands, name, value)
        task.commands = commands
    if args.verify is not None:
        task.verify = args.verify
    if args.verification_skip_reason is not None:
        task.verification_skip_reason = args.verification_skip_reason
    if args.max_attempts is not None:
        task.budget.max_attempts = args.max_attempts
    if args.timeout is not None:
        task.budget.timeout_s = args.timeout

    # Mutations above must pass the same validation as a newly loaded spec.
    task = TaskSpec.model_validate(task.model_dump(mode="json", by_alias=True))
    if task.depends_on or task.context.from_:
        raise ValueError("task dependencies and context.from are available from phase 2")
    if (
        task.repo.root != "."
        or task.repo.base_branch is not None
        or task.repo.input_commit is not None
    ):
        raise ValueError(
            "select the repository with -C; task.repo must use root '.' without base pins"
        )
    if task.output.result != "result.spec.yaml" or task.output.artifacts_dir != "artifacts/":
        raise ValueError("custom output channels are not supported by foreground runs yet")
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


def _print_result(result: RunResult, *, as_json: bool, diagnostics: str = "") -> None:
    if result.exit_code == Exit.BLOCKED and diagnostics:
        print(diagnostics, file=sys.stderr, end="")
    if as_json:
        print(json.dumps(result.to_json(), indent=2, ensure_ascii=False))
        return
    if not as_json and result.exit_code != Exit.OK:
        print(
            result.error or "; ".join(result.reasons) or f"run ended with exit {result.exit_code}",
            file=sys.stderr,
        )
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
