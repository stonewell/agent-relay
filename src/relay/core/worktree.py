"""Per-node git worktrees: creation, submodule/LFS setup, cache isolation, decommission.

Design sections 6, 8.3 and 9.4. Each node runs in its own ``git worktree`` at its input
commit, detached. A write node's ``relay/<run>/<node>`` branch is created and advanced later,
by output publication (task 1.17); this module only prepares the checkout at the pinned input
commit, so it creates a detached worktree for both write and read nodes.

Placement. ``paths.worktree_root`` is ``auto`` or an explicit absolute path. ``auto`` resolves
to ``<repo>/.agents/worktrees`` on POSIX. On Windows it resolves to a short root *outside* the
repository (``%LOCALAPPDATA%\\relay\\wt`` by default) with the layout
``<root>/<8-char repo hash>/<run-short>/<node-short>``, so the full path stays short even for
deep projects; ``<run-short>`` and ``<node-short>`` truncate long ids and append a short hash,
which keeps two nodes that share a long prefix from colliding (section 8.3). On Windows the
worktree is created with ``core.longpaths=true`` per worktree, never in the user's global git
config.

Submodules and LFS. When the commit records a ``.gitmodules`` file the worktree runs
``git submodule update --init --recursive`` before use; ``-c protocol.file.allow=always`` is
scoped to that one command because submodule clones re-read the recorded (already
user-accepted) URL and a local-path submodule otherwise fails the file-transport check. When
Git LFS is installed and the worktree declares ``filter=lfs`` in ``.gitattributes`` the
worktree runs ``git lfs pull``; an absent LFS tool or a failed pull is reported, never treated
as a run failure, because the missing tool is unrelated to the node's own work.

Cache isolation. ``cache_env`` returns the environment additions a child should receive so
parallel worktrees do not race on shared build-target caches; the design names
``CARGO_TARGET_DIR`` (section 8.3), pointed at ``.agents/scratch/<run>/<node>/target``.

Decommission. :meth:`WorktreeManager.decommission` removes a node worktree owned by this
manager (a path under its root) with ``git worktree remove --force`` and prunes
``.git/worktrees``; :meth:`WorktreeManager.decommission_candidate` is the trusted-path
counterpart for the capture candidate worktree that capture creates outside the root. Both are
idempotent, never touch the main worktree, and never remove a path that is not a registered
worktree. Retention is the caller's policy: the design keeps failed-node worktrees for debugging
(section 6), so a node that fails is simply not decommissioned.

No global state: a :class:`WorktreeManager` is bound to one repository and run, and every name
it produces is a pure function of that binding and the node id.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

AUTO = "auto"
"""The ``paths.worktree_root`` sentinel for the platform default (design section 8.3)."""

WINDOWS_AUTO_SUFFIX = ("relay", "wt")
"""Path components under ``%LOCALAPPDATA%`` that hold Windows auto-resolved worktrees."""

NODE_MAX = 16
RUN_MAX = 8
"""Truncation limits for the short Windows layout (section 8.3)."""

# Cache-isolation variables for the child (design sections 6 and 8.3). Only build-target
# caches that hold no credentials and are safe to split per node belong here; registry/auth
# homes such as ``CARGO_HOME`` are passed through, never isolated (section 9.4).
CARGO_TARGET_DIR = "CARGO_TARGET_DIR"


class WorktreeError(RuntimeError):
    """A worktree cannot be created, inspected or removed."""


class NotARepositoryError(WorktreeError):
    """The repository root is not inside a git work tree."""


class InvalidWorktreeConfig(WorktreeError):
    """The configured ``paths.worktree_root`` is not usable."""


class WorktreePathInUse(WorktreeError):
    """The intended worktree path already exists and is not a valid worktree."""


class GitCommandError(WorktreeError):
    """A git command failed unexpectedly."""

    def __init__(self, args: Sequence[str], stderr: str) -> None:
        detail = stderr.strip() or "no output"
        super().__init__(f"git {args[0] if args else ''} failed: {detail}")
        self.args = tuple(args)


# --- subprocess helpers (never shell-interpolated) --------------------------------------------


def _run(
    cwd: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise NotARepositoryError("git is not installed or not on PATH") from exc


def _git(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    proc = _run(cwd, *args, env=env)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.stderr)
    return proc.stdout


def _try(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git and return the result regardless of outcome (optional, best-effort steps)."""
    return _run(cwd, *args)


