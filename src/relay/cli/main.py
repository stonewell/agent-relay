"""`relay` command line entry point (design section 14)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from relay import __version__

from .commands import agents as agents_command
from .commands import apply as apply_command
from .commands import clean as clean_command
from .commands import doctor, result, routes, run, status
from .commands import input as input_command
from .exitcodes import Exit
from .phases import COMMAND_PHASE, IMPLEMENTED, availability_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay", description="Route programming tasks to agent CLIs."
    )
    parser.add_argument("--version", "-V", action="version", version=__version__)
    parser.add_argument(
        "-C", dest="repo", metavar="PATH", default=".", help="run as if started in PATH"
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    agents_command.add_parser(commands)
    apply_command.add_parser(commands)
    clean_command.add_parser(commands)
    doctor.add_parser(commands)
    result.add_parser(commands)
    routes.add_parsers(commands)
    run.add_parser(commands)
    input_command.add_parsers(commands)
    status.add_parser(commands)
    for name in COMMAND_PHASE:
        if name not in IMPLEMENTED:
            stub = commands.add_parser(name, add_help=False)
            stub.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.command is None:
        parser.print_help(sys.stderr)
        return Exit.INVALID
    problem = availability_error(args.command)
    if problem is not None:
        print(problem, file=sys.stderr)
        return Exit.INVALID
    repo_root = Path(args.repo).resolve()
    if args.command in ("reply", "resume"):
        return input_command.run(args, repo_root)
    if args.command == "agents":
        return agents_command.run(args, repo_root)
    if args.command == "apply":
        return apply_command.run(args, repo_root)
    if args.command == "clean":
        return clean_command.run(args, repo_root)
    if args.command == "doctor":
        return doctor.run(args, repo_root)
    if args.command == "result":
        return result.run(args, repo_root)
    if args.command in ("routes", "explain"):
        return routes.run(args, repo_root)
    if args.command == "run":
        return run.run(args, repo_root)
    if args.command == "status":
        return status.run(args, repo_root)
    raise AssertionError(f"command {args.command} is marked implemented but has no handler")


if __name__ == "__main__":
    raise SystemExit(main())
