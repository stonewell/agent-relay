"""`relay apply` (design sections 8.3, 8.6 and 14; task 1.24).

The privileged delivery step. It resolves the run's integration commit into a prepared target
with :func:`relay.core.apply.prepare_apply`, runs the run's integration acceptance checks against
the exact prepared tree in a disposable checkout (design section 10 item 4), asks for
confirmation, installs the prepared commit with the compare-and-swap ref update of
:func:`relay.core.apply.commit_ref`, and -- only when the target is the current branch -- updates
the delivered paths in the working tree and index.

Order matters:

1. Preparation never touches the user's checkout. A merge conflict, a dirty overlap or an
   invalid run is refused before anything runs or changes (design section 8.3).
2. Integration verification runs on the prepared tree, not on the target tip, because target
   edits made after the run can regress a clean textual merge. Its commands are the run's
   recorded graph ``integration_acceptance`` (or graph ``build``/``test``/``lint`` defaults);
   when the run records none, the delivery proceeds and the verification outcome is reported as
   ``not_run``. ``verify: none`` with its recorded reason is reported as ``skipped``. This
   reuses :mod:`relay.core.verify`; there is no second verification engine. A failed check exits
   :attr:`relay.cli.exitcodes.Exit.VERIFICATION_FAILED` and changes nothing, even with
   ``--force``.
3. Confirmation comes after verification. ``--yes`` bypasses the prompt (tests, automation);
   without it a non-interactive session is refused with exit 7 and the exact confirming command,
   and a declined prompt is the same. Exit 7 changes no state (design section 14).
4. The plan is prepared again immediately before installation. A target or tip that changed while
   the prompt was open invalidates the confirmation (exit 7); a dirty overlap found at that point
   is the ordinary dirty-overlap abort. The compare-and-swap in :func:`commit_ref` guards the
   remaining race. No user branch, HEAD, index or file is touched before that point.
5. After the ref moves, only the delivered paths are restored from the prepared commit
   (``git restore --source=... --staged --worktree -- <paths>``). Non-overlapping staged,
   unstaged and untracked user edits are preserved. A new or non-current ``--branch`` target is
   never checked out, so the current checkout and index stay untouched.

Events use the catalog names of design section 11: ``apply.started {op_id, target, commit}``,
``apply.progress {op_id, step}`` and ``apply.finished {op_id, status, error?}`` with status
``applied``, ``refused`` or ``failed``. They are appended to the run's ``events.jsonl``; the log
is per run, so the run id is implicit, and payloads carry no answer text or command output.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from relay.core.apply import (
    ApplyError,
    ApplyPlan,
    DirtyOverlapError,
    GitApplyError,
    IntegrationMissingError,
    MergeConflictError,
    RunMissingError,
    RunNotDoneError,
    TargetMovedError,
    TargetRefError,
    commit_ref,
    prepare_apply,
)
from relay.core.capture import CaptureResult, Change, ChangeCounts
from relay.core.events import EventLog, EventLogCorrupt
from relay.core.sessions import SessionError, session_guard
from relay.core.specs.graph import Graph
from relay.core.specs.task import Acceptance, Commands, TaskCore
from relay.core.store import RunStore, StoreError
from relay.core.verify import (
    VerificationError,
    VerifyResult,
    effective_scope,
    executable_commands,
    scope_violations,
    verify,
)

from ..exitcodes import Exit

_FORCE_UNSUPPORTED = (
    "relay apply: --force is only supported in shadow-git mode (phase 4); git-mode delivery "
    "never replaces overlapping user edits (design section 8.3)"
)

VerificationOutcome = Literal["passed", "failed", "skipped", "not_run"]


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("apply", help="apply a run's integration commit to a branch")
    parser.add_argument("run_id", metavar="RUN", help="the run to deliver")
    parser.add_argument(
        "--branch",
        metavar="B",
        default=None,
        help="target branch (default: the current branch); a missing branch is created",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="shadow-git only (phase 4); rejected in git mode",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="apply without the interactive confirmation",
    )
    parser.add_argument("--json", action="store_true", help="print the delivery result as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    as_json: bool = bool(args.json)
    run_id: str = args.run_id
    target: str | None = args.branch
    if bool(args.force):
        return _fail(_FORCE_UNSUPPORTED, Exit.INVALID, as_json)

    try:
        store = RunStore.open(repo_root, run_id)
        record = store.read_run()
    except (StoreError, ValueError, OSError) as exc:
        return _fail(f"relay apply: {exc}", Exit.INVALID, as_json)

    op_id = secrets.token_hex(8)
    try:
        log = EventLog(store.events_path, owner_generation=record.owner_generation)
    except (OSError, EventLogCorrupt) as exc:
        return _fail(f"relay apply: cannot open the run event log: {exc}", Exit.FAILURE, as_json)

    try:
        with session_guard(repo_root, run_id):
            return _apply(
                repo_root,
                run_id,
                target,
                op_id,
                as_json,
                assume_yes=bool(args.yes),
                log=log,
                graph=record.graph,
            )
    except SessionError as exc:
        return _fail(f"relay apply: {exc}", Exit.FAILURE, as_json)
    finally:
        log.close()


# --- the delivery -----------------------------------------------------------------------------


def _apply(
    repo_root: Path,
    run_id: str,
    target: str | None,
    op_id: str,
    as_json: bool,
    *,
    assume_yes: bool,
    log: EventLog,
    graph: Graph | None,
) -> int:
    try:
        plan = prepare_apply(repo_root, run_id, target=target)
    except (RunMissingError, RunNotDoneError, IntegrationMissingError, TargetRefError) as exc:
        return _fail(f"relay apply: {exc}", Exit.INVALID, as_json)
    except DirtyOverlapError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(
            f"relay apply: {exc}. The run is verified but not applied; nothing was changed.",
            Exit.FAILURE,
            as_json,
        )
    except MergeConflictError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(f"relay apply: {exc}", Exit.FAILURE, as_json)
    except ApplyError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(f"relay apply: {exc}", Exit.FAILURE, as_json)

    _event(
        log,
        "apply.started",
        {"op_id": op_id, "target": plan.target_ref, "commit": plan.prepared_commit},
    )
    _event(log, "apply.progress", {"op_id": op_id, "step": "prepared"})

    if plan.noop:
        _event(log, "apply.finished", {"op_id": op_id, "status": "applied"})
        _print_applied(
            plan,
            verify_outcome="not_run",
            checks=0,
            installed=False,
            as_json=as_json,
            run_id=run_id,
            op_id=op_id,
        )
        return int(Exit.OK)

    try:
        verdict = _verify_prepared(repo_root, plan, graph, log=log, op_id=op_id)
    except VerificationError as exc:
        verdict = _Verdict("failed", str(exc))
    except ApplyError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "failed", "error": str(exc)})
        return _fail(
            f"relay apply: integration verification could not run: {exc}; nothing was changed",
            Exit.FAILURE,
            as_json,
        )
    if verdict.outcome == "failed":
        _event(
            log,
            "apply.finished",
            {
                "op_id": op_id,
                "status": "refused",
                "error": "integration verification failed",
            },
        )
        return _fail(_verification_failure(verdict), Exit.VERIFICATION_FAILED, as_json)

    if not as_json:
        print(_summary(plan, verdict))

    if assume_yes:
        _event(log, "apply.progress", {"op_id": op_id, "step": "confirmed"})
    elif not _confirmed(run_id, target, log=log, op_id=op_id):
        return _fail(_not_confirmed(run_id, target), Exit.APPROVAL_REQUIRED, as_json)

    # Preparation and confirmation are invalidated by a changed target or index (8.3).
    try:
        refreshed = prepare_apply(repo_root, run_id, target=target)
    except DirtyOverlapError as exc:
        _event(
            log,
            "apply.finished",
            {"op_id": op_id, "status": "refused", "error": str(exc)},
        )
        return _fail(
            f"relay apply: the working tree or index changed while confirming: {exc}; "
            "nothing was changed",
            Exit.FAILURE,
            as_json,
        )
    except (MergeConflictError, TargetRefError, ApplyError) as exc:
        _event(
            log,
            "apply.finished",
            {"op_id": op_id, "status": "refused", "error": str(exc)},
        )
        return _fail(
            f"relay apply: preparing the delivery again failed: {exc}; nothing was changed",
            Exit.FAILURE,
            as_json,
        )
    if refreshed != plan:
        message = (
            "relay apply: the target or its tip changed while confirming, so the previous "
            "confirmation no longer applies; nothing was changed"
        )
        _event(
            log,
            "apply.finished",
            {"op_id": op_id, "status": "refused", "error": "preparation invalidated"},
        )
        return _fail(
            f"{message}\nRe-run: {_confirmation_command(run_id, target)}",
            Exit.APPROVAL_REQUIRED,
            as_json,
        )

    try:
        result = commit_ref(repo_root, plan)
    except TargetMovedError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(
            f"relay apply: {exc}; nothing was changed. "
            f"Re-run: {_confirmation_command(run_id, target)}",
            Exit.APPROVAL_REQUIRED,
            as_json,
        )
    except ApplyError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(f"relay apply: {exc}; nothing was changed", Exit.FAILURE, as_json)

    _event(log, "apply.progress", {"op_id": op_id, "step": "ref-updated"})

    try:
        installed = _install_worktree(repo_root, plan)
    except ApplyError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "failed", "error": str(exc)})
        return _fail(
            f"relay apply: the branch {plan.target_ref} now points at {plan.prepared_commit}, "
            f"but the working tree update failed: {exc}. Resolve the working tree and re-run.",
            Exit.FAILURE,
            as_json,
        )

    _event(log, "apply.progress", {"op_id": op_id, "step": "installed"})
    _event(log, "apply.finished", {"op_id": op_id, "status": "applied"})
    _print_applied(
        plan,
        verify_outcome=verdict.outcome,
        checks=verdict.checks,
        installed=installed,
        as_json=as_json,
        ref_updated=result.changed,
        run_id=run_id,
        op_id=op_id,
    )
    return int(Exit.OK)


# --- integration verification ----------------------------------------------------------------


@dataclass(frozen=True)
class _Verdict:
    """The outcome of running the run's integration acceptance on the prepared tree."""

    outcome: VerificationOutcome
    detail: str
    checks: int = 0
    result: VerifyResult | None = None


