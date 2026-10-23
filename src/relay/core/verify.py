"""Verification of a captured candidate tree (design section 10).

The Verifier runs after every write node and after merges, against the candidate checkout
produced by :func:`relay.core.capture.capture_changes`. It never trusts agent self-reports:
it runs the task's executable acceptance commands itself, checks that every changed path
falls inside the allowed scope, and confirms that verification did not mutate the candidate.

Policy (design section 10):

* ``verify`` defaults to ``default``: run the explicit executable ``acceptance`` entries;
  when there are none, run the configured ``build``/``test``/``lint`` commands. A
  ``diff_scope`` entry is a scope check and never counts as an executable check. If no
  executable check can be resolved for a write node, verification raises
  :class:`NoExecutableChecksError` rather than reporting an untested change as verified
  (design section 10 item 1).
* ``verify: none`` with a nonempty ``verification_skip_reason`` skips the command checks (the
  scope check still applies) and is recorded as ``skipped`` with that reason, never
  ``passed`` (design section 10 item 2).
* Scope checks use the complete captured delta. ``diff_scope`` (from ``acceptance`` entries)
  is the acceptance constraint; when absent it defaults to ``owns``; when both are absent the
  whole repository is in scope (design section 10 item 3). ``owns`` is a planning/scheduling
  constraint, not a verification constraint, so a change outside ``owns`` but inside
  ``diff_scope`` still passes.
* Candidate files must remain unchanged by verification: the candidate HEAD must not move and
  no tracked file may change. A mutation is a failure, never silently accepted (design
  section 8.3).

Acceptance commands are split into an explicit argument list (no shell) and run in the
candidate worktree with per-command and overall timeouts, captured stdout/stderr and exit
codes (design sections 9.4 and 10).

The result is a :class:`VerifyResult` the run loop (task 1.19) and publication (task 1.17)
consume; failed verification publishes nothing.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .capture import CaptureResult, Change
from .classifier import match_path_glob
from .platform.proc import CommandNotFound, ProcessResult, resolve_command, run_process
from .specs.task import RunCheck, ScopeCheck, TaskCore

Outcome = Literal["passed", "failed", "skipped"]
CheckKind = Literal["command", "scope", "mutation"]
CheckStatus = Literal["passed", "failed", "skipped", "timed_out"]

_GLOB_META = frozenset("*?[")
_DEFAULT_COMMAND_KEYS: tuple[str, ...] = ("build", "test", "lint")


class VerificationError(RuntimeError):
    """Verification could not be performed."""


class NoExecutableChecksError(VerificationError):
    """A write node in default mode has no executable check to run (design section 10 item 1).

    The task is rejected before dispatch rather than reporting an untested change as verified.
    """


@dataclass(frozen=True)
class Command:
    """One executable acceptance command, already split into an argument list.

    ``label`` is the human-readable check name (the ``run`` string for an acceptance entry,
    or ``commands.<build|test|lint>`` for a default command).
    """

    label: str
    argv: tuple[str, ...]
    expect_exit: int = 0
    expect_output: str | None = None
    timeout_s: float | None = None


@dataclass(frozen=True)
class CheckResult:
    """One verification check (scope, command or candidate-unchanged)."""

    name: str
    kind: CheckKind
    status: CheckStatus
    details: tuple[str, ...] = ()
    failed_paths: tuple[str, ...] = ()
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    expected_output: str | None = None


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of verification (design section 10 item 5).

    ``outcome`` is ``passed``, ``failed`` or ``skipped``. ``checks`` lists every check that
    ran (or was skipped), in deterministic order. ``failed_paths`` names changed paths outside
    the allowed scope, ``skip_reason`` carries the ``verify: none`` opt-out reason, and
    ``timed_out`` is true when any check hit a timeout. ``candidate_unchanged`` records
    whether the candidate tree was left untouched by the commands.
    """

    outcome: Outcome
    mode: Literal["default", "none"]
    checks: tuple[CheckResult, ...]
    skip_reason: str | None = None
    failed_paths: tuple[str, ...] = ()
    timed_out: bool = False
    candidate_unchanged: bool = True

    @property
    def publication_allowed(self) -> bool:
        """Whether publication may proceed; failed verification publishes nothing (section 8.3)."""
        return self.outcome != "failed"


