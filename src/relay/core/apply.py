"""Delivery of a run's integration commit into a user branch (design sections 8.3 and 14).

``relay apply <run>`` is the privileged delivery step: it takes the run's verified integration
commit (``refs/heads/relay/<run>/integration``, section 8.3) and makes it available on a target
branch. This module is the git-mode core only -- there is no CLI, confirmation prompt,
``--yes`` handling, event writing, verification, ``--force`` or shadow-git support here (those
belong to task 1.24b and later phases).

Two phases, deliberately separate:

* :func:`prepare_apply` resolves the run's integration commit and the target branch, then builds
  the commit that applying would produce -- a fast-forward, a no-op, or a real merge performed
  in a disposable detached worktree at the target tip. It never touches the user's branch, HEAD,
  index or working tree. It then computes the target-to-prepared delta, checks it against the
  user's dirty paths (staged, unstaged and untracked) and raises :class:`DirtyOverlapError` when
  the delivery would overwrite concurrent user edits. Paths under the reserved runtime subtrees
  (section 6) never count as user dirt, and temporary files from a previous ``--allow-dirty`` run
  are recognized by comparing the working-tree blob with the run's baseline blob.
* :func:`commit_ref` installs a prepared plan with a single compare-and-swap ref update
  (``git update-ref <ref> <new> <old>``). It never force-updates and never touches the working
  tree or index; a moved target raises :class:`TargetMovedError`.

The merge commit uses the package's pinned ``relay`` orchestrator identity and timestamps and a
deterministic message, so preparing the same run against the same target twice yields the same
commit sha. The disposable worktree is always removed, including on conflict.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .events import EventLogCorrupt, read_events
from .gitbase import base_ref, is_reserved_path, read_ref
from .publish import PublicationError, integration_branch
from .store import RunStore, StoreError

# Pinned identity and timestamps for the prepared merge commit. Same convention as output
# publication: the orchestrator's identity, never an agent's or the user's, and raw
# `<unix> <tz>` dates so the same inputs always produce the same merge sha.
_ORCHESTRATOR_ENV = {
    "GIT_AUTHOR_NAME": "relay",
    "GIT_AUTHOR_EMAIL": "relay@example.invalid",
    "GIT_COMMITTER_NAME": "relay",
    "GIT_COMMITTER_EMAIL": "relay@example.invalid",
    "GIT_AUTHOR_DATE": "1000000000 +0000",
    "GIT_COMMITTER_DATE": "1000000000 +0000",
}

# As the `<old>` of `git update-ref`, all zeros require the ref to be absent.
_ZERO_OID = "0" * 40

# Run ids are path-safe single segments (design section 6; same shape as the publication ids).
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


class ApplyError(RuntimeError):
    """Delivery of a run's integration commit failed."""


class RunMissingError(ApplyError):
    """The run directory or its ``run.json`` does not exist."""


class RunNotDoneError(ApplyError):
    """The run is not in the ``done`` state, so there is nothing to apply."""

    def __init__(self, run_id: str, state: str) -> None:
        super().__init__(f"run {run_id} is {state!r}, not 'done'; nothing to apply")
        self.state = state


class IntegrationMissingError(ApplyError):
    """The run has no usable integration commit (no record, event or branch)."""


class TargetRefError(ApplyError):
    """The target branch cannot be used: detached HEAD, unsafe ref, or no base commit."""


class MergeConflictError(ApplyError):
    """Preparing the merge hit textual conflicts; the user's checkout was not touched."""

    def __init__(self, conflicts: list[str]) -> None:
        preview = ", ".join(conflicts[:8])
        more = f" and {len(conflicts) - 8} more" if len(conflicts) > 8 else ""
        super().__init__(
            f"applying the run conflicts on {len(conflicts)} path(s): {preview}{more}; "
            "nothing was changed"
        )
        self.conflicts = list(conflicts)


class DirtyOverlapError(ApplyError):
    """User working-tree edits overlap the paths the delivery would change."""

    def __init__(self, overlaps: list[str]) -> None:
        preview = ", ".join(overlaps[:8])
        more = f" and {len(overlaps) - 8} more" if len(overlaps) > 8 else ""
        super().__init__(
            f"user edits overlap {len(overlaps)} path(s) the run would change: {preview}{more}; "
            "commit or stash them first"
        )
        self.overlaps = list(overlaps)


