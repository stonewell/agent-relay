"""Output publication for a verified node (design sections 8.1 and 8.3, task 1.17 part A).

After capture (task 1.15) and verification (task 1.16) succeed, the orchestrator publishes the
node's output: it creates the *orchestrator-owned* output commit (parent = the node's immutable
``input_commit``, tree = the captured candidate tree, author/committer = the orchestrator identity
-- never the agent's), points the node's output ref at it under the ``refs/relay/<run>/...``
namespace, and writes the diff artifact into the node's run directory. The user's HEAD and
branches are never touched.

Ownership. Agents are asked to leave edits uncommitted; the orchestrator alone creates the output
commit (design section 8.3). The agent's identity is never reused: the commit is authored by the
fixed ``relay`` placeholder identity used across the package.

Guards, in order:

1. Failed (or otherwise non-publishable) verification publishes nothing: the guard raises before
   any commit, ref or artifact is created (design section 10 item 5).
2. The candidate tree must not contain reserved runtime paths (``.git/**`` and the ``.agents/``
   runtime subtrees, design section 6). Capture already filters them, so this is defence in
   depth: a reserved path present in the candidate tree is refused, never silently stripped.

Idempotency. Publication is deterministic: the same input commit, candidate tree and message
always produce the same output commit sha (the parent is always ``input_commit``, never the
current ref), regardless of the current ref's history. Republishing:

* the same node with the same candidate is a no-op -- the existing ref is left in place and the
  artifact (deterministic content at a fixed path) is rewritten in place, never duplicated;
* an output ref that already exists is only ever moved to the equal commit, a descendant, or (for
  a same-node retry with a new ``attempt``) a sibling output parented on the same input commit; a
  republish that would move the ref to an ancestor or an unrelated commit is refused.

Concurrency. Every publication ref update is a compare-and-swap (``git update-ref <ref> <new>
<old>``) against the value observed before any write, so a concurrent writer (the phase 2
supervisor) cannot be raced past a read. A lost creation race on the attempt ref is reconciled
idempotently when the foreign value equals the output; a lost current-ref race rolls back only an
attempt ref this call created, leaving the current ref and any pre-existing attempt ref unchanged.

Part B adds the run's integration branch and immutable per-attempt output refs:

* The integration branch ``refs/heads/relay/<run>/integration`` merges every *successful* node
  output into one deterministic commit (design section 8.3). A failed/refused node publishes
  nothing and is absent from the branch. A single-node run points the branch at that node's
  output commit; a multi-node run merges the successful outputs in deterministic node-id order,
  fast-forwarding where possible and otherwise creating real merge commits with the contributing
  commits as parents (never flattened to a single parent). A merge conflict raises
  :class:`IntegrationConflictError` and leaves the branch untouched -- the conflict-resolution
  node is phase 2a (task 2.6) and out of scope here.
* Per-attempt output refs (``refs/relay/<run>/<node>@<attempt>``) are immutable. Retrying a node
  that already published advances the node's current ref to the *sibling* output commit for the
  new attempt -- every output commit is parented on the node's ``input_commit``, so retries are
  siblings, not descendants -- while the prior attempt stays reachable through its per-attempt ref
  so worktree pruning and garbage collection never lose it (design section 8.3).

Ref-component safety. A node id or run id is validated before any ref is read or written. The
ids are path-safe single segments, and two names are additionally reserved because an
orchestrator-owned ref already occupies them: ``base`` (the synthetic base ref
``refs/relay/<run>/base``, design section 8.3) and ``integration`` (the integration branch
``refs/heads/relay/<run>/integration``, which is also the worktree branch slot a node id named
``integration`` would occupy). The design names ``base`` explicitly but does not name
``integration`` as a reserved *node* id; it is reserved here conservatively (case-insensitively,
so the check errs toward rejecting on every host) so a node id can never collide with the
integration branch. See :data:`RESERVED_REF_COMPONENTS` and :func:`_validate_component`.

``relay apply``, ``relay run``, shadow-git and integration-acceptance command execution are out
of scope here.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .capture import CaptureResult
from .gitbase import is_reserved_path, read_ref, ref_namespace
from .store import atomic_write
from .verify import VerifyResult

# Author/committer identity for the orchestrator-owned output commit. Same placeholder
# convention as the rest of the package: the orchestrator's identity, never the agent's.
# The timestamps are pinned (raw `<unix> <tz>` form) so the commit -- and therefore the output
# sha that downstream `input_key`s depend on -- is byte-identical on every republish.
_ORCHESTRATOR_ENV = {
    "GIT_AUTHOR_NAME": "relay",
    "GIT_AUTHOR_EMAIL": "relay@example.invalid",
    "GIT_COMMITTER_NAME": "relay",
    "GIT_COMMITTER_EMAIL": "relay@example.invalid",
    "GIT_AUTHOR_DATE": "1000000000 +0000",
    "GIT_COMMITTER_DATE": "1000000000 +0000",
}

# Diff artifact placement inside the node directory (design sections 5.3 and 6).
ARTIFACTS_DIR = "artifacts"
DIFF_ARTIFACT_NAME = "diff.patch"
DIFF_ARTIFACT_TYPE = "diff"

# Integration branch suffix: `refs/heads/relay/<run>/integration` (design sections 6 and 8.3).
INTEGRATION_BRANCH_SUFFIX = "integration"

# Ref components are path-safe ids (spec `NodeId`/`RunId`); reject anything that could be
# mistaken for a git option, a path separator, or whitespace before it reaches git.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# Ref-component names that already belong to an orchestrator-owned ref, so a node id (or run id)
# equal to one would collide (design sections 6 and 8.3):
#   * `base`        -> `refs/relay/<run>/base`, the synthetic base ref (gitbase.BASE_REF_NAME);
#   * `integration` -> `refs/heads/relay/<run>/integration`, the integration branch, which is
#                     also the worktree branch slot `refs/heads/relay/<run>/<node>` a node id
#                     named `integration` would occupy.
# `integration` is not named as a reserved node id in the design; it is reserved conservatively
# so the node-id namespace stays disjoint from the integration branch. Matching is
# case-insensitive: git refs are case-sensitive on case-sensitive filesystems but collide on
# case-insensitive ones, so the check errs toward rejecting on every host (section 8.1).
RESERVED_REF_COMPONENTS = frozenset({"base", "integration"})

# The all-zero object id: as the `<old>` of `git update-ref` it requires the ref to be absent.
_ZERO_OID = "0" * 40


class PublicationError(RuntimeError):
    """Output publication failed."""


class PublicationRefusedError(PublicationError):
    """Verification did not allow publication; nothing was created (design section 10 item 5)."""


class ReservedPathError(PublicationError):
    """The candidate tree contains a reserved runtime path; refusing rather than stripping."""


class OutputRefConflictError(PublicationError):
    """The output ref exists and the republish would move it to a non-descendant commit."""


class IntegrationConflictError(PublicationError):
    """The integration branch could not be updated without losing work; it was left untouched.

    Raised both when the merge itself hits a conflict (a run failure, design section 8.3) and
    when the branch would be moved to a non-descendant commit (a backward move, a sibling, or a
    rebuild that omits already-integrated output). Automated conflict resolution via a
    ``resolve-conflict`` node arrives with phase 2a (task 2.6) and is not performed here.
    """


class GitCommandError(PublicationError):
    """A git command failed unexpectedly."""

    def __init__(self, args: tuple[str, ...], stderr: str) -> None:
        detail = stderr.strip() or "no output"
        super().__init__(f"git {args[0] if args else ''} failed: {detail}")
        self.args = args


@dataclass(frozen=True)
class PublicationResult:
    """What :func:`publish` did (design section 8.3).

    ``output_commit`` is the orchestrator-owned commit (or the reused ``input_commit`` for a
    no-change node). ``ref`` is the full output ref (``refs/relay/<run>/<node>``) now pointing
    at it. ``artifact_path`` is the diff artifact in the node directory. ``reused`` is true when
    the commit already existed and no new commit was created: either the current ref already
    pointed at ``output_commit``, or its immutable per-attempt ref already recorded it. The
    current ref is never moved backward; a missing or behind current ref (a prior partial write)
    is recovered forward with a compare-and-swap and still reports ``reused`` because the commit
    itself was reused. ``attempt_ref`` is the immutable per-attempt ref written for this attempt
    (``None`` when no ``attempt`` was given).
    """

    output_commit: str
    ref: str
    artifact_path: Path
    reused: bool
    attempt_ref: str | None = None


@dataclass(frozen=True)
class IntegrationResult:
    """What :func:`publish_integration` did (design section 8.3).

    ``commit`` is the integration commit that ``branch`` (``refs/heads/relay/<run>/integration``)
    now points at. ``base_commit`` is the pinned run base the outputs were merged onto. ``nodes``
    is the deterministic merge order -- ``(node_id, output_commit)`` pairs for the *successful*
    nodes only, in ascending node-id order. ``merged`` is true when at least one real
    (non-fast-forward) merge commit was created.
    """

    commit: str
    branch: str
    base_commit: str
    nodes: tuple[tuple[str, str], ...]
    merged: bool


def node_ref(run_id: str, node_id: str) -> str:
    """The durable output ref for a node: ``refs/relay/<run>/<node>`` (sections 6, 8.3)."""
    _validate_component(run_id, "run id")
    _validate_component(node_id, "node id")
    return f"{ref_namespace(run_id)}/{node_id}"


def attempt_ref(run_id: str, node_id: str, attempt: int) -> str:
    """The immutable per-attempt output ref: ``refs/relay/<run>/<node>@<attempt>`` (section 8.3).

    Each successful attempt gets its own ref that is never moved, so a retry that advances the
    node's current ref leaves the prior attempt reachable for pruning/GC. ``@`` cannot appear in
    a node id (the ids are ``[A-Za-z0-9._-]``), so the two namespaces cannot collide.
    """
    if attempt < 1:
        raise PublicationError(f"attempt must be >= 1, got {attempt}")
    return f"{node_ref(run_id, node_id)}@{attempt}"


def integration_branch(run_id: str) -> str:
    """The run's integration branch: ``refs/heads/relay/<run>/integration`` (sections 6, 8.3)."""
    _validate_component(run_id, "run id")
    return f"refs/heads/relay/{run_id}/{INTEGRATION_BRANCH_SUFFIX}"


