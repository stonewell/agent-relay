"""Base commit pinning, synthetic dirty-tree bases, and the `refs/relay/<run>` namespace.

Design section 8.3: at run creation the orchestrator pins a base commit. A clean tree pins the
current branch HEAD. With `--allow-dirty`, a synthetic commit under `refs/relay/<run>/base`
captures the current working tree (tracked files plus untracked, non-ignored files), minus the
reserved runtime paths (section 6), minus sensitive-looking untracked paths, and minus any
caller-supplied `--exclude` globs. The synthetic commit's parent is the checked-out branch HEAD,
and it is written to objects and a ref only: the user's branch and working tree are never touched.
Dirty submodules are not captured (a gitlink records only the checked-out commit); they are
reported via `BaseResult.dirty_submodules` for task 1.14, which owns submodule/LFS handling.

Refs under `refs/relay/` are never pushed by `relay` and are deleted by `relay clean --purge-runs`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from .classifier import match_path_glob
from .specs.common import RESERVED_ROOTS

REFS_NAMESPACE = "refs/relay"
BASE_REF_NAME = "base"

# Sensitive-looking names, matched against any path component (design section 8.3).
SENSITIVE_PATTERNS: tuple[str, ...] = (
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*.pfx",
    "credentials*",
    "*secret*",
)

# A conservative stand-in for the spec `RunId` validator (section 6): path-safe, no ref
# metacharacters. Run ids are `<date>-<hex>`, so this never fires for generated ids.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REF_UNSAFE = frozenset("~^:?*[\\")

_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "relay",
    "GIT_AUTHOR_EMAIL": "relay@example.invalid",
    "GIT_COMMITTER_NAME": "relay",
    "GIT_COMMITTER_EMAIL": "relay@example.invalid",
}


class GitBaseError(RuntimeError):
    """The base commit cannot be resolved or created."""


class NotARepositoryError(GitBaseError):
    """`repo_root` is not inside a git work tree (design section 1)."""


class EmptyRepositoryError(GitBaseError):
    """The repository has an unborn HEAD: no commits to pin."""


class DirtyTreeError(GitBaseError):
    """The working tree is not clean and `--allow-dirty` was not supplied (section 9.4)."""

    def __init__(self, paths: list[str]) -> None:
        preview = ", ".join(paths[:8])
        more = f" and {len(paths) - 8} more" if len(paths) > 8 else ""
        super().__init__(
            f"working tree is not clean ({len(paths)} path(s): {preview}{more}); "
            "commit or stash the changes, or pass --allow-dirty"
        )
        self.paths = list(paths)


class InvalidRefError(GitBaseError):
    """A run id, ref, or exclude pattern cannot be used safely."""


class GitFailure(GitBaseError):
    """A git command failed unexpectedly."""

    def __init__(self, args: tuple[str, ...], stderr: str) -> None:
        detail = stderr.strip() or "no output"
        super().__init__(f"git {args[0] if args else ''} failed: {detail}")


@dataclass(frozen=True)
class BaseResult:
    """The resolved base for a run (section 8.3)."""

    commit: str
    synthetic: bool
    parent: str | None
    branch: str | None
    sensitive_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    dirty_submodules: tuple[str, ...] = ()


def ref_namespace(run_id: str) -> str:
    """The `refs/relay/<run>` prefix that holds every ref owned by a run."""
    return f"{REFS_NAMESPACE}/{run_id}"


def base_ref(run_id: str) -> str:
    """The ref name of a run's synthetic base commit (only exists for `--allow-dirty` runs)."""
    return f"{ref_namespace(run_id)}/{BASE_REF_NAME}"


def scan_sensitive_paths(paths: Iterable[str]) -> list[str]:
    """Return the repo-relative paths in `paths` whose name looks sensitive (section 8.3).

    A path is sensitive when any of its components matches one of `SENSITIVE_PATTERNS`, so both
    `config/.env` and a `secrets/` directory tree are caught. Matching is case-insensitive and
    host-independent.
    """
    return sorted({p for p in paths if _is_sensitive(p)})


def is_reserved_path(path: str) -> bool:
    """True if a repo-relative path is `.git/**` or a runtime subtree (section 6)."""
    parts = tuple(segment.lower() for segment in path.replace("\\", "/").split("/") if segment)
    return any(parts[: len(root)] == root for root in RESERVED_ROOTS)