# --- short, collision-free directory names -----------------------------------------------------


def _hash4(identifier: str) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:4]


def _shorten(identifier: str, limit: int) -> str:
    """Keep `identifier` verbatim when it fits, else truncate and append a 4-hex hash.

    The hash is of the full identifier, so two identifiers that share a long prefix still
    produce different names (the collision window is the 16-bit hash, deterministic per id).
    """
    if len(identifier) <= limit:
        return identifier
    prefix = identifier[:limit].rstrip("-.")
    return f"{prefix}-{_hash4(identifier)}"


def node_dir_name(node_id: str, *, short: bool) -> str:
    """The directory component for a node under the worktree root.

    Full ids are used on POSIX; on Windows the id is shortened per section 8.3 so the path
    stays short. ``short`` selects the layout and should match the resolved root.
    """
    return _shorten(node_id, NODE_MAX) if short else node_id


def _validate_component(value: str, what: str) -> None:
    if not value:
        raise InvalidWorktreeConfig(f"{what} must be non-empty")
    if value.startswith("-"):
        raise InvalidWorktreeConfig(f"{what} {value!r} must not begin with '-'")
    if any(ch in value for ch in "/\\"):
        raise InvalidWorktreeConfig(f"{what} {value!r} must not contain a path separator")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise InvalidWorktreeConfig(f"{what} {value!r} must not contain whitespace")
    if ".." in value or "~" in value or value == "." or value.endswith(".lock"):
        raise InvalidWorktreeConfig(f"{what} {value!r} is not a safe directory name")


# --- root resolution ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeRoot:
    """Where a run's worktrees live and how their paths are laid out."""

    root: Path
    platform: Literal["win32", "posix"]
    short: bool
    explicit: bool
    note: str | None = None


def _windows_auto_root(parent_env: Mapping[str, str]) -> tuple[Path, str | None]:
    """The short Windows root: ``%LOCALAPPDATA%\\relay\\wt``, with a documented fallback."""
    for name in ("LOCALAPPDATA", "TEMP", "TMP"):
        value = parent_env.get(name)
        if value:
            return Path(value).joinpath(*WINDOWS_AUTO_SUFFIX), None
    home = Path.home()
    return home.joinpath("AppData", "Local", *WINDOWS_AUTO_SUFFIX), (
        "LOCALAPPDATA, TEMP and TMP are unset; fell back to the home directory"
    )


def resolve_worktree_root(
    repo_root: Path,
    worktree_root: str = AUTO,
    *,
    platform: str | None = None,
    parent_env: Mapping[str, str] | None = None,
) -> WorktreeRoot:
    """Resolve ``paths.worktree_root`` to an absolute directory (design section 8.3).

    ``auto`` is ``<repo>/.agents/worktrees`` on POSIX and a short root outside the repository
    on Windows; an explicit value must be an absolute path and is used verbatim everywhere.
    """
    platform_name = sys.platform if platform is None else platform
    is_windows = platform_name == "win32"
    normalized: Literal["win32", "posix"] = "win32" if is_windows else "posix"
    parent = dict(os.environ if parent_env is None else parent_env)

    if worktree_root != AUTO:
        path = Path(worktree_root)
        if not path.is_absolute():
            raise InvalidWorktreeConfig(
                f"paths.worktree_root {worktree_root!r} must be an absolute path (or 'auto')"
            )
        return WorktreeRoot(root=path, platform=normalized, short=is_windows, explicit=True)

    if is_windows:
        root, note = _windows_auto_root(parent)
        return WorktreeRoot(root=root, platform=normalized, short=True, explicit=False, note=note)
    return WorktreeRoot(
        root=repo_root / ".agents" / "worktrees",
        platform=normalized,
        short=False,
        explicit=False,
    )