def _verify_prepared(
    repo_root: Path,
    plan: ApplyPlan,
    graph: Graph | None,
    *,
    log: EventLog,
    op_id: str,
) -> _Verdict:
    """Run integration acceptance against the prepared tree (design section 10 item 4).

    Commands come from the run's recorded graph: executable ``integration_acceptance`` entries
    win, otherwise graph ``build``/``test``/``lint`` defaults. Without a recorded graph there is
    nothing to run and the delivery proceeds with the outcome reported as ``not_run``; the run
    itself was already verified when it completed. A run whose graph opts out with
    ``verify: none`` is reported as ``skipped`` with its recorded reason.
    """
    task = _integration_task(graph)
    if task is None:
        log.append("apply.progress", {"op_id": op_id, "step": "verification-not-recorded"})
        return _Verdict("not_run", "no integration acceptance commands are recorded for this run")

    changes = tuple(Change(path=path, status="modified") for path in plan.changed_paths)

    if task.verify == "default" and not executable_commands(task):
        # Scope checks are acceptance entries of their own and apply even when no command is
        # configured (design section 10 item 3); run them before deciding there is nothing else.
        violations = scope_violations(changes, effective_scope(task))
        if violations:
            log.append("apply.progress", {"op_id": op_id, "step": "verification-failed"})
            return _Verdict(
                "failed",
                "scope: " + ", ".join(violations) + " outside the integration scope",
                1,
            )
        log.append("apply.progress", {"op_id": op_id, "step": "verification-not-configured"})
        return _Verdict("not_run", "the run records no executable integration checks")

    with tempfile.TemporaryDirectory(prefix="relay-apply-verify-") as temp:
        worktree = Path(temp) / "worktree"
        _worktree_add(repo_root, worktree, plan.prepared_commit)
        try:
            capture = CaptureResult(
                input_commit=plan.target_tip,
                candidate_tree=plan.prepared_tree,
                candidate_commit=plan.prepared_commit,
                candidate_path=worktree,
                changes=changes,
                counts=ChangeCounts(modified=len(changes)),
            )
            result = verify(capture, task)
        finally:
            _worktree_remove(repo_root, worktree)

    step = (
        "verification-skipped" if result.outcome == "skipped" else f"verification-{result.outcome}"
    )
    log.append("apply.progress", {"op_id": op_id, "step": step})
    if result.outcome == "failed":
        failed = [check.name for check in result.checks if check.status in ("failed", "timed_out")]
        return _Verdict(
            "failed",
            ", ".join(failed) or "verification failed",
            len(result.checks),
            result,
        )
    if result.outcome == "skipped":
        return _Verdict(
            "skipped",
            f"verify: none ({result.skip_reason or 'no reason recorded'})",
            len(result.checks),
            result,
        )
    return _Verdict("passed", "passed", len(result.checks), result)


