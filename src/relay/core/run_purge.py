"""Cleaning and purging one run's worktrees, scratch, refs and record (design section 6).

The one implementation of the `relay clean` rules, shared by `relay clean [--purge-runs]` and
confirmed session deletion:

* A run whose owner is alive, or cannot be checked, is never cleaned or purged.
* Only git worktrees registered under the run's own worktree directory are removed (and, when
  purging, capture candidates registered under the run directory), each through
  :class:`WorktreeManager`'s containment checks. The main worktree and unrelated paths are
  never touched.
* Only refs under `refs/relay/<run>/` and `refs/heads/relay/<run>/` are deleted: no other branch,
  and never the user's checked-out branch, is touched.
* Every other removal comes before the run record, and the record is kept when any of them
  failed, so an interrupted or partly failed purge can be repeated. Lock objects are never
  created, replaced or unlinked.

Nothing here opens a run for writing or sweeps its temp files: planning only reads.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .runlock import OwnerState, inspect_owner
from .store import AGENTS_DIR, runs_dir
from .worktree import WorktreeError, WorktreeManager

__all__ = [
    "Removal",
    "RunPurge",
    "execute_run_purge",
    "owner_blocker",
    "owner_state",
    "plan_run_purge",
    "purge_run",
    "registered_worktrees",
    "run_refs",
]

_PROBE_NODE = "n1"  # a valid node id, used only to derive the run's worktree directory
_GIT_TIMEOUT_S = 300
KEPT_RECORD = "kept: an earlier removal failed, so the purge can be retried"


@dataclass
class Removal:
    """One path a clean or purge removes, and what happened to it."""

    path: Path
    candidate: bool = False
    """A capture candidate under the run directory (not a node worktree)."""
    removed: bool = False
    error: str | None = None


@dataclass
class RunPurge:
    """What cleaning (or, with `record`, purging) one run removes; outcomes are filled in."""

    run_id: str
    run_dir: Path
    worktree_dir: Path
    worktrees: list[Removal] = field(default_factory=list[Removal])
    scratch: Removal | None = None
    record: Removal | None = None
    """The run directory; None when only worktrees and scratch are cleaned."""
    refs: list[str] = field(default_factory=list[str])
    ref_errors: list[str] = field(default_factory=list[str])

    @property
    def errors(self) -> list[str]:
        found = [f"{r.path}: {r.error}" for r in self.worktrees if r.error is not None]
        if self.scratch is not None and self.scratch.error is not None:
            found.append(f"{self.scratch.path}: {self.scratch.error}")
        found += self.ref_errors
        if self.record is not None and self.record.error is not None:
            found.append(f"{self.record.path}: {self.record.error}")
        return found


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git without stdin and with a bound; a missing git or a timeout is a WorktreeError."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env={**os.environ},
            timeout=_GIT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {args[0]} timed out") from exc


def owner_state(repo_root: Path, run_id: str) -> OwnerState:
    """The run lock's recorded owner; an unreadable lock counts as UNKNOWN."""
    try:
        return inspect_owner(repo_root, run_id).state
    except OSError:
        return OwnerState.UNKNOWN


def owner_blocker(repo_root: Path, run_id: str) -> str | None:
    """Why the run must not be purged now (its owner is alive or unverifiable), or None."""
    state = owner_state(repo_root, run_id)
    if state is OwnerState.ALIVE:
        return f"run {run_id} is in progress"
    if state is OwnerState.UNKNOWN:
        return f"the owner of run {run_id} cannot be checked"
    return None


