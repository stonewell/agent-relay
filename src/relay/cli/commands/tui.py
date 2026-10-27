"""`relay tui`, bare `relay` on a terminal and `relay resume --session` (design section 14.1).

Textual is imported only inside :func:`launch`, so headless commands, `--json`, `--help` and
`--version` never import or initialize it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from relay.core.sessions import SessionError, SessionStore

from ..errors import report_error
from ..exitcodes import Exit

NOT_A_TERMINAL = (
    "relay {command}: the interactive interface needs a terminal on standard input and output; "
    "use the headless commands (relay run, relay graph run, relay status, ...) instead"
)


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("tui", help="open the conversational terminal interface")
    parser.add_argument("--session", metavar="SESSION", help="reopen a saved session")


def interactive() -> bool:
    """Both standard input and standard output are terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def launch(repo_root: Path, session_id: str | None) -> int:
    from relay.tui.app import run_tui  # lazy: the only place Textual is imported

    return run_tui(repo_root, session_id=session_id)


def run(args: argparse.Namespace, repo_root: Path, *, command: str = "tui") -> int:
    if getattr(args, "json", False):
        return report_error(
            f"relay {command}: the interactive interface has no --json mode", Exit.INVALID, True
        )
    if not interactive():
        return report_error(NOT_A_TERMINAL.format(command=command), Exit.INVALID, False)
    session_id: str | None = getattr(args, "session", None)
    if session_id is not None:
        # Not `SessionStore.open`: it sweeps temp files, which would race a live owner.
        try:
            found = (SessionStore(repo_root, session_id).dir / "session.json").is_file()
        except SessionError as exc:
            return report_error(f"relay {command}: {exc}", Exit.INVALID, False)
        if not found:
            return report_error(
                f"relay {command}: session {session_id} not found", Exit.INVALID, False
            )
    return launch(repo_root, session_id)
