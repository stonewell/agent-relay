"""Command line entry point for the local completion gateway."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from relay import __version__

from .commands import serve
from .errors import report_error
from .exitcodes import Exit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relay", description="Serve agent CLIs as a local completion API."
    )
    parser.add_argument("--version", "-V", action="version", version=__version__)
    parser.add_argument(
        "-C", dest="repo", metavar="PATH", default=".", help="run as if started in PATH"
    )
    commands = parser.add_subparsers(dest="command", metavar="<command>")
    serve.add_parser(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return report_error("relay: choose a command; run relay --help", Exit.INVALID, False)
    try:
        return serve.run(args, Path(args.repo).resolve())
    except KeyboardInterrupt:
        return report_error("relay: cancelled by operator", Exit.CANCELLED, False)
    except Exception as exc:  # noqa: BLE001 - the CLI boundary never emits a traceback
        return report_error(f"relay serve: {exc}", Exit.FAILURE, False)


if __name__ == "__main__":
    raise SystemExit(main())
