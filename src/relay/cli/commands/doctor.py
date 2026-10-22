"""`relay doctor` (design section 14)."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from relay.core.doctor import DoctorContext, default_registry, run_checks

from ..exitcodes import Exit


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser(
        "doctor", help="check this machine, the configuration and agents"
    )
    parser.add_argument("--paid", action="store_true", help="also run checks that call a model")
    parser.add_argument(
        "--check", action="append", default=[], metavar="ID", help="run only this check"
    )
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    ctx = DoctorContext(repo_root=repo_root)
    registry = default_registry(ctx)
    try:
        report = asyncio.run(
            run_checks(registry, ctx, include_paid=args.paid, only=tuple(args.check))
        )
    except KeyError as exc:
        known = ", ".join(c.id for c in registry)
        print(f"relay doctor: unknown check {exc.args[0]!r} (known: {known})")
        return Exit.INVALID
    print(report.to_json() if args.json else report.render_text())
    return Exit.FAILURE if report.exit_code else Exit.OK
