"""`orch` command line entry point (design section 14)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from orch import __version__

from .commands import doctor
from .exitcodes import Exit
from .phases import COMMAND_PHASE, IMPLEMENTED, availability_error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orch", description="Route programming tasks to agent CLIs."
    )
    parser.add_argument("--version", "-V", action="version", version=__version__)
    parser.add_argument(
        "-C", dest="repo", metavar="PATH", default=".", help="run as if started in PATH"
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    doctor.add_parser(commands)
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
    if args.command == "doctor":
        return doctor.run(args, repo_root)
    raise AssertionError(f"command {args.command} is marked implemented but has no handler")


if __name__ == "__main__":
    raise SystemExit(main())