def _integration_task(graph: Graph | None) -> TaskCore | None:
    """The integration verification policy recorded with the run, or None.

    ``integration_acceptance`` and ``commands`` are graph-level in the design; a run without a
    recorded graph has no authorized integration commands to run at apply time.
    """
    if graph is None:
        return None
    acceptance: list[Acceptance] = list(graph.integration_acceptance)
    commands: Commands | None = graph.commands
    if not acceptance and commands is None and graph.verify != "none":
        return None
    return TaskCore(
        category="implement",
        goal="integration acceptance",
        acceptance=acceptance,
        commands=commands,
        verify=graph.verify,
        verification_skip_reason=graph.verification_skip_reason,
    )


# --- confirmation -----------------------------------------------------------------------------


def _interactive() -> bool:
    return sys.stdin.isatty()


def _confirmed(
    run_id: str,
    target: str | None,
    *,
    log: EventLog,
    op_id: str,
) -> bool:
    """Ask the operator to confirm, after verification and before any state changes.

    ``--yes`` is handled by the caller. A non-interactive session is refused (exit 7 at the
    caller) with the exact confirming command; a declined prompt changes nothing.
    """
    if not _interactive():
        _event(
            log,
            "apply.finished",
            {"op_id": op_id, "status": "refused", "error": "not confirmed"},
        )
        return False

    sys.stderr.write("\nApply this delivery? [y/N] ")
    sys.stderr.flush()
    try:
        answer = input()
    except EOFError:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        _event(log, "apply.progress", {"op_id": op_id, "step": "confirmed"})
        return True
    _event(
        log,
        "apply.finished",
        {"op_id": op_id, "status": "refused", "error": "not confirmed"},
    )
    return False