def repo_hash(repo_root: Path) -> str:
    """The short, path-safe identity of a repository for the Windows layout (section 8.3)."""
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    return digest[:8]


# --- results -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Worktree:
    """A prepared node worktree, ready for an adapter invocation."""

    path: Path
    node_id: str
    run_id: str
    input_commit: str
    branch: str | None
    detached: bool
    cache_env: Mapping[str, str]
    setup_notes: tuple[str, ...]


@dataclass(frozen=True)
class DecommissionResult:
    """What a decommission operation (:meth:`WorktreeManager.decommission` or
    :meth:`WorktreeManager.decommission_candidate`) did."""

    path: Path
    removed: bool
    pruned: bool


# --- the manager -------------------------------------------------------------------------------


class WorktreeManager:
    """Creates and removes node worktrees for one run; no global mutable state."""

    def __init__(
        self,
        repo_root: Path,
        run_id: str,
        *,
        worktree_root: str = AUTO,
        platform: str | None = None,
        parent_env: Mapping[str, str] | None = None,
    ) -> None:
        _validate_component(run_id, "run id")
        self._lock = threading.RLock()
        self.repo_root = repo_root
        self.run_id = run_id
        self._resolved = resolve_worktree_root(
            repo_root, worktree_root, platform=platform, parent_env=parent_env
        )
        self._repo_hash = repo_hash(repo_root)

    # --- paths -------------------------------------------------------------------------

    @property
    def root(self) -> Path:
        """The absolute worktree root for this run."""
        return self._resolved.root

    @property
    def short(self) -> bool:
        return self._resolved.short

    def node_path(self, node_id: str) -> Path:
        """The absolute path where the node's worktree lives (or will live)."""
        _validate_component(node_id, "node id")
        if self._resolved.short:
            return (
                self.root
                / self._repo_hash
                / _shorten(self.run_id, RUN_MAX)
                / node_dir_name(node_id, short=True)
            )
        return self.root / self.run_id / node_dir_name(node_id, short=False)

    def cache_env(self, node_id: str) -> dict[str, str]:
        """Cache-isolation variables for the node's child (design sections 6 and 8.3).

        The value points at a per-run, per-node scratch directory under the reserved
        ``.agents/scratch/`` subtree so parallel worktrees never share a build target.
        """
        target = self.repo_root / ".agents" / "scratch" / self.run_id / node_id / "target"
        return {CARGO_TARGET_DIR: str(target)}

    # --- creation ----------------------------------------------------------------------

    def create(
        self,
        node_id: str,
        input_commit: str,
        *,
        branch: str | None = None,
        exist_ok: bool = True,
    ) -> Worktree:
        """Create (or reuse) the node's detached worktree at `input_commit`.

        `branch`, when given, checks the worktree out on that branch instead of detached;
        output publication (task 1.17) owns branch creation, so callers normally leave it
        ``None``. With `exist_ok`, a path that already holds a valid worktree is returned as-is
        (resume preserves the node's prior edits, design section 8.2); a path occupied by
        anything else is an error so two nodes never share a directory.
        """
        with self._lock:
            _validate_component(node_id, "node id")
            _validate_component(input_commit, "input commit")
            path = self.node_path(node_id)
            commit = self._resolve_commit(input_commit)

            if path.exists():
                if exist_ok and self._is_worktree_at(path, commit):
                    return self._inspect(
                        path, node_id, input_commit, branch, ("reused existing worktree",)
                    )
                raise WorktreePathInUse(f"worktree path {path} already exists")

            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self._resolved.short:
                    self._create_short(path, commit, branch)
                else:
                    if branch is not None:
                        _git(self.repo_root, "worktree", "add", "-b", branch, str(path), commit)
                    else:
                        _git(self.repo_root, "worktree", "add", "--detach", str(path), commit)
                notes = self._setup_aux(path)
            except BaseException:
                self._cleanup_failed_creation(path, branch)
                raise
            return self._inspect(path, node_id, input_commit, branch, notes)

    # --- decommission ------------------------------------------------------------------

    def decommission(self, node_or_worktree: str | Path) -> DecommissionResult:
        """Remove a node worktree owned by this manager and prune ``.git/worktrees``.

        A ``str`` is a node id and resolves to the node's path under this manager's root; a
        ``Path`` must already be a directory under that root. Only a registered worktree under
        the root is removed; the main worktree, a path outside the root (including a symlink or
        ``..`` that resolves outside it) and an unregistered path are never touched. For the
        capture candidate worktree, which is created outside the root, use
        :meth:`decommission_candidate`.

        Idempotent: a second call finds nothing to remove and succeeds.
        """
        path = (
            self.node_path(node_or_worktree)
            if isinstance(node_or_worktree, str)
            else Path(node_or_worktree)
        )
        removed = self._remove_worktree(path, contained=True)
        pruned = self._prune()
        return DecommissionResult(path=path, removed=removed, pruned=pruned)

    def decommission_candidate(self, path: Path) -> DecommissionResult:
        """Remove the capture candidate worktree (created outside this manager's root).

        This is the trusted-path counterpart to :meth:`decommission`: the caller asserts that
        `path` is the candidate checkout it created with ``git worktree add --detach`` (the
        :attr:`relay.core.capture.CaptureResult.candidate_path`), which lives outside the root.
        Only a *registered* worktree at `path` is removed; the main worktree, an unregistered
        path and an unrelated directory are never touched.

        Idempotent: a second call finds nothing to remove and succeeds.
        """
        candidate = Path(path)
        removed = self._remove_worktree(candidate, contained=False)
        pruned = self._prune()
        return DecommissionResult(path=candidate, removed=removed, pruned=pruned)

    # --- internals ---------------------------------------------------------------------

    def _resolve_commit(self, input_commit: str) -> str:
        proc = _try(
            self.repo_root, "rev-parse", "--verify", "--quiet", f"{input_commit}^{{commit}}"
        )
        if proc.returncode != 0:
            raise WorktreeError(f"input commit {input_commit!r} does not exist in the repository")
        return proc.stdout.strip()

    def _create_short(self, path: Path, commit: str, branch: str | None) -> None:
        """Windows path: create without checkout, enable per-worktree longpaths, then checkout.

        ``core.longpaths`` must be active for the checkout itself, so the worktree is created
        with ``--no-checkout``, configured per worktree, then checked out. The per-worktree
        config file requires ``extensions.worktreeConfig``, enabled idempotently in the
        repository's shared config (repo-local, never global).
        """
        _git(self.repo_root, "config", "extensions.worktreeConfig", "true")
        if branch is not None:
            _git(
                self.repo_root,
                "worktree",
                "add",
                "--no-checkout",
                "-b",
                branch,
                str(path),
                commit,
            )
        else:
            _git(self.repo_root, "worktree", "add", "--no-checkout", "--detach", str(path), commit)
        _git(path, "config", "--worktree", "core.longpaths", "true")
        _git(path, "checkout", "--force", commit)

    def _setup_aux(self, path: Path) -> tuple[str, ...]:
        """Initialize submodules and pull LFS where available; degrade cleanly otherwise."""
        notes: list[str] = []
        if self._has_submodules(path):
            _git(
                path,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "update",
                "--init",
                "--recursive",
            )
            notes.append("submodules initialized")
        else:
            notes.append("no submodules")
        if self._uses_lfs(path):
            if _git_lfs_available(self.repo_root):
                proc = _try(path, "lfs", "pull")
                notes.append(
                    "lfs pulled" if proc.returncode == 0 else "lfs pull failed; continuing"
                )
            else:
                notes.append("git-lfs not installed; skipped lfs pull")
        return tuple(notes)

    @staticmethod
    def _has_submodules(path: Path) -> bool:
        return (path / ".gitmodules").is_file()

    @staticmethod
    def _uses_lfs(path: Path) -> bool:
        attributes = path / ".gitattributes"
        if not attributes.is_file():
            return False
        try:
            text = attributes.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in text.splitlines():
            stripped = line.split("#", 1)[0]
            if any(token == "filter=lfs" for token in stripped.split()):
                return True
        return False

    def _is_worktree_at(self, path: Path, commit: str) -> bool:
        """True when `path` is already a registered worktree checked out at `commit`."""
        if not (path / ".git").exists():
            return False
        head = _try(path, "rev-parse", "--verify", "--quiet", "HEAD")
        return head.returncode == 0 and head.stdout.strip() == commit

    def _inspect(
        self,
        path: Path,
        node_id: str,
        input_commit: str,
        branch: str | None,
        notes: tuple[str, ...],
    ) -> Worktree:
        head = _try(path, "rev-parse", "--verify", "--quiet", "HEAD")
        actual = head.stdout.strip() if head.returncode == 0 else input_commit
        detached = branch is None
        return Worktree(
            path=path,
            node_id=node_id,
            run_id=self.run_id,
            input_commit=actual,
            branch=branch,
            detached=detached,
            cache_env=self.cache_env(node_id),
            setup_notes=notes,
        )

    def _remove_worktree(self, path: Path, *, contained: bool) -> bool:
        """`git worktree remove --force` for `path`; False when it was not a worktree.

        Only a registered worktree is ever removed; a path that is not one (already gone, or
        an unrelated directory) returns False and `git` refuses it on its own. Stale metadata
        (the directory deleted out-of-band) is left for `git worktree prune` to clear. With
        `contained`, a path outside this manager's root is refused before any git command.
        """
        if path.resolve() == self.repo_root.resolve():
            raise WorktreeError("refusing to decommission the main worktree")
        if contained and not self._is_under_root(path):
            raise WorktreeError(
                f"refusing to decommission {path}: outside this manager's worktree root"
            )
        if not self._registered(path):
            return False
        proc = _try(
            self.repo_root, "-c", "core.longpaths=true", "worktree", "remove", "--force", str(path)
        )
        if proc.returncode == 0:
            return True
        if not path.exists():
            return False  # stale admin metadata; `_prune` below removes it
        raise GitCommandError(("worktree", "remove", str(path)), proc.stderr)

    def _is_under_root(self, path: Path) -> bool:
        """True when `path` (symlinks and ``..`` resolved) lies under this manager's root."""
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return False
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            return False
        return True

    def _cleanup_failed_creation(self, path: Path, branch: str | None) -> None:
        """Remove a worktree whose creation failed after ``git worktree add``.

        Best-effort and never raises: the original error is re-raised by the caller. When the
        worktree was registered (``worktree add`` succeeded and a later step such as submodule
        init failed) it is removed, its admin metadata pruned, and a branch ref created by this
        call (``worktree add -b`` only succeeds for a new branch) is deleted. When it was not
        registered, only the metadata is pruned so a pre-existing branch of the same name is
        never touched.
        """
        if self._registered(path):
            _try(
                self.repo_root,
                "-c",
                "core.longpaths=true",
                "worktree",
                "remove",
                "--force",
                str(path),
            )
            if branch is not None:
                _try(self.repo_root, "branch", "-D", branch)
        self._prune()

    def _registered(self, path: Path) -> bool:
        listed = _try(self.repo_root, "worktree", "list", "--porcelain", "-z")
        if listed.returncode != 0:
            return False
        return any(
            Path(entry.removeprefix("worktree ")).resolve() == path.resolve()
            for entry in listed.stdout.split("\0")
            if entry.startswith("worktree ")
        )

    def _prune(self) -> bool:
        proc = _try(self.repo_root, "worktree", "prune")
        return proc.returncode == 0


def _git_lfs_available(repo: Path) -> bool:
    if shutil.which("git-lfs") is not None:
        return True
    return _try(repo, "lfs", "version").returncode == 0
