"""Complete change capture for a node worktree (design section 8.3).

After a worker exits, the orchestrator captures the complete delta of the node's worktree
relative to its ``input_commit``: agent-created commits, staged and unstaged edits, untracked
non-ignored files, deletions, renames (detected the way git detects them, so both staged and
unstaged renames are found), binary files, and file-mode changes. The delta is materialized in a
separate *candidate checkout* -- a clean detached git worktree at a floating candidate commit --
that the Verifier (task 1.16) runs acceptance commands in and output publication (task 1.17)
builds the owned output commit from.

How capture works. The working tree and index are combined into a single "effective tree" using
a temporary index: ``git read-tree <input_commit>`` seeds it, ``git add -A`` stages the current
working-tree state (tracked modifications, deletions, untracked non-ignored files, mode changes)
on top, and ``git write-tree`` produces the candidate tree. A temporary ``GIT_INDEX_FILE`` keeps
the node worktree's own index and files untouched. Agent-created commits are captured implicitly:
whatever is checked out (committed or not) becomes the net delta from ``input_commit``.

Runtime-state exclusion. Reserved paths (``.git/**`` and the ``.agents/`` runtime subtrees,
design section 6) are removed from the temporary index before ``write-tree`` using the existing
:func:`relay.core.gitbase.is_reserved_path`, so they appear neither in the delta nor in the
candidate. This is a path filter, independent of the repository-local exclude block, so it holds
even where that block is absent. ``.agents/agents.yaml`` and other ordinary ``.agents`` files
are not reserved and are captured normally.

What git cannot represent, documented rather than silently dropped:

* **Empty directories.** Git records files, not directories, so an untracked empty directory
  has no representation. They are reported on :attr:`CaptureResult.unrepresentable`.
* **Submodule internals.** Only a submodule's gitlink (the recorded commit) is captured; a
  submodule with uncommitted internal changes is reported on :attr:`CaptureResult.dirty_submodules`
  and those changes are *not* in the candidate.
* **Ignored files.** ``git add -A`` honours ``.gitignore``/``info/exclude``; ignored outputs are
  excluded unless a caller explicitly declares them deliverables (design section 8.3), which this
  module does not do.

The candidate commit is a floating object (no ref): publication owns the real output commit under
``refs/relay/<run>/...``. The candidate worktree is created with ``git worktree add --detach`` and
is the caller's responsibility to decommission (task 1.17/1.19), with
:meth:`relay.core.worktree.WorktreeManager.decommission_candidate`.

``input_commit`` is resolved once with ``git rev-parse <input>^{commit}`` at the start of capture
and its canonical sha is used for every later git command and stored on
:attr:`CaptureResult.input_commit`, so a symbolic input (``HEAD``, a branch or tag) cannot move
between capture and publication.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .gitbase import is_reserved_path

ChangeStatus = Literal["added", "modified", "deleted", "renamed", "typechanged", "copied"]

# Author/committer identity for the floating candidate commit. Same placeholder convention as
# the rest of the package (no real identity is ever recorded).
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "relay",
    "GIT_AUTHOR_EMAIL": "relay@example.invalid",
    "GIT_COMMITTER_NAME": "relay",
    "GIT_COMMITTER_EMAIL": "relay@example.invalid",
}


class CaptureError(RuntimeError):
    """Change capture failed."""


class MissingWorktreeError(CaptureError):
    """The node worktree path does not exist (the worker deleted it, or it was never created)."""


class NotARepositoryError(CaptureError):
    """The capture source is not inside a git work tree."""


class InvalidInputCommitError(CaptureError):
    """`input_commit` does not resolve to a commit in the repository."""


class CandidateExistsError(CaptureError):
    """The requested candidate directory already exists."""


class GitCaptureFailure(CaptureError):
    """A git command failed unexpectedly."""

    def __init__(self, args: Sequence[str], stderr: str) -> None:
        detail = stderr.strip() or "no output"
        super().__init__(f"git {args[0] if args else ''} failed: {detail}")
        self.args = tuple(args)


@dataclass(frozen=True)
class Change:
    """One changed path in the captured delta (design section 8.3).

    ``path`` is the repository-relative final path. ``old_path`` is set only for renames and
    copies. ``old_mode``/``new_mode`` are the raw git modes (e.g. ``100644``, ``100755``,
    ``120000``, ``160000``); ``000000`` means "absent" on the corresponding side.
    """

    path: str
    status: ChangeStatus
    old_path: str | None = None
    binary: bool = False
    old_mode: str | None = None
    new_mode: str | None = None

    @property
    def mode_changed(self) -> bool:
        """True for a genuine mode change (modified/renamed with differing modes).

        Added files (``000000 -> 100644``) and deletions (``100644 -> 000000``) differ too, but
        that is an addition/removal, not a mode change on an existing file.
        """
        if self.status not in ("modified", "renamed", "typechanged"):
            return False
        return (
            self.old_mode is not None
            and self.new_mode is not None
            and self.old_mode != self.new_mode
        )


@dataclass(frozen=True)
class ChangeCounts:
    """Totals per change status, in a fixed order (design section 8.3)."""

    added: int = 0
    modified: int = 0
    deleted: int = 0
    renamed: int = 0
    typechanged: int = 0
    copied: int = 0

    @property
    def total(self) -> int:
        return (
            self.added
            + self.modified
            + self.deleted
            + self.renamed
            + self.typechanged
            + self.copied
        )

    @property
    def empty(self) -> bool:
        return self.total == 0


@dataclass(frozen=True)
class CaptureResult:
    """The captured delta and the candidate checkout produced from it.

    ``input_commit`` is the canonical sha the caller's input resolved to (never a symbolic rev),
    so publication can parent its output commit on an immutable commit. ``candidate_tree`` is the
    git tree of the captured delta applied to ``input_commit``; ``candidate_commit`` is a
    floating commit (parent ``input_commit``) whose tree is that tree; ``candidate_path`` is the
    detached worktree checked out there. Publication builds the real output commit from
    ``candidate_tree`` (section 8.3); the Verifier runs in ``candidate_path``.

    ``changes`` is deterministically ordered (by final path, then old path). ``excluded`` lists
    reserved runtime paths that were present in the worktree but dropped from the delta and the
    candidate. ``agent_commits`` lists commits on top of ``input_commit`` (informational only).
    ``unrepresentable`` and ``dirty_submodules`` report state git cannot capture (see the module
    docstring).
    """

    input_commit: str
    candidate_tree: str
    candidate_commit: str
    candidate_path: Path
    changes: tuple[Change, ...]
    counts: ChangeCounts
    agent_commits: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    dirty_submodules: tuple[str, ...] = ()
    unrepresentable: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return self.counts.empty


# --- subprocess helpers (never shell-interpolated) --------------------------------------------

_RENAME_STATUSES = ("R", "C")


def _run(
    cwd: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            input=input,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise CaptureError("git is not installed or not on PATH") from exc


def _git(
    cwd: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
) -> str:
    proc = _run(cwd, *args, env=env, input=input)
    if proc.returncode != 0:
        raise GitCaptureFailure(args, proc.stderr)
    return proc.stdout


def _try(
    cwd: Path, *args: str, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run git and return the result regardless of outcome (best-effort steps)."""
    return _run(cwd, *args, env=env)