def _confirmation_command(run_id: str, target: str | None) -> str:
    parts = ["relay", "apply", run_id]
    if target is not None:
        parts.extend(["--branch", target])
    parts.append("--yes")
    return shlex.join(parts)


# --- installation -----------------------------------------------------------------------------


def _install_worktree(repo_root: Path, plan: ApplyPlan) -> bool:
    """Install the delivered paths when the target is the current branch.

    Only ``target_tip..prepared`` paths are restored, staged and written from the prepared
    commit. Staged, unstaged and untracked user edits outside that delta are left exactly as
    they are (design section 8.3). A new or non-current branch is never checked out.
    """
    current = _current_branch(repo_root)
    if current != plan.target_ref:
        return False
    if not plan.changed_paths:
        return False
    paths = list(plan.changed_paths)
    proc = _git(
        repo_root,
        "restore",
        f"--source={plan.prepared_commit}",
        "--staged",
        "--worktree",
        "--",
        *paths,
    )
    if proc.returncode != 0:
        raise GitApplyError(("restore", plan.prepared_commit), proc.stderr)
    return True


def _current_branch(repo_root: Path) -> str | None:
    proc = _git(repo_root, "symbolic-ref", "--quiet", "HEAD")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


# --- output -----------------------------------------------------------------------------------


def _mode(plan: ApplyPlan) -> Literal["noop", "fast-forward", "merge"]:
    if plan.noop:
        return "noop"
    if plan.prepared_commit == plan.integration_commit:
        return "fast-forward"
    return "merge"