def diff_artifact_path(node_dir: Path) -> Path:
    """The fixed path of the diff artifact inside a node directory (``artifacts/diff.patch``)."""
    return node_dir / ARTIFACTS_DIR / DIFF_ARTIFACT_NAME


def publish(
    repo_root: Path,
    run_id: str,
    node_id: str,
    capture: CaptureResult,
    verification: VerifyResult,
    *,
    node_dir: Path,
    message: str | None = None,
    attempt: int | None = None,
) -> PublicationResult:
    """Publish a verified node's output (design section 8.3).

    ``capture`` is the captured candidate from task 1.15 and ``verification`` the result from
    task 1.16. ``node_dir`` is the node's ``NN-<node>/`` directory in the run store, where the
    diff artifact is written. Returns the output commit, its ref, and the artifact path.

    ``attempt`` is the 1-based attempt number (phase 1 is single-attempt per run, but retries and
    resume make it well-defined). When given, an immutable per-attempt ref
    (``refs/relay/<run>/<node>@<attempt>``) is written for the output, and a same-node retry
    advances the current ref to the new attempt's *sibling* output commit (both parented on
    ``capture.input_commit``); the prior attempt stays reachable via its per-attempt ref. A
    per-attempt ref is never rewritten: republishing an attempt that already recorded a different
    commit is refused, while republishing one that already recorded this commit is idempotent and
    never moves the current ref backward past a later attempt.

    Raises :class:`PublicationRefusedError` when ``verification`` did not allow publication,
    :class:`ReservedPathError` when the candidate tree contains a reserved runtime path, and
    :class:`OutputRefConflictError` when an existing output ref or attempt ref would be moved to
    an ancestor or unrelated commit. The user's HEAD and branches are never touched.
    """
    if not verification.publication_allowed:
        raise PublicationRefusedError(
            f"verification did not allow publication (outcome={verification.outcome}); "
            "no commit, ref or artifact was created"
        )

    if attempt is not None and attempt < 1:
        raise PublicationError(f"attempt must be >= 1, got {attempt}")

    ref = node_ref(run_id, node_id)
    _validate_candidate_tree(repo_root, capture.candidate_tree)

    attempt_ref_name = attempt_ref(run_id, node_id, attempt) if attempt is not None else None

    # Resolve both refs before any write: the decision and every CAS below compare against this
    # snapshot, so a concurrent writer (phase 2 supervisor) cannot be raced past a read.
    existing = read_ref(repo_root, ref)
    attempt_existing = (
        read_ref(repo_root, attempt_ref_name) if attempt_ref_name is not None else None
    )

    natural = _create_output_commit(repo_root, capture, node_id, message)

    # F1 decision (read-only): resolve the output commit and whether the current ref must move.
    # Every output commit is parented on `capture.input_commit`, so retries are siblings.
    output_commit = natural
    reused = False
    advance_current = False
    if attempt_existing is not None:
        if attempt_existing != natural:
            raise OutputRefConflictError(
                f"attempt ref {attempt_ref_name} already points at {attempt_existing[:12]}; "
                f"refusing to move it to {natural[:12]} (same attempt, different content)"
            )
        # Idempotent: this attempt already recorded exactly this commit. Never move the current
        # ref backward, but recover it forward when a prior partial write recorded the attempt
        # ref without advancing the current ref.
        output_commit = attempt_existing
        reused = True
        advance_current = _current_ref_behind(repo_root, existing, output_commit)
    elif existing is None:
        advance_current = True
    elif existing == natural:
        output_commit = existing
        reused = True
    elif _is_ancestor(repo_root, existing, natural):
        advance_current = True
    elif (
        attempt is not None
        and _is_prior_output(repo_root, existing, capture.input_commit)
        and not _is_ancestor(repo_root, natural, existing)
    ):
        # A legitimate sibling retry: `existing` and `natural` are both parented on the immutable
        # input commit, so `natural` is a sibling (never a descendant) and the new attempt
        # advances the current ref to it. The prior attempt stays reachable via its attempt ref.
        advance_current = True
    else:
        raise OutputRefConflictError(
            f"output ref {ref} already points at {existing[:12]}; refusing to move it to "
            f"{natural[:12]} (not a descendant)"
        )

    # Transaction: create the immutable attempt ref first (CAS on absence), then advance the
    # current ref (CAS on the observed old value). A failed current-ref CAS rolls back only an
    # attempt ref this call created, so a conflict never leaves partial state behind.
    created_attempt_ref: str | None = None
    if attempt_ref_name is not None and attempt_existing is None:
        if _compare_and_swap(repo_root, attempt_ref_name, output_commit, _ZERO_OID):
            created_attempt_ref = attempt_ref_name
        else:
            # Lost the creation race: re-read and reconcile. Same output is idempotent; a
            # different output means a divergent concurrent publish of the same attempt.
            observed = read_ref(repo_root, attempt_ref_name)
            if observed == output_commit:
                reused = True
                advance_current = _current_ref_behind(repo_root, existing, output_commit)
            else:
                raise _conflict(attempt_ref_name, _ZERO_OID, observed)

    if advance_current and not _compare_and_swap(
        repo_root, ref, output_commit, existing or _ZERO_OID
    ):
        observed = read_ref(repo_root, ref)
        if observed == output_commit:
            reused = True  # another writer already advanced it to the same output
        else:
            if created_attempt_ref is not None:
                _delete_ref_cas(repo_root, created_attempt_ref, output_commit)
            raise _conflict(ref, existing, observed)

    artifact = diff_artifact_path(node_dir)
    _write_diff_artifact(repo_root, capture.input_commit, output_commit, artifact)

    return PublicationResult(
        output_commit=output_commit,
        ref=ref,
        artifact_path=artifact,
        reused=reused,
        attempt_ref=attempt_ref_name,
    )