# --- raw diff parsing -------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawEntry:
    status: str
    old_path: str
    new_path: str
    old_mode: str
    new_mode: str


def _parse_raw_diff(text: str) -> list[_RawEntry]:
    """Parse ``git diff-tree -r -M -z --raw`` output.

    Entries are NUL-separated: ``:<old_mode> <new_mode> <old_sha> <new_sha> <status>\\0<path>\\0``,
    and for renames/copies two paths follow the status instead of one.
    """
    tokens = text.split("\0")
    entries: list[_RawEntry] = []
    i = 0
    while i < len(tokens):
        meta = tokens[i]
        if meta == "":
            i += 1
            continue
        fields = meta.split(" ")
        if len(fields) != 5:
            i += 1
            continue
        old_mode = fields[0][1:] if fields[0].startswith(":") else fields[0]
        new_mode = fields[1]
        status = fields[4]
        if status[:1] in _RENAME_STATUSES:
            old_path = tokens[i + 1] if i + 1 < len(tokens) else ""
            new_path = tokens[i + 2] if i + 2 < len(tokens) else ""
            i += 3
        else:
            old_path = ""
            new_path = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
        entries.append(
            _RawEntry(
                status=status,
                old_path=old_path,
                new_path=new_path,
                old_mode=old_mode,
                new_mode=new_mode,
            )
        )
    return entries