# --- command resolution -----------------------------------------------------------------------


def _split_windows_command_line(command_line: str) -> list[str]:
    """Parse a Windows command line with the ``CommandLineToArgvW`` rules.

    Whitespace separates arguments; double quotes group and are escaped with ``""``;
    backslashes are literal except where they precede a double quote, where pairs collapse to
    a single backslash and an odd count escapes the quote; single quotes are ordinary
    characters. Empty quoted arguments (``""``) are preserved as empty strings.
    """
    args: list[str] = []
    current: list[str] = []
    in_arg = False
    in_quotes = False
    i = 0
    n = len(command_line)
    while i < n:
        ch = command_line[i]
        if ch in " \t":
            if in_quotes:
                current.append(ch)
                in_arg = True
                i += 1
            elif in_arg:
                args.append("".join(current))
                current = []
                in_arg = False
                i += 1
            else:
                i += 1
        elif ch == "\\":
            start = i
            while i < n and command_line[i] == "\\":
                i += 1
            backslashes = i - start
            if i < n and command_line[i] == '"':
                current.append("\\" * (backslashes // 2))
                if backslashes % 2 == 0:
                    in_quotes = not in_quotes
                else:
                    current.append('"')
                i += 1
            else:
                current.append("\\" * backslashes)
            in_arg = True
        elif ch == '"':
            if in_quotes and i + 1 < n and command_line[i + 1] == '"':
                current.append('"')
                i += 2
            else:
                in_quotes = not in_quotes
                i += 1
            in_arg = True
        else:
            current.append(ch)
            in_arg = True
            i += 1
    if in_arg:
        args.append("".join(current))
    return args


def split_command(command: str, *, windows: bool | None = None) -> list[str]:
    """Split a command line into an argument list without invoking a shell.

    On POSIX this is :func:`shlex.split`. On Windows it follows the ``CommandLineToArgvW``
    rules (see :func:`_split_windows_command_line`), so backslashes in ``C:\\tools\\check.cmd``
    are literal and quoted arguments keep their intended shape. ``windows=None`` selects the
    current platform, so the Windows parser is testable on POSIX.
    """
    if windows is None:
        windows = sys.platform == "win32"
    if not windows:
        return shlex.split(command)
    return _split_windows_command_line(command)


def _split_argv(run: str) -> tuple[str, ...]:
    argv = split_command(run)
    if not argv:
        raise VerificationError(f"acceptance command {run!r} has no executable")
    return tuple(argv)


def _command_from_run_check(check: RunCheck) -> Command:
    return Command(
        label=check.run,
        argv=_split_argv(check.run),
        expect_exit=check.expect_exit,
        expect_output=check.expect_output,
        timeout_s=float(check.timeout_s) if check.timeout_s is not None else None,
    )


def executable_commands(task: TaskCore) -> tuple[Command, ...]:
    """The executable checks for `task` in default mode (design section 10 item 1).

    Explicit executable ``acceptance`` entries win; when there are none, the configured
    ``build``/``test``/``lint`` commands are used in that order. ``diff_scope`` entries are
    scope checks and are ignored here. Returns an empty tuple when no command can be
    resolved, which callers use to reject the task before dispatch.
    """
    run_checks = [entry for entry in task.acceptance if isinstance(entry, RunCheck)]
    if run_checks:
        return tuple(_command_from_run_check(check) for check in run_checks)
    if task.commands is None:
        return ()
    commands: list[Command] = []
    for key in _DEFAULT_COMMAND_KEYS:
        value = getattr(task.commands, key)
        if value:
            commands.append(
                Command(
                    label=f"commands.{key}",
                    argv=_split_argv(value),
                )
            )
    return tuple(commands)


# --- scope -------------------------------------------------------------------------------------


def effective_scope(task: TaskCore) -> tuple[str, ...] | None:
    """The allowed write scope for verification (design section 10 item 3).

    ``diff_scope`` (the union of ``acceptance`` scope entries) is the acceptance constraint;
    when absent it defaults to ``owns``; when both are absent the result is ``None``, meaning
    the entire repository is in scope.
    """
    patterns = [
        p for entry in task.acceptance if isinstance(entry, ScopeCheck) for p in entry.diff_scope
    ]
    if patterns:
        return tuple(patterns)
    if task.owns:
        return tuple(task.owns)
    return None


def path_in_scope(path: str, pattern: str) -> bool:
    """True if repository-relative `path` falls within the scope `pattern`.

    ``**`` crosses segments and ``*``/``?``/``[...]`` match within one, as in
    :func:`relay.core.classifier.match_path_glob`. A literal (glob-free) pattern also covers
    everything beneath it, so a directory scope such as ``src/editor`` matches
    ``src/editor/autosave.ts``.
    """
    if match_path_glob(path, pattern):
        return True
    if not any(ch in pattern for ch in _GLOB_META):
        return match_path_glob(path, f"{pattern}/**")
    return False


def _change_paths(change: Change) -> tuple[str, ...]:
    if change.old_path is not None:
        return (change.path, change.old_path)
    return (change.path,)


def scope_violations(changes: Sequence[Change], scope: tuple[str, ...] | None) -> tuple[str, ...]:
    """Changed paths outside the allowed scope, sorted and deduplicated.

    Every changed path (including rename/copy source and destination) must fall inside
    `scope`; ``None`` means the whole repository, so nothing violates.
    """
    if scope is None:
        return ()
    outside = {
        path
        for change in changes
        for path in _change_paths(change)
        if not any(path_in_scope(path, pattern) for pattern in scope)
    }
    return tuple(sorted(outside))


# --- candidate-unchanged check -----------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or "no output"
        raise VerificationError(f"git {args[0] if args else ''} failed in candidate: {detail}")
    return proc.stdout


def _candidate_mutations(candidate: Path, before_head: str) -> tuple[str, ...]:
    """Detect changes to the candidate during verification (design section 8.3).

    The candidate HEAD must not move and no tracked file may change. Untracked files created
    by the commands (build artifacts, caches) are not part of the deliverable and are ignored.
    """
    try:
        head = _git(candidate, "rev-parse", "HEAD").strip()
        status = _git(candidate, "status", "--porcelain")
    except VerificationError as exc:
        return (f"could not inspect candidate: {exc}",)
    mutations: list[str] = []
    if head != before_head:
        mutations.append(f"HEAD moved from {before_head[:12]} to {head[:12]}")
    for line in status.splitlines():
        if not line:
            continue
        if line.startswith("??"):
            continue  # untracked verification output is not a deliverable
        mutations.append(f"tracked file changed: {line[3:]}")
    return tuple(mutations)


# --- the verify entry point --------------------------------------------------------------------


def verify(
    capture: CaptureResult,
    task: TaskCore,
    *,
    env: Mapping[str, str] | None = None,
    overall_timeout_s: float | None = None,
) -> VerifyResult:
    """Run the verification policy of design section 10 against `capture`.

    `task` supplies ``acceptance``, ``commands``, ``verify``, ``verification_skip_reason`` and
    ``owns`` (the effective values, with graph-level inheritance already resolved by the
    caller). Commands run in ``capture.candidate_path`` with `env` (defaults to the parent
    environment) and are bounded by each entry's ``timeout_s`` and `overall_timeout_s`.

    Raises :class:`NoExecutableChecksError` when a default-mode write node has no executable
    check (design section 10 item 1). Raises :class:`VerificationError` when an acceptance
    command cannot be split into arguments.
    """
    mode = task.verify
    candidate = capture.candidate_path
    scope = effective_scope(task)
    effective_env: Mapping[str, str] = os.environ if env is None else env

    checks: list[CheckResult] = []

    # 1. Scope check (pure, no side effects) -- always applies, even for verify: none.
    violations = scope_violations(capture.changes, scope)
    if scope is None:
        scope_detail = ("scope: entire repository",)
    else:
        scope_detail = (f"scope: {', '.join(scope)}",)
    if violations:
        checks.append(
            CheckResult(
                name="scope",
                kind="scope",
                status="failed",
                details=scope_detail + tuple(f"{path} outside scope" for path in violations),
                failed_paths=violations,
            )
        )
        return VerifyResult(
            outcome="failed",
            mode=mode,
            checks=tuple(checks),
            failed_paths=violations,
            candidate_unchanged=True,
        )

    checks.append(CheckResult(name="scope", kind="scope", status="passed", details=scope_detail))

    # 2. Command checks.
    if mode == "none":
        skip_reason = (task.verification_skip_reason or "").strip()
        checks.append(
            CheckResult(
                name="commands",
                kind="command",
                status="skipped",
                details=(f"verify: none ({skip_reason})",),
            )
        )
        return VerifyResult(
            outcome="skipped",
            mode=mode,
            checks=tuple(checks),
            skip_reason=skip_reason,
            candidate_unchanged=True,
        )

    commands = executable_commands(task)
    if not commands:
        raise NoExecutableChecksError(
            "no executable acceptance or build/test/lint command configured for this write "
            "node; refusing to report an untested change as verified (design section 10 item 1)"
        )

    before_head = _git(candidate, "rev-parse", "HEAD").strip()
    deadline = time.monotonic() + overall_timeout_s if overall_timeout_s is not None else None

    for command in commands:
        remaining: float | None = None
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                checks.append(
                    CheckResult(
                        name=command.label,
                        kind="command",
                        status="timed_out",
                        details=(
                            f"overall verification timeout ({overall_timeout_s:g}s) exceeded",
                        ),
                        timed_out=True,
                    )
                )
                continue
        timeout_s = command.timeout_s
        if timeout_s is None:
            timeout_s = remaining
        elif remaining is not None:
            timeout_s = min(timeout_s, remaining)

        try:
            resolved = resolve_command(
                command.argv[0], command.argv[1:], env=effective_env, cwd=candidate
            )
        except CommandNotFound as exc:
            checks.append(
                CheckResult(
                    name=command.label,
                    kind="command",
                    status="failed",
                    details=(str(exc),),
                )
            )
            continue

        started = time.monotonic()
        proc = run_process(resolved, cwd=candidate, env=effective_env, timeout_s=timeout_s)
        duration = time.monotonic() - started
        checks.append(_evaluate(command, proc, duration))

    # 3. Candidate-unchanged check -- a mutation is a failure, not silently accepted.
    mutations = _candidate_mutations(candidate, before_head)
    checks.append(
        CheckResult(
            name="candidate-unchanged",
            kind="mutation",
            status="passed" if not mutations else "failed",
            details=mutations,
        )
    )

    command_failed = any(
        c.kind == "command" and c.status in ("failed", "timed_out") for c in checks
    )
    failed = command_failed or bool(mutations)
    timed_out = any(c.timed_out for c in checks)
    return VerifyResult(
        outcome="failed" if failed else "passed",
        mode=mode,
        checks=tuple(checks),
        timed_out=timed_out,
        candidate_unchanged=not mutations,
    )


def _evaluate(command: Command, result: ProcessResult, duration: float) -> CheckResult:
    """Turn a completed :class:`~relay.core.platform.proc.ProcessResult` into a CheckResult."""
    details: list[str] = []
    if result.timed_out:
        status: CheckStatus = "timed_out"
        details.append(
            f"timed out after {command.timeout_s:g}s" if command.timeout_s else "timed out"
        )
    elif result.exit_code != command.expect_exit:
        status = "failed"
        details.append(f"exit {result.exit_code} (expected {command.expect_exit})")
    elif command.expect_output is not None and command.expect_output not in result.stdout:
        status = "failed"
        details.append(f"expected output {command.expect_output!r} not in stdout")
    else:
        status = "passed"
        details.append(f"exit {result.exit_code}")
    if status == "failed" and result.stderr.strip():
        details.append(result.stderr.strip())
    return CheckResult(
        name=command.label,
        kind="command",
        status=status,
        details=tuple(details),
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=result.timed_out,
        duration_s=duration,
        expected_output=command.expect_output,
    )