class TargetMovedError(ApplyError):
    """The target ref changed between preparation and installation."""

    def __init__(self, ref: str, expected: str | None, observed: str | None) -> None:
        exp = "absent" if expected is None else expected[:12]
        obs = "absent" if observed is None else observed[:12]
        super().__init__(f"target {ref} moved during apply (expected {exp}, observed {obs})")
        self.ref = ref
        self.expected = expected
        self.observed = observed


class GitApplyError(ApplyError):
    """A git command failed unexpectedly while preparing or installing the delivery."""

    def __init__(self, args: tuple[str, ...], stderr: str) -> None:
        detail = stderr.strip() or "no output"
        super().__init__(f"git {args[0] if args else ''} failed: {detail}")
        self.args = args


@dataclass(frozen=True)
class ApplyPlan:
    """A prepared, not yet installed delivery (design section 8.3).

    ``prepared_commit`` is the commit the target branch would point at: the integration commit
    for a fast-forward, the target tip when the run is already contained, or a deterministic
    merge commit of ``target_tip`` and ``integration_commit``. ``expected_old`` is the target ref
    value observed during preparation (``None`` for a new branch), used as the compare-and-swap
    guard at installation. ``changed_paths`` is the delta ``target_tip..prepared_commit`` (both
    sides of renames); ``overlaps`` and ``conflicts`` are empty on a returned plan because both
    conditions raise instead.
    """

    run_id: str
    integration_commit: str
    prepared_commit: str
    prepared_tree: str
    target_ref: str
    target_tip: str
    expected_old: str | None
    changed_paths: tuple[str, ...]
    overlaps: tuple[str, ...]
    conflicts: tuple[str, ...]
    base_commit: str

    @property
    def clean(self) -> bool:
        """True when no conflict or dirty overlap blocks installation."""
        return not self.overlaps and not self.conflicts

    @property
    def noop(self) -> bool:
        """True when the target already contains the run (nothing would change)."""
        return self.prepared_commit == self.target_tip


@dataclass(frozen=True)
class ApplyResult:
    """What :func:`commit_ref` did: the commit the target now holds and whether it moved."""

    prepared_commit: str
    target_ref: str
    changed: bool


def resolve_integration_commit(repo_root: Path, run_id: str) -> str:
    """Resolve the run's integration commit (design sections 6 and 8.3).

    Resolution order, first usable source wins:

    1. ``run.json``: the run must be ``done`` and its ``final_commit`` a commit;
    2. the last ``run.finished {integration_commit}`` event in ``events.jsonl``;
    3. the integration branch ``refs/heads/relay/<run>/integration``.

    Every value is validated as a commit in this repository and returned as its canonical full
    sha. A missing run, a run that is not ``done``, and a ``done`` run with no integration
    commit raise :class:`RunMissingError`, :class:`RunNotDoneError` and
    :class:`IntegrationMissingError` respectively.
    """
    _validate_run_id(run_id)
    store = _open_run(repo_root, run_id)
    try:
        record = store.read_run()
    except (OSError, ValueError) as exc:
        raise ApplyError(f"cannot read run {run_id}: {exc}") from exc

    if record.state != "done":
        raise RunNotDoneError(run_id, record.state)

    if record.final_commit is not None:
        return _require_commit(repo_root, record.final_commit, "run integration commit")

    from_event = _integration_from_events(store)
    if from_event is not None:
        return _require_commit(repo_root, from_event, "run integration commit")

    branch = _integration_branch(run_id)
    from_ref = read_ref(repo_root, branch)
    if from_ref is not None:
        return _require_commit(repo_root, from_ref, "run integration commit")

    raise IntegrationMissingError(
        f"run {run_id} is done but has no integration commit (no final_commit, no "
        "run.finished event, and no integration branch)"
    )