def _parse_numstat_binary(text: str) -> frozenset[str]:
    """Return the set of paths whose diff is binary, from ``git diff --numstat -M -z``.

    Binary entries are ``-\\t-\\t<path>\\0``. Rename entries are ``<a>\\t<d>\\t\\0<old>\\0<new>\\0``
    (an empty path field followed by the two paths).
    """
    tokens = text.split("\0")
    binary: set[str] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "":
            i += 1
            continue
        fields = token.split("\t")
        if len(fields) == 3:
            added, deleted, path = fields
            if path == "" and i + 2 < len(tokens):
                old, new = tokens[i + 1], tokens[i + 2]
                if added == "-" and deleted == "-":
                    binary.add(old)
                    binary.add(new)
                i += 3
                continue
            if added == "-" and deleted == "-":
                binary.add(path)
        i += 1
    return frozenset(binary)


def _classify_status(status: str) -> ChangeStatus:
    first = status[:1]
    if first == "R":
        return "renamed"
    if first == "C":
        return "copied"
    mapping: dict[str, ChangeStatus] = {
        "A": "added",
        "M": "modified",
        "D": "deleted",
        "T": "typechanged",
    }
    return mapping.get(first, "modified")


def _build_changes(entries: list[_RawEntry], binary: frozenset[str]) -> list[Change]:
    changes: list[Change] = []
    for entry in entries:
        status = _classify_status(entry.status)
        path = entry.new_path
        old_path = entry.old_path if status in ("renamed", "copied") else None
        is_binary = path in binary or (old_path is not None and old_path in binary)
        changes.append(
            Change(
                path=path,
                status=status,
                old_path=old_path,
                binary=is_binary,
                old_mode=entry.old_mode,
                new_mode=entry.new_mode,
            )
        )
    changes.sort(key=lambda change: (change.path, change.old_path or ""))
    return changes


def _counts(changes: Sequence[Change]) -> ChangeCounts:
    return ChangeCounts(
        added=sum(1 for c in changes if c.status == "added"),
        modified=sum(1 for c in changes if c.status == "modified"),
        deleted=sum(1 for c in changes if c.status == "deleted"),
        renamed=sum(1 for c in changes if c.status == "renamed"),
        typechanged=sum(1 for c in changes if c.status == "typechanged"),
        copied=sum(1 for c in changes if c.status == "copied"),
    )


# --- detection of what git cannot represent ---------------------------------------------------


def _tree_paths(cwd: Path, commit: str) -> frozenset[str]:
    proc = _try(cwd, "ls-tree", "-r", "--name-only", "-z", commit)
    if proc.returncode != 0:
        return frozenset()
    return frozenset(path for path in proc.stdout.split("\0") if path)


def _index_gitlinks(cwd: Path, env: Mapping[str, str]) -> frozenset[str]:
    proc = _try(cwd, "ls-files", "--stage", "-z", env=env)
    if proc.returncode != 0:
        return frozenset()
    links: set[str] = set()
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        info, _sep, path = entry.partition("\t")
        if info.split(" ", 1)[0] == "160000":
            links.add(path)
    return frozenset(links)


def _is_ignored(cwd: Path, rel: str) -> bool:
    return _try(cwd, "check-ignore", "-q", "--", rel).returncode == 0


def _empty_untracked_dirs(worktree: Path, gitlinks: frozenset[str]) -> tuple[str, ...]:
    """Directly-empty directories that git cannot represent (see module docstring).

    A directory is reported only when it has no files or subdirectories at all, is not a reserved
    path or a submodule, and is not ignored.
    """
    lost: list[str] = []
    for dirpath, dirnames, filenames in os.walk(worktree):
        rel = os.path.relpath(dirpath, worktree)
        norm = rel.replace(os.sep, "/")
        if norm == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
            continue
        if norm == ".git" or is_reserved_path(norm):
            dirnames[:] = []
            continue
        if norm in gitlinks:
            dirnames[:] = []
            continue
        if not filenames and not dirnames and not _is_ignored(worktree, norm):
            lost.append(norm)
    return tuple(sorted(lost))


def _dirty_submodules(worktree: Path, gitlinks: frozenset[str]) -> tuple[str, ...]:
    """Submodules whose working tree differs from their gitlink (not captured, see docstring)."""
    dirty: list[str] = []
    for path in sorted(gitlinks):
        sub = worktree / path
        if not sub.is_dir():
            continue
        proc = _try(sub, "status", "--porcelain", "--untracked-files=all")
        if proc.returncode != 0:
            continue
        if proc.stdout.strip():
            dirty.append(path)
    return tuple(dirty)


def _agent_commits(worktree: Path, input_commit: str) -> tuple[str, ...]:
    proc = _try(worktree, "rev-list", "--reverse", f"{input_commit}..HEAD")
    if proc.returncode != 0:
        return ()
    return tuple(proc.stdout.splitlines())