def publish_integration(
    repo_root: Path,
    run_id: str,
    *,
    base_commit: str,
    outputs: Sequence[tuple[str, str]],
    message: str | None = None,
) -> IntegrationResult:
    """Build (or update) the run's integration branch from the successful node outputs.

    ``outputs`` is a sequence of ``(node_id, output_commit)`` pairs for the *successful* nodes
    only; a failed or refused node must be absent. They are merged onto ``base_commit`` in
    deterministic ascending node-id order (design section 8.3). Each step fast-forwards when the
    incoming commit already contains the accumulated history and otherwise records a real merge
    commit with both contributing commits as parents; ancestry is never flattened into a
    single-parent commit. A single output simply fast-forwards the branch to that commit. The
    user's HEAD and branches are never touched: only ``refs/heads/relay/<run>/integration`` is
    updated.

    A merge conflict raises :class:`IntegrationConflictError` and leaves the integration branch
    untouched, so a failed run never leaves a half-merged branch. Automated conflict resolution
    (the ``resolve-conflict`` node) is phase 2a (task 2.6), out of scope here.

    The run loop (task 1.19) is responsible for running the graph's ``integration_acceptance``
    commands against the returned ``commit`` before marking the run done (design section 10 item
    4), and for writing ``run.finished {state, integration_commit}`` and the
    ``integration.published`` event; this function only returns the commit and branch.

    Re-running with the same inputs is idempotent: merge commits use pinned identity and
    timestamps, so identical inputs always produce the same integration commit sha.
    """
    branch = integration_branch(run_id)
    _require_commit(repo_root, base_commit, "base commit")

    ordered = _order_outputs(outputs)
    if not ordered:
        raise PublicationError("no successful node outputs to integrate (run is not done)")

    current = base_commit
    merged = False
    for node_id, output_commit in ordered:
        _require_commit(repo_root, output_commit, f"output commit for node {node_id}")
        current, step_merged = _merge_into(
            repo_root, current, output_commit, node_id, run_id, message
        )
        merged = merged or step_merged

    _write_integration_branch(repo_root, branch, current)

    return IntegrationResult(
        commit=current,
        branch=branch,
        base_commit=base_commit,
        nodes=tuple(ordered),
        merged=merged,
    )


