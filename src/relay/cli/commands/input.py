"""File-based foreground reply and resume commands (design sections 8.2 and 14)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import secrets
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import TypeAdapter

from relay.core.answers import current_questions, record_answers, validate_answers
from relay.core.registry import Config
from relay.core.run import run_foreground
from relay.core.runlock import RunLock
from relay.core.specs import load_spec
from relay.core.specs.answers import AnswerValue, Submission
from relay.core.specs.task import TaskSpec
from relay.core.store import RunStore, iso, read_text
from relay.core.yamlio import load_yaml

from .inspect_common import InspectError, check_run_id, load_run
from .run import _positive_int, _print_result  # pyright: ignore[reportPrivateUsage]


def add_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    reply = subparsers.add_parser("reply", help="answer a blocked node's questions")
    reply.add_argument("run_id")
    reply.add_argument("node_id")
    reply.add_argument("answer", nargs="?")
    reply.add_argument("--answers-file", metavar="FILE")
    reply.add_argument("--revision", type=_positive_int)
    reply.add_argument("--submission-id", default=None)
    reply.add_argument("--resume", action="store_true")
    reply.add_argument("--yes", action="store_true")
    reply.add_argument("--json", action="store_true")
    resume = subparsers.add_parser(
        "resume", help="resume an interrupted or answered run, or reopen a session in the TUI"
    )
    resume.add_argument("run_id", nargs="?")
    resume.add_argument(
        "--session", metavar="SESSION", help="reopen a saved session in the TUI (no RUN)"
    )
    resume.add_argument("--max-attempts", type=_positive_int)
    resume.add_argument("--non-interactive", action="store_true")
    resume.add_argument("--json", action="store_true")


def session_conflict(args: argparse.Namespace) -> int:
    """`relay resume RUN --session S`: a run and a session are mutually exclusive."""
    return _error(
        "relay resume: give either a run or --session, not both", 2, getattr(args, "json", False)
    )


def run(args: argparse.Namespace, repo_root: Path) -> int:
    if args.command == "resume" and args.run_id is None:
        return _error("relay resume: give a run, or --session SESSION to reopen one", 2, args.json)
    try:
        try:
            check_run_id(args.run_id)
        except InspectError as exc:
            raise ValueError(str(exc)) from exc
        store = RunStore(repo_root, args.run_id)
        if not store.run_dir.is_dir():
            raise ValueError(
                f"run {args.run_id!r} not found; run relay status to list available runs"
            )
        load_run(repo_root, args.run_id)

        if args.command == "reply":
            result = current_questions(store, args.node_id)
            if (args.answer is None) == (args.answers_file is None):
                raise ValueError("provide exactly one answer or --answers-file")
            answers: dict[str, AnswerValue]
            if args.answers_file is not None:
                raw = load_yaml(read_text(repo_root / args.answers_file))
                answers = TypeAdapter(dict[str, AnswerValue]).validate_python(raw)
            else:
                if len(result.open_questions) != 1:
                    raise ValueError("use --answers-file for multiple questions")
                answers = {result.open_questions[0].id: str(args.answer)}
            revision = args.revision or result.question_revision or 1
            validate_answers(result, answers)
            if revision != result.question_revision:
                raise ValueError("stale question revision")
            submission = Submission(
                submission_id=args.submission_id or secrets.token_hex(8),
                question_revision=revision,
                provenance="operator-cli",
                submitted_at=iso(datetime.now(UTC)),
                answers=answers,
            )
            if not args.yes:
                if not args.json:
                    print(
                        f"Record answers for {args.node_id} revision {revision}?", file=sys.stderr
                    )
                confirmed = False
                if not args.json and sys.stdin.isatty() and sys.stdout.isatty():
                    with contextlib.suppress(EOFError):
                        confirmed = input("Confirm [y/N] ").strip().lower() in ("y", "yes")
                if not confirmed:
                    return _error(
                        "Approval required; confirm with: "
                        + shlex.join(_confirmation_command(args)),
                        7,
                        args.json,
                    )
            with RunLock.acquire(repo_root, store.run_id):
                recorded = record_answers(store, args.node_id, submission)
            if not args.resume:
                print(json.dumps({"run_id": store.run_id, "recorded": recorded}))
                return 0
        record = store.read_run()
        if store.path("graph.marker").exists():
            from .graph import resume

            return resume(args, repo_root, store)
        if len(record.nodes) != 1:
            raise ValueError("foreground resume requires exactly one node")
        node_id = next(iter(record.nodes))
        task = load_spec(TaskSpec, store.node_dir(node_id) / "task.spec.yaml")
        limit = getattr(args, "max_attempts", None)
        if limit is not None:
            if limit < (task.budget.max_attempts or 1):
                raise ValueError("--max-attempts must not lower the total attempt limit")
            task.budget.max_attempts = limit
        config = Config.model_validate_json(read_text(store.path("config.snapshot.json")))
        diagnostics = io.StringIO()
        outcome = asyncio.run(
            run_foreground(
                repo_root,
                task,
                config=config,
                resume_run=store.run_id,
                retry_failed=getattr(args, "retry_failed", False),
                err=diagnostics,
                non_interactive=getattr(args, "non_interactive", False) or args.json,
                out=sys.stderr if args.json else sys.stdout,
            )
        )
        _print_result(outcome, as_json=args.json, diagnostics=diagnostics.getvalue())
        return outcome.exit_code
    except (ValueError, OSError, yaml.YAMLError) as exc:
        return _error(f"relay {args.command}: {exc}", 2, args.json)
    except Exception as exc:  # noqa: BLE001 - map storage/ownership failures at the CLI boundary
        return _error(f"relay {args.command}: {exc}", 1, args.json)


def _confirmation_command(args: argparse.Namespace) -> list[str]:
    command = ["relay", "-C", args.repo, "reply", args.run_id, args.node_id]
    if args.answer is not None:
        command.append(args.answer)
    if args.answers_file is not None:
        command.extend(["--answers-file", args.answers_file])
    for name in ("revision", "submission_id"):
        value = getattr(args, name)
        if value is not None:
            command.extend(["--" + name.replace("_", "-"), str(value)])
    if args.resume:
        command.append("--resume")
    if args.json:
        command.append("--json")
    return [*command, "--yes"]


def _error(message: str, code: int, as_json: bool) -> int:
    print(json.dumps({"error": message}) if as_json else message, file=sys.stderr)
    return code