def _validate_input_commit(value: str) -> None:
    """Reject an ``input_commit`` that cannot name a single commit safely.

    The value is spliced into ``rev-parse``, ``read-tree``, ``commit-tree`` and ``diff`` command
    lines (never through a shell). It must name one commit (a sha, branch, tag or ``HEAD``), so a
    range (``..``), a parent suffix (``~``), leading ``-`` (a git option) and whitespace/control
    characters are all refused before any git command runs.
    """
    if not value or value.startswith("-"):
        raise InvalidInputCommitError(f"input commit {value!r} is not a valid commit reference")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise InvalidInputCommitError(f"input commit {value!r} must not contain whitespace")
    if "~" in value or ".." in value:
        raise InvalidInputCommitError(f"input commit {value!r} is not a valid commit reference")


# --- the capture entry point ------------------------------------------------------------------


def capture_changes(
    worktree: Path,
    input_commit: str,
    *,
    candidate_dir: Path,
    message: str | None = None,
) -> CaptureResult:
    """Capture the complete delta of `worktree` from `input_commit` into a candidate checkout.

    ``worktree`` is the node worktree directory; it is read but never mutated. ``candidate_dir``
    is where the candidate checkout is materialized (a path that must not already exist). The
    delta is the net difference between ``input_commit``'s tree and the current working-tree plus
    index state, with reserved runtime paths excluded (design sections 6 and 8.3).

    ``input_commit`` may be a sha or a symbolic rev (``HEAD``, a branch or a tag); it is resolved
    to its canonical sha once, and the returned :attr:`CaptureResult.input_commit` is that sha, so
    a symbolic input cannot move between capture and publication.
    """
    source = Path(worktree)
    if not source.is_dir():
        raise MissingWorktreeError(f"node worktree {source} does not exist")
    if _try(source, "rev-parse", "--git-dir").returncode != 0:
        raise NotARepositoryError(f"{source} is not inside a git work tree")
    _validate_input_commit(input_commit)
    resolved = _try(source, "rev-parse", "--verify", "--quiet", f"{input_commit}^{{commit}}")
    if resolved.returncode != 0:
        raise InvalidInputCommitError(f"input commit {input_commit!r} does not exist")
    input_sha = resolved.stdout.strip()

    candidate = Path(candidate_dir)
    if candidate.exists():
        raise CandidateExistsError(f"candidate directory {candidate} already exists")

    input_paths = _tree_paths(source, input_sha)

    index_dir = Path(tempfile.mkdtemp(prefix="relay-capture-"))
    try:
        index_path = index_dir / "index"
        env: dict[str, str] = {"GIT_INDEX_FILE": str(index_path)}
        _git(source, "read-tree", input_sha, env=env)
        _git(source, "add", "-A", env=env)

        staged = _git(source, "ls-files", "-z", env=env).split("\0")
        remove = sorted(
            {path for path in staged if path and is_reserved_path(path) and path not in input_paths}
        )
        if remove:
            _git(
                source,
                "update-index",
                "--force-remove",
                "-z",
                "--stdin",
                input="\0".join(remove) + "\0",
                env=env,
            )

        tree = _git(source, "write-tree", env=env).strip()
        gitlinks = _index_gitlinks(source, env)
    finally:
        shutil.rmtree(index_dir, ignore_errors=True)

    commit = _git(
        source,
        "commit-tree",
        tree,
        "-p",
        input_sha,
        "-m",
        message or "relay: candidate",
        env=_COMMIT_ENV,
    ).strip()

    candidate.parent.mkdir(parents=True, exist_ok=True)
    _git(source, "worktree", "add", "--detach", str(candidate), commit)

    raw = _git(
        source,
        "diff-tree",
        "-r",
        "-M",
        "-z",
        "--no-commit-id",
        "--raw",
        input_sha,
        commit,
    )
    numstat = _try(source, "diff", "--numstat", "-M", "-z", input_sha, commit)
    binary = _parse_numstat_binary(numstat.stdout) if numstat.returncode == 0 else frozenset[str]()

    changes = _build_changes(_parse_raw_diff(raw), binary)

    return CaptureResult(
        input_commit=input_sha,
        candidate_tree=tree,
        candidate_commit=commit,
        candidate_path=candidate,
        changes=tuple(changes),
        counts=_counts(changes),
        agent_commits=_agent_commits(source, input_sha),
        excluded=tuple(remove),
        dirty_submodules=_dirty_submodules(source, gitlinks),
        unrepresentable=_empty_untracked_dirs(source, gitlinks),
    )