def resolve_base(
    repo_root: Path,
    run_id: str,
    *,
    allow_dirty: bool = False,
    exclude: Iterable[str] = (),
) -> BaseResult:
    """Resolve (and, for `--allow-dirty`, create) the pinned base commit for a run.

    A clean tree pins the current branch HEAD. With `allow_dirty`, a synthetic commit under
    `refs/relay/<run>/base` is created once and reused on later calls for the same run, so
    resume sees the original snapshot. `exclude` globs are additional paths to omit from the
    snapshot on top of the reserved runtime paths and the sensitive-path scan.
    """
    _validate_run_id(run_id)
    patterns = tuple(_normalize_exclude(p) for p in exclude)
    head = _head_sha(repo_root)
    branch = _branch_name(repo_root)

    if not allow_dirty:
        dirty = _dirty_paths(repo_root)
        if dirty:
            raise DirtyTreeError(dirty)
        return BaseResult(commit=head, synthetic=False, parent=None, branch=branch)

    existing = read_ref(repo_root, base_ref(run_id))
    if existing is not None:
        return BaseResult(
            commit=existing,
            synthetic=True,
            parent=_commit_parent(repo_root, existing),
            branch=branch,
        )

    sensitive, excluded, dirty_submodules = _build_synthetic_base(repo_root, run_id, head, patterns)
    commit = read_ref(repo_root, base_ref(run_id))
    if commit is None:
        raise GitBaseError("synthetic base was not written")
    return BaseResult(
        commit=commit,
        synthetic=True,
        parent=head,
        branch=branch,
        sensitive_paths=tuple(sensitive),
        excluded_paths=tuple(excluded),
        dirty_submodules=tuple(dirty_submodules),
    )


def read_ref(repo_root: Path, ref: str) -> str | None:
    """Return the commit a ref points at, or `None` when it does not exist."""
    _validate_ref(ref)
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def write_ref(repo_root: Path, ref: str, sha: str) -> None:
    """Point `ref` at `sha` (idempotent; creates the namespace directories as needed)."""
    _validate_ref(ref, namespace=True)
    _git(repo_root, "update-ref", ref, sha)


def _validate_ref(ref: str, *, namespace: bool = False) -> None:
    """Reject refs that could be mistaken for git options or injected into the command line.

    `read_ref` and `write_ref` splice `ref` into `rev-parse`/`update-ref`; a ref beginning
    with `-` or containing whitespace/control characters must never reach git unguarded.
    `write_ref` additionally requires the `refs/relay/` namespace, which is the only
    namespace relay may create refs under (section 8.3).
    """
    if ref.startswith("-"):
        raise InvalidRefError(f"ref {ref!r} must not begin with '-'")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in ref):
        raise InvalidRefError(f"ref {ref!r} must not contain whitespace or control characters")
    if namespace and not ref.startswith(f"{REFS_NAMESPACE}/"):
        raise InvalidRefError(f"ref {ref!r} must be under {REFS_NAMESPACE!r} to be written")


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID_RE.fullmatch(run_id) or run_id.startswith(".") or run_id.endswith("."):
        raise InvalidRefError(f"run id {run_id!r} is not a safe ref name")
    if run_id.endswith(".lock") or ".." in run_id or any(ch in _REF_UNSAFE for ch in run_id):
        raise InvalidRefError(f"run id {run_id!r} is not a safe ref name")


def _normalize_exclude(pattern: str) -> str:
    text = pattern.strip().replace("\\", "/")
    if not text:
        raise InvalidRefError("--exclude patterns must be non-empty strings")
    # Mirror classifier._split_path validation: absolute and parent-traversal patterns
    # are rejected with `ValueError` (design section 8.3; matching happens in
    # `_matches_exclude` via `match_path_glob`, which enforces the same rules).
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ValueError(f"--exclude pattern {pattern!r} must be repository-relative")
    parts = tuple(part for part in text.split("/") if part not in ("", "."))
    if ".." in parts:
        raise ValueError(f"--exclude pattern {pattern!r} must not contain '..'")
    return text


def _is_sensitive(path: str) -> bool:
    return any(
        fnmatchcase(part.lower(), pattern.lower())
        for part in path.replace("\\", "/").split("/")
        if part
        for pattern in SENSITIVE_PATTERNS
    )


_GLOB_META = frozenset("*?[")


def _matches_exclude(path: str, pattern: str) -> bool:
    """True if `path` is excluded by `pattern` (classifier.match_path_glob semantics).

    `**` crosses path segments; `*`, `?` and `[...]` stay within a single segment, matching
    git's wildmatch and `classifier.match_path_glob`. A literal pattern (no glob
    metacharacters) also matches everything beneath it, so `tmp/` and other directory
    prefixes keep their historical meaning.
    """
    if match_path_glob(path, pattern):
        return True
    if any(ch in pattern for ch in _GLOB_META):
        return False
    return match_path_glob(path, f"{pattern}/**")