def prepare_apply(
    repo_root: Path,
    run_id: str,
    *,
    target: str | None = None,
    source_commit: str | None = None,
    baseline_commit: str | None = None,
) -> ApplyPlan:
    """Prepare delivery of a run into a branch, without changing the user's checkout.

    ``source_commit`` delivers that commit instead of the run's own integration commit; the
    shared session delivery service (task 2.27) uses it for a cumulative round or a sealed
    checkpoint, and has already validated the commit. ``run_id`` then only names the operation
    (merge message, events). ``baseline_commit`` replaces the run's recorded base when
    recognizing pre-existing ``--allow-dirty`` state, because a session's original snapshot is
    the first round's base, not the retained base of the last round.

    ``target`` is ``None`` for the currently checked-out branch (a detached HEAD is refused), a
    short branch name (``feature`` -> ``refs/heads/feature``), or a full ref under
    ``refs/heads/``. A branch that does not exist yet is planned from the current ``HEAD`` and
    created on installation; any other target is planned from its current tip.

    The merge, when one is needed, happens in a disposable detached worktree at the target tip.
    Conflicts raise :class:`MergeConflictError` with the unmerged paths. The prepared commit is
    deterministic, and the worktree is removed on every path, including conflicts.

    Raises :class:`DirtyOverlapError` when staged, unstaged or untracked user edits overlap the
    prepared delta. Reserved runtime subtrees are ignored, and paths whose working-tree blob
    equals the run's baseline blob are treated as pre-existing ``--allow-dirty`` state, not
    concurrent edits.
    """
    _validate_run_id(run_id)
    if source_commit is None:
        integration_commit = resolve_integration_commit(repo_root, run_id)
    else:
        integration_commit = _require_commit(repo_root, source_commit, "delivery source commit")
    target_ref, target_tip, expected_old = _resolve_target(repo_root, target)
    if baseline_commit is None:
        base_commit = _resolve_base_commit(repo_root, run_id, target_tip, integration_commit)
    else:
        base_commit = _require_commit(repo_root, baseline_commit, "delivery baseline commit")

    if _is_ancestor(repo_root, integration_commit, target_tip):
        # The target already contains the run: there is nothing to deliver.
        prepared_commit = target_tip
        changed: tuple[str, ...] = ()
        conflicts: tuple[str, ...] = ()
    elif _is_ancestor(repo_root, target_tip, integration_commit):
        prepared_commit = integration_commit
        changed = _changed_paths(repo_root, target_tip, prepared_commit)
        conflicts = ()
    else:
        prepared_commit, changed, conflicts = _prepare_merge(
            repo_root, run_id, target_ref, target_tip, integration_commit
        )

    prepared_tree = _commit_tree(repo_root, prepared_commit)
    current = _run(repo_root, "symbolic-ref", "--quiet", "HEAD")
    dirty: set[str] = _dirty_paths(repo_root) if current.stdout.strip() == target_ref else set()
    overlaps = _overlap_paths(changed, dirty)
    overlaps = [path for path in overlaps if not _matches_baseline(repo_root, base_commit, path)]
    if overlaps:
        raise DirtyOverlapError(overlaps)

    return ApplyPlan(
        run_id=run_id,
        integration_commit=integration_commit,
        prepared_commit=prepared_commit,
        prepared_tree=prepared_tree,
        target_ref=target_ref,
        target_tip=target_tip,
        expected_old=expected_old,
        changed_paths=changed,
        overlaps=(),
        conflicts=conflicts,
        base_commit=base_commit,
    )


def commit_ref(repo_root: Path, plan: ApplyPlan) -> ApplyResult:
    """Install a prepared plan by compare-and-swap on the target branch (design section 8.3).

    Only ``refs/heads/...`` may be updated. ``git update-ref <ref> <new> <old>`` is never forced:
    if the target moved after preparation, or was created concurrently, the update fails and
    :class:`TargetMovedError` reports the observed value. The working tree and index are not
    touched. A plan whose target already points at the prepared commit is a no-op
    (``changed`` is ``False``).
    """
    _validate_target_refname(repo_root, plan.target_ref)
    if not plan.target_ref.startswith("refs/heads/"):
        raise TargetRefError(f"target {plan.target_ref!r} is not a branch under refs/heads/")
    _require_commit(repo_root, plan.prepared_commit, "prepared commit")
    if plan.expected_old is not None:
        _require_commit(repo_root, plan.expected_old, "expected target commit")

    # Still run the guard below: a concurrent deletion of the ref must be detected even though
    # an already-installed plan would not change its value.
    changed = plan.expected_old != plan.prepared_commit

    proc = _run(
        repo_root,
        "update-ref",
        plan.target_ref,
        plan.prepared_commit,
        plan.expected_old or _ZERO_OID,
    )
    if proc.returncode == 0:
        return ApplyResult(
            prepared_commit=plan.prepared_commit, target_ref=plan.target_ref, changed=changed
        )

    observed = read_ref(repo_root, plan.target_ref)
    raise TargetMovedError(plan.target_ref, plan.expected_old, observed)