def run_refs(repo_root: Path, run_id: str) -> list[str]:
    """Exactly the refs a purge removes for `run_id` (its own namespaces only)."""
    proc = _git(repo_root, "for-each-ref", "--format=%(refname)")
    if proc.returncode != 0:
        raise WorktreeError(f"cannot list refs: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if _own_ref(run_id, line)]


def _own_ref(run_id: str, ref: str) -> bool:
    return ref == f"refs/relay/{run_id}" or ref.startswith(
        (f"refs/relay/{run_id}/", f"refs/heads/relay/{run_id}/")
    )


def registered_worktrees(repo_root: Path) -> list[Path]:
    """Every worktree git has registered for the repository."""
    proc = _git(repo_root, "worktree", "list", "--porcelain")
    if proc.returncode != 0:
        raise WorktreeError(f"cannot list worktrees: {proc.stderr.strip()}")
    return [
        Path(line[len("worktree ") :])
        for line in proc.stdout.splitlines()
        if line.startswith("worktree ")
    ]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def run_worktree_dir(repo_root: Path, run_id: str, worktree_root: str) -> Path:
    """The run's own worktree directory (a pure path computation; nothing is created)."""
    manager = WorktreeManager(repo_root, run_id, worktree_root=worktree_root)
    return manager.node_path(_PROBE_NODE).parent


def plan_run_purge(
    repo_root: Path,
    run_id: str,
    worktree_root: str,
    *,
    record: bool = True,
    registered: list[Path] | None = None,
) -> RunPurge:
    """What cleaning `run_id` removes (read-only); `record` also purges its refs and record.

    The caller checks the owner first (:func:`owner_blocker`). `registered` lets a caller
    planning several runs list the worktrees once.
    """
    worktree_dir = run_worktree_dir(repo_root, run_id, worktree_root)
    run_dir = runs_dir(repo_root) / run_id
    plan = RunPurge(run_id, run_dir, worktree_dir)
    for path in registered if registered is not None else registered_worktrees(repo_root):
        if _is_under(path, worktree_dir):
            plan.worktrees.append(Removal(path))
        elif record and _is_under(path, run_dir):
            # Capture candidates live under the run directory, outside the worktree root.
            plan.worktrees.append(Removal(path, candidate=True))
    scratch = repo_root / AGENTS_DIR / "scratch" / run_id
    if scratch.is_dir():
        plan.scratch = Removal(scratch)
    if record:
        plan.record = Removal(run_dir)
        plan.refs = run_refs(repo_root, run_id)
    return plan


def purge_run(repo_root: Path, plan: RunPurge) -> None:
    """Attempt every removal in `plan`, recording each outcome in it; never raises for them.

    Worktrees and scratch go first, then the refs; the run record goes last and only when
    nothing before it failed (it is then marked kept with :data:`KEPT_RECORD`).
    """
    for removal in plan.worktrees:
        try:
            # The manager validates containment again for every removal.
            if removal.candidate:
                result = WorktreeManager(repo_root, plan.run_id).decommission_candidate(
                    removal.path
                )
            else:
                result = WorktreeManager(
                    repo_root, plan.run_id, worktree_root=str(plan.worktree_dir.parent)
                ).decommission(removal.path)
            removal.removed = result.removed
        except (OSError, WorktreeError) as exc:
            removal.error = str(exc)
    if plan.scratch is not None:
        _remove_tree(plan.scratch)
    for ref in plan.refs:
        if not _own_ref(plan.run_id, ref):
            plan.ref_errors.append(f"refusing to delete {ref}: outside run {plan.run_id}")
            continue
        try:
            proc = _git(repo_root, "update-ref", "-d", ref)
        except WorktreeError as exc:
            plan.ref_errors.append(f"cannot delete {ref}: {exc}")
            continue
        if proc.returncode != 0:
            detail = proc.stderr.strip() or "git update-ref failed"
            plan.ref_errors.append(f"cannot delete {ref}: {detail}")
    if plan.record is not None:
        if plan.errors:
            plan.record.error = KEPT_RECORD
        else:
            _remove_tree(plan.record)
    with contextlib.suppress(OSError):
        plan.worktree_dir.rmdir()  # still holds retained worktrees, or is already gone
    if plan.record is not None:
        with contextlib.suppress(WorktreeError):
            _git(repo_root, "worktree", "prune")


def execute_run_purge(repo_root: Path, plan: RunPurge) -> None:
    """Purge as planned; raises :class:`WorktreeError` listing what failed (retry is safe)."""
    purge_run(repo_root, plan)
    if plan.errors:
        raise WorktreeError(f"cannot purge run {plan.run_id}: " + "; ".join(plan.errors))


def _remove_tree(removal: Removal) -> None:
    try:
        if removal.path.is_dir():
            shutil.rmtree(removal.path)
        elif removal.path.exists():
            removal.path.unlink()
        removal.removed = True
    except OSError as exc:
        removal.error = str(exc)
