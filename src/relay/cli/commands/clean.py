"""``relay clean`` (design sections 6, 8.6 and 14; task 1.22).

Prunes what relay owns for one run (or every run): registered node worktrees under the run's
worktree directory, the per-run scratch directory, and -- only with ``--purge-runs`` -- the run
directory and the git refs under ``refs/relay/<run>/`` and ``refs/heads/relay/<run>/``.

What is removed, in which order, and when a run is refused are the rules of
:mod:`relay.core.run_purge`, shared with confirmed session deletion; this module selects runs,
asks for confirmation and reports. In short:

* Only worktrees git reports as registered and that live under the run's own worktree
  directory (or, for purge, under the run directory) are removed. The main worktree and any
  unrelated path are never touched; :class:`relay.core.worktree.WorktreeManager` enforces that
  again for each removal.
* A live run is never cleaned. The phase 1 lock file is *inspected* only: ``clean`` never
  creates, replaces or unlinks a lock object (design section 6), so a purged run id stays
  unusable while its lock object exists. Nothing is swept or written in a run it skips.
* Refs go before the run record, and the record is kept when an earlier removal failed, so a
  failed purge can be repeated. A run branch that a kept worktree (the main one included) has
  checked out is shown as blocked, never deleted, and keeps the record (exit ``1``).
* ``--purge-runs`` is privileged (design section 8.6). Without ``--yes`` an interactive
  session is asked to confirm; a non-interactive session exits ``7`` and changes nothing. The
  exact confirming command is printed. ``--dry-run`` prints the plan and changes nothing.

Exit codes are those of design section 14.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from relay.core import registry as registry_module
from relay.core.registry import ConfigError, load_config
from relay.core.run_purge import (
    Removal,
    RunPurge,
    owner_state,
    plan_run_purge,
    purge_run,
    registered_worktrees,
    run_worktree_dir,
)
from relay.core.runlock import OwnerState
from relay.core.sessions import protected_run_ids
from relay.core.store import runs_dir
from relay.core.worktree import WorktreeError

from ..exitcodes import Exit
from .inspect_common import (
    InspectError,
    age_seconds,
    check_run_id,
    display_path,
    load_run,
    run_directory,
)

_DURATION_RE = re.compile(r"^(\d+)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}


class CleanError(RuntimeError):
    """A destructive plan cannot be built; the message is the complete explanation."""

    def __init__(self, message: str, code: Exit = Exit.INVALID) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _RunPlan:
    run_id: str
    state: str | None
    worktree_dir: Path
    worktrees: list[Removal] = field(default_factory=list[Removal])
    scratch: Removal | None = None
    run_dir: Removal | None = None
    refs: list[str] = field(default_factory=list[str])
    blocked_refs: dict[str, Path] = field(default_factory=dict[str, Path])
    """Run branches a kept worktree has checked out: never deleted; they keep the record."""
    skipped: str | None = None
    active: bool = False
    error: str | None = None
    note: str | None = None
    purge: RunPurge | None = None
    """The shared removal plan (None when the run is skipped for its owner)."""


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("clean", help="prune run worktrees and scratch directories")
    parser.add_argument(
        "run_id", nargs="?", metavar="RUN", help="only this run (default: every inactive run)"
    )
    parser.add_argument(
        "--older-than",
        metavar="DURATION",
        help="only runs older than this duration (for example 24h or 7d)",
    )
    parser.add_argument(
        "--purge-runs",
        action="store_true",
        help="also delete run records and refs (privileged; needs confirmation or --yes)",
    )
    parser.add_argument("--yes", action="store_true", help="confirm --purge-runs without prompting")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be removed, then stop"
    )
    parser.add_argument("--json", action="store_true", help="print the clean plan as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    as_json = bool(args.json)
    try:
        older = _parse_duration(args.older_than)
    except ValueError as exc:
        return _fail(f"relay clean: {exc}", Exit.INVALID, as_json)

    try:
        plans = _plan(repo_root, args.run_id, older, purge=bool(args.purge_runs))
    except CleanError as exc:
        return _fail(f"relay clean: {exc}", exc.code, as_json)
    except (ConfigError, WorktreeError, InspectError, OSError) as exc:
        return _fail(f"relay clean: {exc}", Exit.INVALID, as_json)

    if args.dry_run:
        _print_plan(plans, repo_root, as_json, dry_run=True)
        return int(Exit.OK)

    if args.run_id is not None and plans and plans[0].active:
        plan = plans[0]
        return _fail(
            f"relay clean: run {plan.run_id}: {plan.skipped}; it was not cleaned",
            Exit.FAILURE,
            as_json,
        )

    if bool(args.purge_runs) and _has_destructive_work(plans) and not bool(args.yes):
        _print_summary(plans, repo_root, to_stderr=True)
        if not _interactive() or not _confirmed():
            return _fail(_approval_message(args), Exit.APPROVAL_REQUIRED, as_json)
    _execute(plans, repo_root)

    _print_plan(plans, repo_root, as_json, dry_run=False)
    return int(Exit.FAILURE if any(plan.error for plan in plans) else Exit.OK)


# --- planning ---------------------------------------------------------------------------------


def _plan(
    repo_root: Path, run_id: str | None, older: timedelta | None, *, purge: bool
) -> list[_RunPlan]:
    ids = _selected_ids(repo_root, run_id)
    if not ids:
        return []
    loaded = load_config(project_root=repo_root, user_config=registry_module.USER_CONFIG)
    plans: list[_RunPlan] = []
    retained = protected_run_ids(repo_root) if purge else set[str]()
    listed: list[list[Path]] = []

    def registered() -> list[Path]:
        if not listed:  # listed once, and only when a run is not skipped for its owner
            listed.append(registered_worktrees(repo_root))
        return listed[0]

    for candidate in ids:
        if candidate in retained and run_id is not None:
            raise CleanError(
                f"run {candidate} is referenced by a retained session and cannot be purged; "
                "delete the session first",
                Exit.FAILURE,
            )
        plan = _plan_run(
            repo_root,
            candidate,
            loaded.config.paths.worktree_root,
            purge=purge and candidate not in retained,
            registered=registered,
        )
        if candidate in retained:
            plan.note = (
                "referenced by a retained session; worktrees and scratch cleaned, record kept"
            )
        if older is not None and plan.skipped is None:
            age = _run_age(repo_root, candidate)
            if age is not None and age < older.total_seconds():
                plan.skipped = f"newer than --older-than {_format_duration(older)}"
        plans.append(plan)
    return plans


def _selected_ids(repo_root: Path, run_id: str | None) -> list[str]:
    if run_id is not None:
        check_run_id(run_id)
        if not run_directory(repo_root, run_id).is_dir():
            raise InspectError(
                f"run {run_id!r} not found under {runs_dir(repo_root)}; "
                "run `relay status` to list available runs"
            )
        return [run_id]
    parent = runs_dir(repo_root)
    if not parent.is_dir():
        return []
    return sorted(child.name for child in parent.iterdir() if child.is_dir())


def _plan_run(
    repo_root: Path,
    run_id: str,
    worktree_root: str,
    *,
    purge: bool,
    registered: Callable[[], list[Path]],
) -> _RunPlan:
    # A pure path computation: exactly this run's directory in either the POSIX or the short
    # Windows layout, without touching git or the disk.
    worktree_dir = run_worktree_dir(repo_root, run_id, worktree_root)
    plan = _RunPlan(run_id=run_id, state=None, worktree_dir=worktree_dir)

    try:
        view = load_run(repo_root, run_id)
        plan.state = view.record.state
    except InspectError as exc:
        # A purge is the documented way to remove an incomplete or unreadable run, so the
        # missing record is not an error when the run directory itself is about to go.
        if not purge:
            plan.error = str(exc)

    owner = owner_state(repo_root, run_id)
    if owner in (OwnerState.ALIVE, OwnerState.UNKNOWN):
        plan.active = True
        plan.skipped = (
            "run is in progress" if owner is OwnerState.ALIVE else "run owner cannot be checked"
        )
        return plan

    shared = plan_run_purge(repo_root, run_id, worktree_root, record=purge, registered=registered())
    plan.purge = shared
    plan.worktrees, plan.scratch, plan.run_dir = shared.worktrees, shared.scratch, shared.record
    plan.refs = shared.refs
    plan.blocked_refs = shared.blocked_refs
    return plan


def _run_age(repo_root: Path, run_id: str) -> int | None:
    try:
        view = load_run(repo_root, run_id)
    except InspectError:
        try:
            modified = run_directory(repo_root, run_id).stat().st_mtime
        except OSError:
            return None
        return max(0, int(datetime.now(UTC).timestamp() - modified))
    age = age_seconds(view.record.created_at)
    if age is not None:
        return age
    try:
        modified = view.directory.stat().st_mtime
    except OSError:
        return None
    return max(0, int(datetime.now(UTC).timestamp() - modified))


def _parse_duration(value: str | None) -> timedelta | None:
    if value is None:
        return None
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("--older-than must be a duration such as 30m, 24h, 7d or 2w")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("--older-than must be greater than zero")
    return timedelta(seconds=amount * _UNITS[match.group(2)])


def _format_duration(value: timedelta) -> str:
    seconds = int(value.total_seconds())
    for unit, size in (("w", 604_800), ("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


# --- execution --------------------------------------------------------------------------------


def _execute(plans: list[_RunPlan], repo_root: Path) -> None:
    for plan in plans:
        if plan.skipped is not None or plan.purge is None:
            continue
        purge_run(repo_root, plan.purge)  # the shared rules; outcomes land in the removals
        problems = list(plan.purge.ref_errors)
        if plan.run_dir is not None and plan.run_dir.error is not None:
            problems.append(f"run directory {plan.run_dir.error}")
        if problems:
            plan.error = "; ".join([plan.error, *problems] if plan.error else problems)


# --- confirmation -----------------------------------------------------------------------------


def _has_destructive_work(plans: list[_RunPlan]) -> bool:
    for plan in plans:
        if plan.skipped is not None:
            continue
        if plan.worktrees or plan.scratch is not None or plan.run_dir is not None or plan.refs:
            return True
    return False


def _interactive() -> bool:
    return sys.stdin.isatty()


def _confirmed() -> bool:
    sys.stderr.write("\nPurge these run records? [y/N] ")
    sys.stderr.flush()
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _approval_message(args: argparse.Namespace) -> str:
    command = _confirmation_command(args)
    return (
        "relay clean: approval required; the purge was prepared but not confirmed. "
        f"Nothing was changed.\nConfirm with: {command}"
    )


def _confirmation_command(args: argparse.Namespace) -> str:
    parts = ["relay", "clean"]
    if args.run_id is not None:
        parts.append(str(args.run_id))
    if args.older_than is not None:
        parts.extend(["--older-than", str(args.older_than)])
    parts.extend(["--purge-runs", "--yes"])
    if bool(args.json):
        parts.append("--json")
    return shlex.join(parts)


# --- output -----------------------------------------------------------------------------------


def _print_summary(plans: list[_RunPlan], repo_root: Path, *, to_stderr: bool) -> None:
    lines = _summary_lines(plans, repo_root)
    stream = sys.stderr if to_stderr else sys.stdout
    if lines:
        print("\n".join(lines), file=stream)


def _print_plan(plans: list[_RunPlan], repo_root: Path, as_json: bool, *, dry_run: bool) -> None:
    if as_json:
        _print_json(
            {
                "dry_run": dry_run,
                "runs": [_plan_json(plan, repo_root, dry_run=dry_run) for plan in plans],
                "removed": _counts(plans),
                "errors": [
                    f"{plan.run_id}: {plan.error}" for plan in plans if plan.error is not None
                ],
            }
        )
        return
    lines = _summary_lines(plans, repo_root, dry_run=dry_run)
    if not lines:
        print("no runs to clean")
        return
    print("\n".join(lines))


def _summary_lines(plans: list[_RunPlan], repo_root: Path, *, dry_run: bool = False) -> list[str]:
    verb = "would remove" if dry_run else "removed"
    lines: list[str] = []
    for plan in plans:
        if plan.skipped is not None:
            lines.append(f"run {plan.run_id}: skipped ({plan.skipped})")
            continue
        if plan.note is not None:
            lines.append(f"run {plan.run_id}: {plan.note}")
        if (
            plan.state is None
            and not plan.worktrees
            and plan.scratch is None
            and plan.run_dir is None
        ):
            continue
        state = plan.state or "unknown"
        counts = _plan_counts(plan)
        scratch_word = "directory" if counts["scratch"] == 1 else "directories"
        runs_word = "directory" if counts["run_directories"] == 1 else "directories"
        lines.append(
            f"run {plan.run_id} ({state}): {verb} {counts['worktrees']} worktree(s), "
            f"{counts['scratch']} scratch {scratch_word}, "
            f"{counts['run_directories']} run {runs_word}, "
            f"{counts['refs']} ref(s)"
        )
        for removal in plan.worktrees:
            lines.append(f"  {_removal_line('worktree', removal, repo_root, dry_run=dry_run)}")
        if plan.scratch is not None:
            lines.append(f"  {_removal_line('scratch', plan.scratch, repo_root, dry_run=dry_run)}")
        if plan.run_dir is not None:
            lines.append(
                f"  {_removal_line('run directory', plan.run_dir, repo_root, dry_run=dry_run)}"
            )
        for ref in plan.refs:
            lines.append(f"  ref {ref}")
        for ref, holder in plan.blocked_refs.items():
            lines.append(
                f"  ref {ref} (blocked: checked out in {_worktree_label(holder, repo_root)}; "
                "kept with the run record)"
            )
        if plan.error is not None:
            lines.append(f"  error: {plan.error}")
    return lines


def _worktree_label(path: Path, repo_root: Path) -> str:
    shown = display_path(path, repo_root)
    return "the main worktree" if shown == "." else f"the worktree {shown}"


def _removal_line(kind: str, removal: Removal, repo_root: Path, *, dry_run: bool) -> str:
    if removal.error is not None:
        status = f"failed: {removal.error}"
    elif dry_run:
        status = "would remove"
    else:
        status = "removed" if removal.removed else "not found"
    return f"{kind} {display_path(removal.path, repo_root)} ({status})"


def _plan_json(plan: _RunPlan, repo_root: Path, *, dry_run: bool) -> dict[str, Any]:
    return {
        "run_id": plan.run_id,
        "state": plan.state,
        "skipped": plan.skipped,
        "note": plan.note,
        "worktrees": [
            {
                "path": display_path(removal.path, repo_root),
                "candidate": removal.candidate,
                "removed": removal.removed and not dry_run,
                "error": removal.error,
            }
            for removal in plan.worktrees
        ],
        "scratch": _removal_json(plan.scratch, repo_root, dry_run=dry_run),
        "run_directory": _removal_json(plan.run_dir, repo_root, dry_run=dry_run),
        "refs": list(plan.refs),
        "blocked_refs": [
            {"ref": ref, "worktree": display_path(holder, repo_root)}
            for ref, holder in plan.blocked_refs.items()
        ],
        "error": plan.error,
    }


def _removal_json(
    removal: Removal | None, repo_root: Path, *, dry_run: bool
) -> dict[str, Any] | None:
    if removal is None:
        return None
    return {
        "path": display_path(removal.path, repo_root),
        "removed": removal.removed and not dry_run,
        "error": removal.error,
    }


def _plan_counts(plan: _RunPlan) -> dict[str, int]:
    return {
        "worktrees": len(plan.worktrees),
        "scratch": 0 if plan.scratch is None else 1,
        "run_directories": 0 if plan.run_dir is None else 1,
        "refs": len(plan.refs),
    }


def _counts(plans: list[_RunPlan]) -> dict[str, int]:
    totals = {"worktrees": 0, "scratch": 0, "run_directories": 0, "refs": 0}
    for plan in plans:
        if plan.skipped is not None:
            continue
        for key, value in _plan_counts(plan).items():
            totals[key] += value
    return totals


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