def install_worktree(repo_root: Path, plan: ApplyPlan) -> bool:
    """Install the delivered paths when the target is the current branch.

    Only ``target_tip..prepared`` paths are restored, staged and written from the prepared
    commit. Staged, unstaged and untracked user edits outside that delta are left exactly as
    they are (design section 8.3). A new or non-current branch is never checked out. Returns
    whether the working tree was updated.
    """
    proc = _run(repo_root, "symbolic-ref", "--quiet", "HEAD")
    if proc.returncode != 0 or proc.stdout.strip() != plan.target_ref:
        return False
    if not plan.changed_paths:
        return False
    restore = _run(
        repo_root,
        "restore",
        f"--source={plan.prepared_commit}",
        "--staged",
        "--worktree",
        "--",
        *plan.changed_paths,
    )
    if restore.returncode != 0:
        raise GitApplyError(("restore", plan.prepared_commit), restore.stderr)
    return True


def changed_paths(repo_root: Path, old: str, new: str) -> tuple[str, ...]:
    """Paths that differ between two commits (both sides of renames); see :func:`prepare_apply`."""
    return _changed_paths(repo_root, old, new)


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant`` (a commit is its own ancestor)."""
    return _is_ancestor(repo_root, ancestor, descendant)


@contextmanager
def detached_checkout(repo_root: Path, commit: str) -> Generator[Path]:
    """A disposable detached worktree at ``commit``, removed on every path.

    Used to run verification against an exact prepared tree without touching the user's
    checkout. Removal is best-effort and never masks the caller's failure.
    """
    with tempfile.TemporaryDirectory(prefix="relay-apply-verify-") as temp:
        worktree = Path(temp) / "worktree"
        make = _run(repo_root, "worktree", "add", "--detach", str(worktree), commit)
        if make.returncode != 0:
            raise GitApplyError(("worktree", "add", commit), make.stderr)
        try:
            yield worktree
        finally:
            _remove_worktree(repo_root, worktree)


# --- run resolution ---------------------------------------------------------------------------


def _validate_run_id(run_id: str) -> None:
    if not _RUN_ID_RE.fullmatch(run_id) or ".." in run_id or run_id.endswith(".lock"):
        raise ApplyError(f"run id {run_id!r} is not a safe ref name")


def _open_run(repo_root: Path, run_id: str) -> RunStore:
    try:
        store = RunStore.open(repo_root, run_id)
    except StoreError as exc:
        raise RunMissingError(f"run {run_id} not found: {exc}") from exc
    if not store.run_json.is_file():
        raise RunMissingError(f"run {run_id} has no run.json under {store.run_dir}")
    return store


def _integration_from_events(store: RunStore) -> str | None:
    try:
        events = read_events(store.events_path)
    except EventLogCorrupt as exc:
        raise ApplyError(f"run {store.run_id} has a corrupt event log: {exc}") from exc
    for event in reversed(events):
        if event.type != "run.finished":
            continue
        value = event.data.get("integration_commit")
        if isinstance(value, str) and value:
            return value
    return None


def _integration_branch(run_id: str) -> str:
    try:
        return integration_branch(run_id)
    except PublicationError as exc:  # publication owns the reserved-ref validation
        raise ApplyError(f"run id {run_id!r} cannot name an integration branch: {exc}") from exc


def _require_commit(repo_root: Path, sha: str, what: str) -> str:
    """Return the canonical commit ``sha`` names, or raise a clear apply error."""
    if not _SHA_RE.fullmatch(sha):
        raise IntegrationMissingError(f"{what} {sha!r} is not a commit id")
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if proc.returncode != 0:
        raise IntegrationMissingError(f"{what} {sha!r} is not a commit in this repository")
    return proc.stdout.strip()


def _resolve_base_commit(
    repo_root: Path, run_id: str, target_tip: str, integration_commit: str
) -> str:
    """The run's pinned baseline, used only to recognize pre-existing dirty state.

    Preference order: the ``run.started`` event's ``base_commit`` (the ordinary or synthetic
    ``--allow-dirty`` base recorded at run start), the synthetic base ref
    ``refs/relay/<run>/base``, then the merge base of the target and the integration commit.
    The last fallback keeps a hand-built run record usable; it never affects install decisions
    beyond recognizing a dirty path that already equals that snapshot.
    """
    store = _open_run(repo_root, run_id)
    try:
        events = read_events(store.events_path)
    except EventLogCorrupt as exc:
        raise ApplyError(f"run {run_id} has a corrupt event log: {exc}") from exc
    for event in events:
        if event.type != "run.started":
            continue
        value = event.data.get("base_commit")
        if isinstance(value, str) and value:
            canonical = _try_commit(repo_root, value)
            if canonical is not None:
                return canonical
    synthetic = read_ref(repo_root, base_ref(run_id))
    if synthetic is not None:
        return synthetic
    proc = _run(repo_root, "merge-base", target_tip, integration_commit)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return target_tip


def _try_commit(repo_root: Path, sha: str) -> str | None:
    if not _SHA_RE.fullmatch(sha):
        return None
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


# --- target resolution ------------------------------------------------------------------------


def _resolve_target(repo_root: Path, target: str | None) -> tuple[str, str, str | None]:
    """Return ``(target_ref, target_tip, expected_old)`` without changing anything."""
    ref = _current_branch_ref(repo_root) if target is None else _normalize_target(repo_root, target)
    existing = read_ref(repo_root, ref)
    if existing is not None:
        return ref, existing, existing
    # A new branch forks from the current HEAD; ``expected_old`` stays None so installation
    # creates it with a compare-and-swap on absence.
    return ref, _head_commit(repo_root), None


def _current_branch_ref(repo_root: Path) -> str:
    proc = _run(repo_root, "symbolic-ref", "--quiet", "HEAD")
    if proc.returncode != 0:
        raise TargetRefError(
            "HEAD is detached; pass an explicit target branch, or check out a branch first"
        )
    ref = proc.stdout.strip()
    if not ref.startswith("refs/heads/"):
        raise TargetRefError(f"HEAD points at {ref!r}, not at a local branch")
    _validate_target_refname(repo_root, ref)
    return ref


def _normalize_target(repo_root: Path, target: str) -> str:
    if not target:
        raise TargetRefError("target branch must not be empty")
    if target.startswith("refs/"):
        if not target.startswith("refs/heads/") or target == "refs/heads/":
            raise TargetRefError(
                f"target {target!r} must be a local branch under refs/heads/ "
                "(remote-tracking refs and tags are not targets)"
            )
        ref = target
    else:
        if target.startswith("-"):
            raise TargetRefError(f"target {target!r} must not begin with '-'")
        if target == "HEAD":
            raise TargetRefError("target 'HEAD' is not a branch; name the branch explicitly")
        ref = f"refs/heads/{target}"
    _validate_target_refname(repo_root, ref)
    return ref


def _validate_target_refname(repo_root: Path, ref: str) -> None:
    if not ref.startswith("refs/heads/") or ref == "refs/heads/":
        raise TargetRefError(f"target {ref!r} is not a local branch under refs/heads/")
    proc = _run(repo_root, "check-ref-format", ref)
    if proc.returncode != 0:
        raise TargetRefError(f"target {ref!r} is not a valid branch name")


def _head_commit(repo_root: Path) -> str:
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if proc.returncode != 0:
        raise TargetRefError(
            "repository has no commits yet; create an initial commit before applying a run"
        )
    return proc.stdout.strip()


# --- merge preparation ------------------------------------------------------------------------


def _prepare_merge(
    repo_root: Path,
    run_id: str,
    target_ref: str,
    target_tip: str,
    integration_commit: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Merge the integration commit into the target tip in a disposable worktree.

    Returns ``(prepared_commit, changed_paths, conflicts)``; a conflict raises instead, so
    ``conflicts`` is always empty on return. The worktree is removed in ``finally``.
    """
    with tempfile.TemporaryDirectory(prefix="relay-apply-") as temp:
        root = Path(temp)
        worktree = root / "worktree"
        hooks = root / "hooks"
        hooks.mkdir()
        added = False
        try:
            make = _run(repo_root, "worktree", "add", "--detach", str(worktree), target_tip)
            if make.returncode != 0:
                raise GitApplyError(("worktree", "add", target_tip), make.stderr)
            added = True

            message = f"relay: apply {run_id} to {target_ref}"
            merge = _run(
                worktree,
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={hooks}",
                "merge",
                "--no-ff",
                "--no-edit",
                "-m",
                message,
                integration_commit,
                env=_ORCHESTRATOR_ENV,
            )
            if merge.returncode != 0:
                conflicts = _conflict_paths(worktree)
                if conflicts:
                    raise MergeConflictError(conflicts)
                raise GitApplyError(("merge", integration_commit), merge.stderr)

            prepared = _rev_parse(worktree, "HEAD")
            changed = _changed_paths(repo_root, target_tip, prepared)
            return prepared, changed, ()
        finally:
            if added:
                _remove_worktree(repo_root, worktree)