# --- internals ---------------------------------------------------------------------------------


def _validate_component(value: str, what: str) -> None:
    """Validate a single ref-component id (a node id or run id) before any ref read or write.

    A valid component is one path-safe segment: letters, digits, ``.``, ``_`` and ``-``, with an
    alphanumeric first character and at most 128 characters. Rejected with a distinct message
    are: empty ids, a leading ``-`` (a git option) or ``.`` (a hidden name), a trailing ``.``
    (illegal in a Windows ref), whitespace or control characters, ``/`` or ``\\`` (path
    separators), ``..`` (a parent path or two-dot range), a ``.lock`` suffix (git's lock-file
    convention), ``@`` (the attempt-ref separator, including ``@{``), and any reserved name (see
    :data:`RESERVED_REF_COMPONENTS`). This runs before any ref is touched, so an unsafe id never
    reaches git.
    """
    if not value:
        raise PublicationError(f"{what} must not be empty")
    if value.startswith("-"):
        raise PublicationError(f"{what} {value!r} must not begin with '-'")
    if value.startswith("."):
        raise PublicationError(f"{what} {value!r} must not begin with '.'")
    if value.endswith("."):
        raise PublicationError(f"{what} {value!r} must not end with '.'")
    if value.endswith(".lock"):
        raise PublicationError(f"{what} {value!r} must not end with '.lock'")
    if ".." in value:
        raise PublicationError(f"{what} {value!r} must not contain '..'")
    if "@" in value:
        raise PublicationError(f"{what} {value!r} must not contain '@' (the attempt separator)")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise PublicationError(
            f"{what} {value!r} must not contain whitespace or control characters"
        )
    if "/" in value or "\\" in value:
        raise PublicationError(f"{what} {value!r} must not contain a path separator")
    if not _ID_RE.fullmatch(value):
        raise PublicationError(f"{what} {value!r} is not a safe ref component")
    if value.lower() in RESERVED_REF_COMPONENTS:
        raise PublicationError(f"{what} {value!r} is a reserved ref name")


