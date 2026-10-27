"""`relay apply` (design sections 8.3, 8.6, 10 and 14; tasks 1.24 and 2.27).

The privileged delivery step. It prepares the delivery with the shared service
:mod:`relay.core.delivery` (the same one the TUI uses): the run's integration commit becomes a
prepared target through :func:`relay.core.apply.prepare_apply`, the integration acceptance checks
run against the exact prepared tree in a disposable checkout (design section 10 item 4), the
operator confirms, and the prepared commit is installed with the compare-and-swap ref update of
:func:`relay.core.apply.commit_ref` -- and, only when the target is the current branch, the
delivered paths are updated in the working tree and index.

A run that belongs to a session resolves the session's cumulative delivery policy, revalidates
every carried checkpoint and integration, and checks the whole session delta; it cannot bypass
them by naming the last round (design section 10). A run with no session link behaves exactly as
before.

Order matters:

1. Preparation never touches the user's checkout. A merge conflict, a dirty overlap, a blocked
   session delivery or an invalid run is refused before anything runs or changes (design section
   8.3).
2. Integration verification runs on the prepared tree, not on the target tip, because target
   edits made after the run can regress a clean textual merge. Its commands are the run's
   recorded graph ``integration_acceptance`` (or graph ``build``/``test``/``lint`` defaults), or
   the session's accumulated checks; when none are recorded, the delivery proceeds and the
   verification outcome is reported as ``not_run``. ``verify: none`` with its recorded reason is
   reported as ``skipped``. This reuses :mod:`relay.core.verify`; there is no second
   verification engine. A failed check exits
   :attr:`relay.cli.exitcodes.Exit.VERIFICATION_FAILED` and changes nothing, even with
   ``--force``.
3. Confirmation comes after verification. ``--yes`` bypasses the prompt (tests, automation);
   without it a non-interactive session is refused with exit 7 and the exact confirming command,
   and a declined prompt is the same. Exit 7 changes no state (design section 14).
4. The operation is prepared again immediately before installation and compared by digest. A
   target, session, policy, checkpoint or content change while the prompt was open invalidates
   the confirmation (exit 7); a dirty overlap found at that point is the ordinary dirty-overlap
   abort. The compare-and-swap in :func:`commit_ref` guards the remaining race. No user branch,
   HEAD, index or file is touched before that point.
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
import secrets
import shlex
import sys
from pathlib import Path
from typing import Any, Literal

from relay.core.apply import (
    ApplyError,
    DirtyOverlapError,
    IntegrationMissingError,
    MergeConflictError,
    RunMissingError,
    RunNotDoneError,
    TargetMovedError,
    TargetRefError,
)
from relay.core.delivery import (
    ConfirmationInvalid,
    DeliveryBlocked,
    PreparedDelivery,
    Verdict,
    install_delivery,
    prepare_run_delivery,
    verify_delivery,
)
from relay.core.events import EventLog, EventLogCorrupt
from relay.core.sessions import SessionError, session_guard
from relay.core.store import RunStore, StoreError

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
) -> int:
    try:
        prepared = prepare_run_delivery(repo_root, run_id, target=target)
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
    except DeliveryBlocked as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        lines = ["relay apply: delivery is blocked; nothing was changed"]
        lines.extend(f"  {problem}" for problem in exc.problems)
        return _fail("\n".join(lines), Exit.FAILURE, as_json)
    except ApplyError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(f"relay apply: {exc}", Exit.FAILURE, as_json)

    plan = prepared.plan
    _event(
        log,
        "apply.started",
        {"op_id": op_id, "target": plan.target_ref, "commit": plan.prepared_commit},
    )
    _event(log, "apply.progress", {"op_id": op_id, "step": "prepared"})

    if plan.noop:
        _event(log, "apply.finished", {"op_id": op_id, "status": "applied"})
        _print_applied(
            prepared,
            verify_outcome="not_run",
            checks=0,
            installed=False,
            as_json=as_json,
            run_id=run_id,
            op_id=op_id,
        )
        return int(Exit.OK)

    def progress(step: str) -> None:
        log.append("apply.progress", {"op_id": op_id, "step": step})

    try:
        verdict = verify_delivery(repo_root, prepared, on_step=progress)
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
        print(_summary(prepared, verdict))

    if assume_yes:
        _event(log, "apply.progress", {"op_id": op_id, "step": "confirmed"})
    elif not _confirmed(run_id, target, log=log, op_id=op_id):
        return _fail(_not_confirmed(run_id, target), Exit.APPROVAL_REQUIRED, as_json)

    # Confirmation is bound to the prepared operation's digest. The service prepares the
    # operation again immediately before installation; any difference (target, session, policy,
    # carried checkpoints or content) invalidates it (design sections 8.3 and 10).
    stage: list[str] = []

    def note(step: str) -> None:
        stage.append(step)
        if step in ("ref-updated", "installed"):
            log.append("apply.progress", {"op_id": op_id, "step": step})

    try:
        outcome = install_delivery(
            repo_root, prepared, confirmed_digest=prepared.digest, on_step=note
        )
    except ConfirmationInvalid:
        _event(
            log,
            "apply.finished",
            {"op_id": op_id, "status": "refused", "error": "preparation invalidated"},
        )
        message = (
            "relay apply: the target or its tip changed while confirming, so the previous "
            "confirmation no longer applies; nothing was changed"
        )
        return _fail(
            f"{message}\nRe-run: {_confirmation_command(run_id, target)}",
            Exit.APPROVAL_REQUIRED,
            as_json,
        )
    except DirtyOverlapError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(
            f"relay apply: the working tree or index changed while confirming: {exc}; "
            "nothing was changed",
            Exit.FAILURE,
            as_json,
        )
    except TargetMovedError as exc:
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        return _fail(
            f"relay apply: {exc}; nothing was changed. "
            f"Re-run: {_confirmation_command(run_id, target)}",
            Exit.APPROVAL_REQUIRED,
            as_json,
        )
    except ApplyError as exc:
        if "ref-updated" in stage:
            _event(log, "apply.finished", {"op_id": op_id, "status": "failed", "error": str(exc)})
            return _fail(
                f"relay apply: the branch {plan.target_ref} now points at "
                f"{plan.prepared_commit}, but the working tree update failed: {exc}. "
                "Resolve the working tree and re-run.",
                Exit.FAILURE,
                as_json,
            )
        _event(log, "apply.finished", {"op_id": op_id, "status": "refused", "error": str(exc)})
        if "revalidated" in stage:
            return _fail(f"relay apply: {exc}; nothing was changed", Exit.FAILURE, as_json)
        return _fail(
            f"relay apply: preparing the delivery again failed: {exc}; nothing was changed",
            Exit.FAILURE,
            as_json,
        )

    _event(log, "apply.finished", {"op_id": op_id, "status": "applied"})
    _print_applied(
        prepared,
        verify_outcome=verdict.outcome,
        checks=verdict.checks,
        installed=outcome.installed,
        as_json=as_json,
        ref_updated=outcome.result.changed,
        run_id=run_id,
        op_id=op_id,
    )
    return int(Exit.OK)


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


# --- output -----------------------------------------------------------------------------------


def _mode(prepared: PreparedDelivery) -> Literal["noop", "fast-forward", "merge"]:
    plan = prepared.plan
    if plan.noop:
        return "noop"
    if plan.prepared_commit == plan.integration_commit:
        return "fast-forward"
    return "merge"


def _verification_text(verdict: Verdict) -> str:
    if verdict.outcome == "passed":
        return f"passed ({verdict.checks} check(s))"
    if verdict.outcome == "skipped":
        return f"skipped ({verdict.detail})"
    if verdict.outcome == "failed":
        return f"failed ({verdict.detail})"
    return f"not run ({verdict.detail})"


def _summary(prepared: PreparedDelivery, verdict: Verdict) -> str:
    plan = prepared.plan
    lines = [
        f"apply run {plan.run_id} to {plan.target_ref}",
        f"  target tip: {plan.target_tip[:12]}",
        f"  prepared commit: {plan.prepared_commit[:12]} ({_mode(prepared)})",
        f"  integration commit: {plan.integration_commit[:12]}",
        f"  changed paths: {len(plan.changed_paths)}",
    ]
    for path in plan.changed_paths[:20]:
        lines.append(f"    {path}")
    if len(plan.changed_paths) > 20:
        lines.append(f"    ... and {len(plan.changed_paths) - 20} more")
    if prepared.session_id is not None:
        lines.append(f"  session: {prepared.session_id}")
        lines.append(f"  carried contributions: {len(prepared.carried)}")
        lines.append(f"  session delta: {len(prepared.cumulative_paths)} path(s) from the baseline")
        lines.append(f"  delivery digest: {prepared.digest[:16]}")
    lines.append(f"  verification: {_verification_text(verdict)}")
    return "\n".join(lines)


def _print_applied(
    prepared: PreparedDelivery,
    *,
    verify_outcome: VerificationOutcome,
    checks: int,
    installed: bool,
    as_json: bool,
    ref_updated: bool = False,
    run_id: str | None = None,
    op_id: str | None = None,
) -> None:
    plan = prepared.plan
    if as_json:
        payload: dict[str, Any] = {
            "status": "applied",
            "run_id": run_id if run_id is not None else plan.run_id,
            "op_id": op_id,
            "target": plan.target_ref,
            "target_tip": plan.target_tip,
            "integration_commit": plan.integration_commit,
            "prepared_commit": plan.prepared_commit,
            "mode": _mode(prepared),
            "changed_paths": list(plan.changed_paths),
            "verification": {"outcome": verify_outcome, "checks": checks},
            "ref_updated": ref_updated,
            "worktree_installed": installed,
        }
        if prepared.session_id is not None:
            payload["session_id"] = prepared.session_id
            payload["delivery_digest"] = prepared.digest
            payload["carried"] = [
                {"kind": item.kind, "ref": item.ref, "commit": item.commit}
                for item in prepared.carried
            ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if plan.noop:
        print(f"run {plan.run_id} is already contained in {plan.target_ref}; nothing to apply")
        return
    print(
        f"applied run {plan.run_id} to {plan.target_ref} ({plan.prepared_commit[:12]}, "
        f"{_mode(prepared)})"
    )
    print(f"  changed paths: {len(plan.changed_paths)}")
    print(f"  verification: {verify_outcome} ({checks} check(s))")
    print(
        "  working tree: updated"
        if installed
        else "  working tree: untouched (the target is not the checked-out branch)"
    )


def _verification_failure(verdict: Verdict) -> str:
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


def _event(log: EventLog, type_: str, data: dict[str, str]) -> None:
    log.append(type_, data)