def _conflict_paths(worktree: Path) -> list[str]:
    proc = _run(worktree, "diff", "--name-only", "--diff-filter=U")
    if proc.returncode != 0:
        raise GitApplyError(("diff", "--diff-filter=U"), proc.stderr)
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _remove_worktree(repo_root: Path, worktree: Path) -> None:
    """Best-effort removal of the disposable worktree; never masks the original failure."""
    _run(repo_root, "worktree", "remove", "--force", str(worktree))
    _run(repo_root, "worktree", "prune")


def _commit_tree(repo_root: Path, commit: str) -> str:
    return _rev_parse(repo_root, f"{commit}^{{tree}}")


def _rev_parse(repo_root: Path, revision: str) -> str:
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", revision)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise GitApplyError(("rev-parse", revision), proc.stderr)
    return proc.stdout.strip()


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    proc = _run(repo_root, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitApplyError(("merge-base", "--is-ancestor", ancestor, descendant), proc.stderr)


# --- changed and dirty paths ------------------------------------------------------------------


def _changed_paths(repo_root: Path, old: str, new: str) -> tuple[str, ...]:
    """Delta ``old..new``; rename sides are both listed because rename detection is disabled."""
    proc = _run(
        repo_root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--diff-filter=ACDMRTUXB",
        old,
        new,
    )
    if proc.returncode != 0:
        raise GitApplyError(("diff", old, new), proc.stderr)
    return tuple(sorted({path for path in proc.stdout.split("\0") if path}))


def _dirty_paths(repo_root: Path) -> set[str]:
    """Staged, unstaged and untracked user paths, excluding reserved runtime subtrees.

    ``-z`` emits rename entries as ``XY new\\0old\\0``; both sides are included so a rename that
    collides with either side of the delivery is detected. Only non-ignored paths appear.
    """
    proc = _run(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if proc.returncode != 0:
        raise GitApplyError(("status", "--porcelain=v1"), proc.stderr)

    entries = proc.stdout.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry or len(entry) < 4:
            continue
        path = entry[3:]
        paths.add(path)
        if (entry[0] in "RC" or entry[1] in "RC") and index < len(entries) and entries[index]:
            paths.add(entries[index])
            index += 1
    return {path for path in paths if path and not is_reserved_path(path)}


def _overlap_paths(changed: tuple[str, ...], dirty: set[str]) -> list[str]:
    """Exact matches plus directory/file collisions between the delta and user edits.

    An untracked file ``docs`` and a delivered ``docs/readme.md`` do not intersect as strings,
    but installing the second would fail or overwrite the first; the same holds in reverse.
    Both the user path and the delivered path are reported so the message is actionable.
    """
    changed_set = set(changed)
    hits = changed_set & dirty
    for path in changed_set:
        for prefix in _ancestors(path):
            if prefix in dirty:
                hits.update((prefix, path))
    for path in dirty:
        for prefix in _ancestors(path):
            if prefix in changed_set:
                hits.update((prefix, path))
    return sorted(hits)


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _matches_baseline(repo_root: Path, base_commit: str, path: str) -> bool:
    """True when the working-tree file for ``path`` equals its blob in the run baseline.

    That makes the path pre-existing ``--allow-dirty`` state rather than a concurrent edit. A
    missing working-tree file (a deletion) or a path absent from the baseline never matches.
    """
    # A matching working file cannot excuse separately staged edits or mode changes.
    index_delta = _run(repo_root, "diff", "--cached", "--quiet", "HEAD", "--", path)
    if index_delta.returncode != 0:
        baseline_index_delta = _run(
            repo_root, "diff", "--cached", "--quiet", base_commit, "--", path
        )
        if baseline_index_delta.returncode != 0:
            return False
    working = _run(repo_root, "hash-object", f"--path={path}", "--", path)
    if working.returncode != 0:
        return False
    blob = working.stdout.strip()
    baseline = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{base_commit}:{path}")
    if baseline.returncode != 0:
        return False
    return baseline.stdout.strip() == blob


# --- subprocess helpers -----------------------------------------------------------------------


def _run(
    repo_root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one git command with an explicit argument list (never a shell)."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise GitApplyError(args, "git is not installed or not on PATH") from exc
