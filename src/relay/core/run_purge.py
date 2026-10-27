"""Purging one run's records, refs, worktrees and scratch (design section 6).

The same rules as `relay clean --purge-runs`, for callers in core (confirmed session deletion):

* A run whose owner is alive, or cannot be checked, is never purged.
* Only git worktrees registered under the run's own worktree directory, or capture candidates
  under the run directory, are removed, through :class:`WorktreeManager`'s containment checks.
* Only refs under `refs/relay/<run>/` and `refs/heads/relay/<run>/` are deleted: no other branch,
  and never the user's checked-out branch, is touched.
* Refs go before the run record, so an interrupted purge can be repeated. Lock objects are never
  created, replaced or unlinked.

Nothing here reads with `RunStore.open` (which would sweep another writer's temp files).
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

__all__ = ["RunPurge", "execute_run_purge", "owner_blocker", "plan_run_purge", "run_refs"]

_PROBE_NODE = "n1"  # a valid node id, used only to derive the run's worktree directory


@dataclass
class RunPurge:
    run_id: str
    run_dir: Path
    worktree_dir: Path
    worktrees: list[tuple[Path, bool]] = field(default_factory=list[tuple[Path, bool]])
    """(path, is_capture_candidate) of the registered worktrees to remove."""
    scratch: Path | None = None
    refs: list[str] = field(default_factory=list[str])


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ},
        timeout=300,
    )


def owner_blocker(repo_root: Path, run_id: str) -> str | None:
    """Why the run must not be purged now (its owner is alive or unverifiable), or None."""
    try:
        state = inspect_owner(repo_root, run_id).state
    except OSError:
        state = OwnerState.UNKNOWN
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
    prefixes = (f"refs/relay/{run_id}/", f"refs/heads/relay/{run_id}/")
    return [
        line
        for line in proc.stdout.splitlines()
        if line == f"refs/relay/{run_id}" or line.startswith(prefixes)
    ]


def _registered(repo_root: Path) -> list[Path]:
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


def plan_run_purge(repo_root: Path, run_id: str, worktree_root: str) -> RunPurge:
    """What purging `run_id` would remove (read-only)."""
    manager = WorktreeManager(repo_root, run_id, worktree_root=worktree_root)
    worktree_dir = manager.node_path(_PROBE_NODE).parent  # a pure path computation
    run_dir = runs_dir(repo_root) / run_id
    plan = RunPurge(run_id, run_dir, worktree_dir)
    for path in _registered(repo_root):
        if _is_under(path, worktree_dir):
            plan.worktrees.append((path, False))
        elif _is_under(path, run_dir):
            plan.worktrees.append((path, True))
    scratch = repo_root / AGENTS_DIR / "scratch" / run_id
    plan.scratch = scratch if scratch.is_dir() else None
    plan.refs = run_refs(repo_root, run_id)
    return plan


def execute_run_purge(repo_root: Path, plan: RunPurge) -> None:
    """Remove what `plan` lists; raises on the first failure, leaving the rest for a retry."""
    for path, candidate in plan.worktrees:
        if candidate:
            WorktreeManager(repo_root, plan.run_id).decommission_candidate(path)
        else:
            WorktreeManager(
                repo_root, plan.run_id, worktree_root=str(plan.worktree_dir.parent)
            ).decommission(path)
    if plan.scratch is not None and plan.scratch.is_dir():
        shutil.rmtree(plan.scratch)
    for ref in plan.refs:
        if not (ref.startswith((f"refs/relay/{plan.run_id}/", f"refs/heads/relay/{plan.run_id}/"))
                or ref == f"refs/relay/{plan.run_id}"):  # fmt: skip
            raise WorktreeError(f"refusing to delete {ref}: outside run {plan.run_id}")
        proc = _git(repo_root, "update-ref", "-d", ref)
        if proc.returncode != 0:
            raise WorktreeError(f"cannot delete {ref}: {proc.stderr.strip()}")
    if plan.run_dir.is_dir():
        shutil.rmtree(plan.run_dir)
    with contextlib.suppress(OSError):
        plan.worktree_dir.rmdir()
    _git(repo_root, "worktree", "prune")