def _run(
    repo_root: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            input=input,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise GitBaseError("git is not installed or not on PATH") from exc


def _git(
    repo_root: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input: str | None = None,
) -> str:
    proc = _run(repo_root, *args, env=env, input=input)
    if proc.returncode != 0:
        raise GitFailure(args, proc.stderr)
    return proc.stdout


def _head_sha(repo_root: Path) -> str:
    proc = _run(repo_root, "rev-parse", "--git-dir")
    if proc.returncode != 0:
        raise NotARepositoryError(
            "not inside a git repository; run 'git init' in the working directory first"
        )
    head = _run(repo_root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        raise EmptyRepositoryError(
            "repository has no commits yet; create an initial commit before running relay"
        )
    return head.stdout.strip()


def _branch_name(repo_root: Path) -> str | None:
    proc = _run(repo_root, "symbolic-ref", "--quiet", "HEAD")
    if proc.returncode != 0:
        return None  # detached HEAD
    ref = proc.stdout.strip()
    prefix = "refs/heads/"
    return ref[len(prefix) :] if ref.startswith(prefix) else ref


def _dirty_paths(repo_root: Path) -> list[str]:
    proc = _run(repo_root, "status", "--porcelain", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitFailure(("status",), proc.stderr)
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        path = line[3:].split(" -> ", 1)[-1]
        if path:
            paths.append(path)
    return paths


def _commit_parent(repo_root: Path, sha: str) -> str | None:
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{sha}^")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _submodule_paths(repo_root: Path) -> list[str]:
    """Repo-relative paths of every gitlink (submodule) recorded in the index."""
    proc = _run(repo_root, "ls-files", "--stage", "-z")
    if proc.returncode != 0:
        raise GitFailure(("ls-files",), proc.stderr)
    paths: list[str] = []
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        info, sep, path = entry.partition("\t")
        if sep and info.split(" ", 1)[0] == "160000":
            paths.append(path)
    return paths


def _dirty_submodules(repo_root: Path) -> list[str]:
    """Repo-relative paths of submodules whose working tree differs from the index.

    `git add -A` records only a submodule's gitlink, so a submodule with uncommitted changes,
    untracked files or a moved gitlink is captured lossily by the synthetic base. These paths
    are reported on `BaseResult.dirty_submodules` so task 1.14 (submodules/LFS) can act; full
    support is out of scope here.
    """
    submodules = _submodule_paths(repo_root)
    if not submodules:
        return []
    proc = _run(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitFailure(("status",), proc.stderr)
    submodule_set = set(submodules)
    dirty: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line:
            continue
        path = line[3:].split(" -> ", 1)[-1]
        if path in submodule_set:
            dirty.add(path)
    return sorted(dirty)


def _build_synthetic_base(
    repo_root: Path,
    run_id: str,
    head: str,
    excludes: tuple[str, ...],
) -> tuple[list[str], list[str], list[str]]:
    """Create the `refs/relay/<run>/base` commit; returns (sensitive, excluded, dirty)."""
    dirty_submodules = _dirty_submodules(repo_root)
    # Untracked non-ignored files are scanned against the real index, not the temp one.
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    sensitive = scan_sensitive_paths(untracked.split("\0"))
    sensitive_set = set(sensitive)

    index_dir = Path(tempfile.mkdtemp(prefix="relay-index-"))
    try:
        index_path = index_dir / "index"
        env = {"GIT_INDEX_FILE": str(index_path)}
        _git(repo_root, "read-tree", head, env=env)
        _git(repo_root, "add", "-A", env=env)

        staged = _git(repo_root, "ls-files", "-z", env=env).split("\0")
        remove = sorted({p for p in staged if p and _snapshot_excluded(p, sensitive_set, excludes)})
        if remove:
            _git(
                repo_root,
                "update-index",
                "--force-remove",
                "-z",
                "--stdin",
                input="\0".join(remove) + "\0",
                env=env,
            )

        tree = _git(repo_root, "write-tree", env=env).strip()
        message = f"relay: synthetic base for run {run_id}"
        commit = _git(
            repo_root, "commit-tree", tree, "-p", head, "-m", message, env=_COMMIT_ENV
        ).strip()
        write_ref(repo_root, base_ref(run_id), commit)
        return sensitive, remove, dirty_submodules
    finally:
        shutil.rmtree(index_dir, ignore_errors=True)


def _snapshot_excluded(path: str, sensitive: set[str], excludes: tuple[str, ...]) -> bool:
    if is_reserved_path(path):
        return True
    if path in sensitive:
        return True
    return any(_matches_exclude(path, pattern) for pattern in excludes)