def _run(
    repo_root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        raise PublicationError("git is not installed or not on PATH") from exc


def _git(repo_root: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    proc = _run(repo_root, *args, env=env)
    if proc.returncode != 0:
        raise GitCommandError(args, proc.stderr)
    return proc.stdout


def _validate_candidate_tree(repo_root: Path, candidate_tree: str) -> None:
    """Refuse a candidate tree that contains a reserved runtime path (section 6)."""
    names = _git(repo_root, "ls-tree", "-r", "-z", "--name-only", candidate_tree).split("\0")
    reserved = sorted({name for name in names if name and is_reserved_path(name)})
    if reserved:
        preview = ", ".join(reserved[:8])
        raise ReservedPathError(
            f"candidate tree contains reserved runtime paths ({preview}); refusing to publish"
        )


def _create_output_commit(
    repo_root: Path,
    capture: CaptureResult,
    node_id: str,
    message: str | None,
) -> str:
    """The orchestrator-owned output commit; a no-change node reuses its parent commit.

    The parent is always ``capture.input_commit`` (design section 8.3), so the output commit sha
    depends only on the input commit, candidate tree and message -- never on the current ref's
    history. A same-node retry is therefore a sibling of the prior output, not a descendant.
    """
    base = capture.input_commit
    parent_tree = _git(repo_root, "rev-parse", f"{base}^{{tree}}").strip()
    if capture.candidate_tree == parent_tree:
        return base
    text = message or f"relay: output commit for node {node_id}"
    return _git(
        repo_root,
        "commit-tree",
        capture.candidate_tree,
        "-p",
        base,
        "-m",
        text,
        env=_ORCHESTRATOR_ENV,
    ).strip()


def _is_prior_output(repo_root: Path, commit: str, input_commit: str) -> bool:
    """True when ``commit`` is a prior output for the same input snapshot.

    A prior output is either the input commit itself (a no-change node reused it) or a
    single-parent commit whose parent is ``input_commit``. This distinguishes a legitimate sibling
    retry from an unrelated or later commit the current ref must not move off.
    """
    if commit == input_commit:
        return True
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{commit}^")
    return proc.returncode == 0 and proc.stdout.strip() == input_commit


def _compare_and_swap(repo_root: Path, ref: str, new: str, old: str) -> bool:
    """Atomically point ``ref`` at ``new`` iff it currently equals ``old``; True if it did.

    ``old`` is the currently observed value; pass ``_ZERO_OID`` to require the ref to be absent
    (creation). A mismatched ``old`` -- including a concurrent creation or delete -- leaves the
    ref untouched and returns False, so the caller can re-read and reconcile idempotently or raise
    :class:`OutputRefConflictError`. ``new`` and ``old`` are always validated full object ids, so
    the only realistic non-zero exit from ``git update-ref`` is a compare-and-swap failure.
    """
    proc = _run(repo_root, "update-ref", ref, new, old)
    return proc.returncode == 0


def _delete_ref_cas(repo_root: Path, ref: str, expected: str) -> bool:
    """Delete ``ref`` iff it still points at ``expected``; True if it did.

    Used to roll back an attempt ref this call created, never a pre-existing one. A mismatch (the
    ref was concurrently rewritten or removed) returns False and leaves the ref alone; a real git
    failure raises :class:`GitCommandError`.
    """
    proc = _run(repo_root, "update-ref", "-d", ref, expected)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitCommandError(("update-ref", "-d", ref, expected), proc.stderr)


def _current_ref_behind(repo_root: Path, current: str | None, output: str) -> bool:
    """True when the current ref should be recovered forward to ``output``.

    Only a missing ref or an ancestor of ``output`` is behind; an equal or later/divergent commit
    is left alone (never moved backward).
    """
    if current is None:
        return True
    if current == output:
        return False
    return _is_ancestor(repo_root, current, output)


def _conflict(ref: str, expected: str | None, observed: str | None) -> OutputRefConflictError:
    """Build the conflict error for a compare-and-swap failure, without leaking a traceback."""
    exp = "absent" if expected in (None, _ZERO_OID) else expected[:12]
    obs = "absent" if observed is None else observed[:12]
    return OutputRefConflictError(
        f"ref {ref} expected {exp} but observed {obs} (concurrent write?)"
    )


def _order_outputs(outputs: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Validate node ids and sort the ``(node_id, output_commit)`` pairs deterministically.

    Duplicate node ids are refused: merging the same node twice would record it twice in the
    deterministic order and make the result depend on which duplicate wins, so it is an error
    rather than silently merging twice (even when the two entries are identical).
    """
    ordered = list(outputs)
    seen: set[str] = set()
    for node_id, _ in ordered:
        _validate_component(node_id, "node id")
        if node_id in seen:
            raise PublicationError(f"duplicate node id {node_id!r} in integration outputs")
        seen.add(node_id)
    ordered.sort(key=lambda pair: pair[0])
    return ordered


def _require_commit(repo_root: Path, sha: str, what: str) -> None:
    """Raise a clear error when `sha` is not a commit in the repository."""
    proc = _run(repo_root, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if proc.returncode != 0:
        raise PublicationError(f"{what} {sha!r} is not a commit in this repository")


def _write_integration_branch(repo_root: Path, branch: str, sha: str) -> None:
    """Point the integration branch at `sha` without moving it backward or racing a writer.

    The branch lives under ``refs/heads/``, outside gitbase's ``refs/relay/`` write guard, so it
    is updated with ``update-ref`` directly, after the same ref-name safety checks. Every update
    is a compare-and-swap against the value observed here, so a concurrent writer (the phase 2
    supervisor) cannot be raced past a read, and an existing branch is only ever moved forward:

    * absent -> create, with a compare-and-swap on absence;
    * equal -> no-op;
    * `sha` is a descendant of the existing commit -> advance, with a compare-and-swap on the
      observed value;
    * otherwise (a backward move, a sibling, or a rebuild that omits already-integrated output)
      -> raise :class:`IntegrationConflictError` and leave the branch untouched.

    A lost compare-and-swap re-reads the branch: an equal value is success, otherwise a clear
    conflict is raised. Because the merge is deterministic, a lost create/advance race that still
    points at `sha` is idempotent; a different observed value means a divergent concurrent
    publish, which is not retried.

    Limitation (phase 1): republishing a node whose output changed (a retry) or omitting a node
    produces an integration commit that is not a descendant of the existing branch, so this guard
    refuses it rather than losing already-integrated work. Rebuilding or replacing the
    integration branch for such republishes is owned by phase 2 and out of scope here.
    """
    if branch.startswith("-") or any(ch.isspace() or ord(ch) < 0x20 for ch in branch):
        raise PublicationError(f"branch {branch!r} is not a safe ref name")

    existing = read_ref(repo_root, branch)
    if existing is None:
        if _compare_and_swap(repo_root, branch, sha, _ZERO_OID):
            return
        observed = read_ref(repo_root, branch)
        if observed == sha:
            return
        raise IntegrationConflictError(
            f"integration branch {branch} was created concurrently "
            f"(expected absent, observed {observed[:12] if observed else 'absent'})"
        )
    if existing == sha:
        return
    if _is_ancestor(repo_root, existing, sha):
        if _compare_and_swap(repo_root, branch, sha, existing):
            return
        observed = read_ref(repo_root, branch)
        if observed == sha:
            return
        raise IntegrationConflictError(
            f"integration branch {branch} moved concurrently "
            f"(expected {existing[:12]}, observed {observed[:12] if observed else 'absent'})"
        )
    raise IntegrationConflictError(
        f"refusing to move integration branch {branch} from {existing[:12]} to {sha[:12]}: "
        "the new commit is not a descendant (backward move or missing prior work)"
    )


def _merge_into(
    repo_root: Path,
    current: str,
    incoming: str,
    node_id: str,
    run_id: str,
    message: str | None,
) -> tuple[str, bool]:
    """Merge one output commit into the accumulated integration commit.

    Returns ``(commit, merged)`` where ``merged`` is true when a real (non-fast-forward) merge
    commit was created. Ancestry is preserved: fast-forward when `incoming` already contains
    `current`, no-op when `current` already contains `incoming`, and otherwise a merge commit
    with both contributing commits as parents.
    """
    if _is_ancestor(repo_root, current, incoming):
        return incoming, False  # fast-forward
    if _is_ancestor(repo_root, incoming, current):
        return current, False  # already contained
    tree = _merge_tree(repo_root, current, incoming, node_id)
    text = message or f"relay: integrate {node_id} into {run_id}"
    commit = _git(
        repo_root,
        "commit-tree",
        tree,
        "-p",
        current,
        "-p",
        incoming,
        "-m",
        text,
        env=_ORCHESTRATOR_ENV,
    ).strip()
    return commit, True


def _merge_tree(repo_root: Path, current: str, incoming: str, node_id: str) -> str:
    """The merged tree of `current` and `incoming`, or a conflict error (nothing is written).

    Uses ``git merge-tree --write-tree`` so the merge runs on commit objects only, without a
    worktree or index, and never touches the user's checkout.
    """
    proc = _run(repo_root, "merge-tree", "--write-tree", "--name-only", current, incoming)
    if proc.returncode == 0:
        return proc.stdout.splitlines()[0].strip()
    if proc.returncode == 1:
        paths = _parse_conflict_paths(proc.stdout)
        raise IntegrationConflictError(
            f"integrating node {node_id} conflicts on {', '.join(paths) or 'unknown paths'}; "
            "the integration branch was not updated (run failed)"
        )
    raise GitCommandError(("merge-tree", "--write-tree", current, incoming), proc.stderr)


def _parse_conflict_paths(text: str) -> list[str]:
    """The conflicted paths from ``merge-tree --write-tree --name-only`` conflict output.

    The first line is the merged tree (with conflict markers); the conflicted paths follow until
    the human-readable ``Auto-merging``/``CONFLICT`` messages begin.
    """
    lines = text.splitlines()
    paths: list[str] = []
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith(("Auto-merging", "CONFLICT", "ERROR", "fatal:", "warning:")):
            break
        paths.append(line)
    return paths


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """True when `ancestor` is `descendant` itself or reachable from it."""
    proc = _run(repo_root, "merge-base", "--is-ancestor", ancestor, descendant)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise GitCommandError(("merge-base", "--is-ancestor", ancestor, descendant), proc.stderr)


def _write_diff_artifact(
    repo_root: Path,
    input_commit: str,
    output_commit: str,
    artifact: Path,
) -> None:
    """Write the deterministic unified diff (``artifacts/diff.patch``) into the node directory.

    The diff is pinned to a fixed algorithm and prefixes, ignores textconv/ext-diff and user
    rename config, and includes binary patches with full index lines, so the same two commits
    always produce byte-identical output.
    """
    text = _git(
        repo_root,
        "-c",
        "diff.algorithm=myers",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--binary",
        "--full-index",
        "--no-renames",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        input_commit,
        output_commit,
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(artifact, text)