def _verification_text(verdict: _Verdict) -> str:
    if verdict.outcome == "passed":
        return f"passed ({verdict.checks} check(s))"
    if verdict.outcome == "skipped":
        return f"skipped ({verdict.detail})"
    if verdict.outcome == "failed":
        return f"failed ({verdict.detail})"
    return f"not run ({verdict.detail})"


def _summary(plan: ApplyPlan, verdict: _Verdict) -> str:
    lines = [
        f"apply run {plan.run_id} to {plan.target_ref}",
        f"  target tip: {plan.target_tip[:12]}",
        f"  prepared commit: {plan.prepared_commit[:12]} ({_mode(plan)})",
        f"  integration commit: {plan.integration_commit[:12]}",
        f"  changed paths: {len(plan.changed_paths)}",
    ]
    for path in plan.changed_paths[:20]:
        lines.append(f"    {path}")
    if len(plan.changed_paths) > 20:
        lines.append(f"    ... and {len(plan.changed_paths) - 20} more")
    lines.append(f"  verification: {_verification_text(verdict)}")
    return "\n".join(lines)


def _print_applied(
    plan: ApplyPlan,
    *,
    verify_outcome: VerificationOutcome,
    checks: int,
    installed: bool,
    as_json: bool,
    ref_updated: bool = False,
    run_id: str | None = None,
    op_id: str | None = None,
) -> None:
    if as_json:
        payload: dict[str, Any] = {
            "status": "applied",
            "run_id": run_id if run_id is not None else plan.run_id,
            "op_id": op_id,
            "target": plan.target_ref,
            "target_tip": plan.target_tip,
            "integration_commit": plan.integration_commit,
            "prepared_commit": plan.prepared_commit,
            "mode": _mode(plan),
            "changed_paths": list(plan.changed_paths),
            "verification": {"outcome": verify_outcome, "checks": checks},
            "ref_updated": ref_updated,
            "worktree_installed": installed,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if plan.noop:
        print(f"run {plan.run_id} is already contained in {plan.target_ref}; nothing to apply")
        return
    print(
        f"applied run {plan.run_id} to {plan.target_ref} ({plan.prepared_commit[:12]}, "
        f"{_mode(plan)})"
    )
    print(f"  changed paths: {len(plan.changed_paths)}")
    print(f"  verification: {verify_outcome} ({checks} check(s))")
    print(
        "  working tree: updated"
        if installed
        else "  working tree: untouched (the target is not the checked-out branch)"
    )


def _verification_failure(verdict: _Verdict) -> str:
    lines = [
        f"relay apply: integration verification failed ({verdict.detail}); nothing was changed"
    ]
    if verdict.result is not None:
        for check in verdict.result.checks:
            if check.status not in ("failed", "timed_out"):
                continue
            detail = "; ".join(check.details[:4])
            lines.append(f"  {check.name}: {check.status}" + (f" ({detail})" if detail else ""))
    lines.append("Verification failure rejects application, even with --force.")
    return "\n".join(lines)


def _not_confirmed(run_id: str, target: str | None) -> str:
    return (
        "relay apply: approval required; the delivery was prepared but not confirmed. "
        f"Nothing was changed.\nConfirm with: {_confirmation_command(run_id, target)}"
    )


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)


# --- subprocess helpers -----------------------------------------------------------------------


def _event(log: EventLog, type_: str, data: dict[str, str]) -> None:
    log.append(type_, data)


def _worktree_add(repo_root: Path, worktree: Path, commit: str) -> None:
    proc = _git(repo_root, "worktree", "add", "--detach", str(worktree), commit)
    if proc.returncode != 0:
        raise GitApplyError(("worktree", "add", commit), proc.stderr)


def _worktree_remove(repo_root: Path, worktree: Path) -> None:
    """Best-effort removal; never masks the verification failure that is being reported."""
    _git(repo_root, "worktree", "remove", "--force", str(worktree))
    _git(repo_root, "worktree", "prune")


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command with an explicit argument list (never a shell)."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            env={**os.environ},
        )
    except FileNotFoundError as exc:
        raise GitApplyError(args, "git is not installed or not on PATH") from exc
