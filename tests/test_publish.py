"""Output publication tests (design section 8.3, task 1.17 part A).

Every test builds a real temp git repository, captures a delta and publishes it. The Verifier is
not re-tested here; where a publishable result is needed a minimal ``VerifyResult`` is built, and
one integration test runs the real ``capture_changes`` + ``verify`` + ``publish`` chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import relay.core.publish as publish_mod
from harness import command_line, git, init_repo
from relay.core.capture import CaptureResult, capture_changes
from relay.core.gitbase import read_ref, write_ref
from relay.core.publish import (
    DIFF_ARTIFACT_NAME,
    OutputRefConflictError,
    PublicationError,
    PublicationRefusedError,
    ReservedPathError,
    attempt_ref,
    diff_artifact_path,
    integration_branch,
    node_ref,
    publish,
)
from relay.core.specs.task import RunCheck, TaskSpec
from relay.core.verify import VerifyResult, verify

RUN = "2026-10-22-a1b2"
NODE = "n1"
ORCHESTRATOR_NAME = "relay"
ORCHESTRATOR_EMAIL = "relay@example.invalid"

# Test seam: the CAS helper and its absence sentinel are module-private; reach them once here so
# the race tests can monkeypatch the helper and match on the sentinel without flagging
# reportPrivateUsage at every use site.
_REAL_CAS = publish_mod._compare_and_swap  # pyright: ignore[reportPrivateUsage]
_ZERO_OID = publish_mod._ZERO_OID  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def passed_verification() -> VerifyResult:
    return VerifyResult(outcome="passed", mode="default", checks=())


def failed_verification() -> VerifyResult:
    return VerifyResult(outcome="failed", mode="default", checks=())


def make_capture(repo: Path, tmp_path: Path, *, filename: str = "new.txt") -> CaptureResult:
    """A worktree with one added file, captured into a candidate checkout."""
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "node"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / filename).write_text("added\n", encoding="utf-8", newline="\n")
    return capture_changes(wt, head, candidate_dir=tmp_path / "candidate")


def tree_files(repo: Path, tree: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", tree).splitlines()


def commit_field(repo: Path, sha: str, fmt: str) -> str:
    return git(repo, "show", "-s", f"--format={fmt}", sha)


# --- allowed vs failed verification -------------------------------------------------------


def test_passed_verification_publishes(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    node_dir = tmp_path / "node-dir"
    result = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)

    assert result.output_commit != capture.input_commit
    assert result.ref == node_ref(RUN, NODE) == f"refs/relay/{RUN}/{NODE}"
    assert read_ref(repo, result.ref) == result.output_commit
    assert result.artifact_path == diff_artifact_path(node_dir)
    assert result.artifact_path.is_file()
    assert result.reused is False


def test_failed_verification_creates_nothing(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    node_dir = tmp_path / "node-dir"
    head_before = git(repo, "rev-parse", "HEAD")

    with pytest.raises(PublicationRefusedError, match="did not allow publication"):
        publish(repo, RUN, NODE, capture, failed_verification(), node_dir=node_dir)

    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert not node_dir.exists()  # no artifact (or any) directory was created
    assert git(repo, "rev-parse", "HEAD") == head_before


# --- commit shape and ownership -----------------------------------------------------------


def test_output_commit_parent_tree_author(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    result = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d")

    commit = result.output_commit
    assert commit_field(repo, commit, "%P") == capture.input_commit
    assert git(repo, "rev-parse", f"{commit}^{{tree}}") == capture.candidate_tree
    # Orchestrator identity, never the agent's (harness commits use "test").
    assert commit_field(repo, commit, "%an") == ORCHESTRATOR_NAME
    assert commit_field(repo, commit, "%ae") == ORCHESTRATOR_EMAIL
    assert commit_field(repo, commit, "%cn") == ORCHESTRATOR_NAME
    assert commit_field(repo, commit, "%ce") == ORCHESTRATOR_EMAIL
    assert commit_field(repo, commit, "%s") == f"relay: output commit for node {NODE}"


def test_user_head_and_branch_untouched(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    head_before = git(repo, "rev-parse", "HEAD")
    main_before = git(repo, "rev-parse", "refs/heads/main")

    publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d")

    assert git(repo, "rev-parse", "HEAD") == head_before
    assert git(repo, "rev-parse", "refs/heads/main") == main_before
    # No new local branch was created either.
    assert git(repo, "branch", "--list", f"relay/{RUN}/{NODE}") == ""


# --- reserved-path validation -------------------------------------------------------------


def test_reserved_path_in_candidate_tree_is_refused(repo: Path, tmp_path: Path) -> None:
    # Track a reserved runtime path in the input commit; capture keeps it (it is part of the
    # input tree), so publication must refuse rather than silently strip it.
    state = repo / ".agents" / "runs" / "x"
    state.mkdir(parents=True)
    (state / "state.txt").write_text("s\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "tracked reserved path")
    head = git(repo, "rev-parse", "HEAD")

    wt = tmp_path / "node"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    capture = capture_changes(wt, head, candidate_dir=tmp_path / "candidate")
    assert ".agents/runs/x/state.txt" in tree_files(repo, capture.candidate_tree)

    with pytest.raises(ReservedPathError, match="reserved runtime path"):
        publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d")
    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert not (tmp_path / "d" / "artifacts").exists()


# --- diff artifact ------------------------------------------------------------------------


def test_artifact_content_is_unified_diff(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    result = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d")

    text = result.artifact_path.read_text(encoding="utf-8")
    assert text.startswith("diff --git a/new.txt b/new.txt\n")
    assert "+added" in text
    assert result.artifact_path.name == DIFF_ARTIFACT_NAME


def test_artifact_is_deterministic(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    node_dir = tmp_path / "d"
    first = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)
    first_bytes = first.artifact_path.read_bytes()
    first.artifact_path.unlink()

    second = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)
    assert second.output_commit == first.output_commit
    assert second.artifact_path.read_bytes() == first_bytes


# --- idempotency and ref advance ---------------------------------------------------------


def test_republish_is_idempotent(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    node_dir = tmp_path / "d"
    first = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)
    first_bytes = first.artifact_path.read_bytes()

    second = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)
    assert second.output_commit == first.output_commit
    assert second.reused is True
    assert read_ref(repo, node_ref(RUN, NODE)) == first.output_commit
    # The artifact is not duplicated: exactly one file, byte-identical content.
    assert [p.name for p in (node_dir / "artifacts").iterdir()] == [DIFF_ARTIFACT_NAME]
    assert second.artifact_path.read_bytes() == first_bytes


def test_existing_ref_is_not_moved_backward(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    node_dir = tmp_path / "d"
    first = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)

    # Simulate a later publish: a descendant of the output commit now occupies the ref.
    tree = git(repo, "rev-parse", f"{first.output_commit}^{{tree}}")
    later = git(repo, "commit-tree", tree, "-p", first.output_commit, "-m", "later")
    write_ref(repo, node_ref(RUN, NODE), later)

    with pytest.raises(OutputRefConflictError, match="not a descendant"):
        publish(repo, RUN, NODE, capture, passed_verification(), node_dir=node_dir)
    assert read_ref(repo, node_ref(RUN, NODE)) == later


def test_no_change_node_reuses_input_commit(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "node"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    capture = capture_changes(wt, head, candidate_dir=tmp_path / "candidate")
    assert capture.empty

    result = publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d")
    assert result.output_commit == head
    assert read_ref(repo, node_ref(RUN, NODE)) == head


# --- real chain: capture + verify + publish -----------------------------------------------


def test_capture_verify_publish_chain(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "node"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / "feature.txt").write_text("f\n", encoding="utf-8", newline="\n")
    capture = capture_changes(wt, head, candidate_dir=tmp_path / "candidate")

    ok = tmp_path / "ok.py"
    ok.write_text("print('ok')\n", encoding="utf-8")
    task = TaskSpec(
        id=NODE,
        run_id=RUN,
        category="implement",
        goal="add feature",
        acceptance=[RunCheck(run=command_line([sys.executable, str(ok)]))],
    )
    result = verify(capture, task)

    publication = publish(repo, RUN, NODE, capture, result, node_dir=tmp_path / "d")
    assert result.publication_allowed
    assert publication.output_commit != head
    assert read_ref(repo, publication.ref) == publication.output_commit
    assert publication.artifact_path.is_file()


# --- ref name validation ------------------------------------------------------------------


def test_node_ref_shape() -> None:
    assert node_ref(RUN, NODE) == f"refs/relay/{RUN}/{NODE}"


@pytest.mark.parametrize("bad", ["../x", "a b", "a/b", ".x", "x.lock", "x..y"])
def test_unsafe_ref_components_rejected(bad: str) -> None:
    with pytest.raises(PublicationError):
        node_ref(RUN, bad)
    with pytest.raises(PublicationError):
        node_ref(bad, NODE)


# --- ref-component edge cases (F6) --------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "-n1",  # leading dash (a git option)
        ".n1",  # leading dot
        "n1.",  # trailing dot (illegal in a Windows ref)
        "a\u0001b",  # control character
        "a b",  # whitespace (space)
        "a\tb",  # whitespace (tab)
        "a..b",  # parent traversal / two-dot range
        "x.lock",  # git lock suffix
        "a@{b}",  # @{ reflog/peel syntax
        "n1@2",  # @ (attempt separator)
        "a/b",  # forward path separator
        "a\\b",  # backslash path separator
    ],
)
def test_ref_component_edge_cases_rejected(bad: str) -> None:
    with pytest.raises(PublicationError):
        node_ref(RUN, bad)
    with pytest.raises(PublicationError):
        node_ref(bad, NODE)


def test_ref_component_valid_id_control() -> None:
    assert node_ref("2026-10-22-a1b2", "n1") == "refs/relay/2026-10-22-a1b2/n1"
    assert node_ref("2026-10-22-a1b2", "n1.implement-v2_x") == (
        "refs/relay/2026-10-22-a1b2/n1.implement-v2_x"
    )


# --- reserved ref components (F3) ---------------------------------------------------------


@pytest.mark.parametrize("reserved", ["base", "BASE", "integration", "Integration"])
def test_reserved_node_id_rejected(reserved: str) -> None:
    with pytest.raises(PublicationError, match="reserved"):
        node_ref(RUN, reserved)


@pytest.mark.parametrize("reserved", ["base", "integration"])
def test_reserved_run_id_rejected(reserved: str) -> None:
    with pytest.raises(PublicationError, match="reserved"):
        node_ref(reserved, NODE)
    with pytest.raises(PublicationError, match="reserved"):
        integration_branch(reserved)


def test_reserved_node_id_publishes_nothing(repo: Path, tmp_path: Path) -> None:
    capture = make_capture(repo, tmp_path)
    with pytest.raises(PublicationError, match="reserved"):
        publish(repo, RUN, "base", capture, passed_verification(), node_dir=tmp_path / "d")
    # Rejected before any ref write or artifact: no ref exists and no directory was created.
    assert read_ref(repo, f"refs/relay/{RUN}/base") is None
    assert not (tmp_path / "d").exists()


def test_diff_artifact_path_shape(tmp_path: Path) -> None:
    assert diff_artifact_path(tmp_path / "01-n1") == tmp_path / "01-n1" / "artifacts" / "diff.patch"


# --- compare-and-swap races ----------------------------------------------------------------
#
# The supervisor (phase 2) can publish concurrently, so every publication ref update is a
# compare-and-swap. These tests inject a competing writer between the decision and the CAS by
# monkeypatching ``publish._compare_and_swap``: on the targeted ref the wrapper writes the
# competing value first and then returns False (our CAS loses the race), exactly as a second
# writer would.


def _sibling_commit(repo: Path, parent: str) -> str:
    """A valid commit distinct from any output, parented on `parent` (a concurrent writer's)."""
    tree = git(repo, "rev-parse", f"{parent}^{{tree}}")
    return git(repo, "commit-tree", tree, "-p", parent, "-m", "concurrent writer")


def test_attempt_ref_creation_race_same_output_is_idempotent(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = make_capture(repo, tmp_path)
    target = attempt_ref(RUN, NODE, 1)
    real_cas = _REAL_CAS

    def racer(repo_root: Path, ref: str, new: str, old: str) -> bool:
        if ref == target and old == _ZERO_OID:
            # Another writer creates the same attempt ref (same output) first.
            write_ref(repo_root, ref, new)
            return False  # our creation CAS loses the race
        return real_cas(repo_root, ref, new, old)

    monkeypatch.setattr(publish_mod, "_compare_and_swap", racer)

    result = publish(
        repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d", attempt=1
    )

    # The foreign ref equals our output, so the publish is idempotent: no conflict, and the
    # current ref still advances to the shared output.
    assert result.reused is True
    assert result.output_commit != capture.input_commit
    assert read_ref(repo, target) == result.output_commit
    assert read_ref(repo, node_ref(RUN, NODE)) == result.output_commit


def test_attempt_ref_creation_race_different_output_conflicts(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = make_capture(repo, tmp_path)
    target = attempt_ref(RUN, NODE, 1)
    other = git(repo, "rev-parse", "HEAD")  # a commit distinct from our output
    real_cas = _REAL_CAS

    def racer(repo_root: Path, ref: str, new: str, old: str) -> bool:
        if ref == target and old == _ZERO_OID:
            write_ref(repo_root, ref, other)
            return False
        return real_cas(repo_root, ref, new, old)

    monkeypatch.setattr(publish_mod, "_compare_and_swap", racer)

    with pytest.raises(OutputRefConflictError, match="concurrent write"):
        publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d", attempt=1)

    # A conflict leaves the current ref untouched and the foreign attempt ref in place.
    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert read_ref(repo, target) == other


def test_current_ref_cas_race_rolls_back_new_attempt_ref(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = make_capture(repo, tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    other = _sibling_commit(repo, head)
    real_cas = _REAL_CAS

    def racer(repo_root: Path, ref: str, new: str, old: str) -> bool:
        if ref == node_ref(RUN, NODE):
            # Another writer moves the current ref right after our decision.
            write_ref(repo_root, ref, other)
            return False
        return real_cas(repo_root, ref, new, old)

    monkeypatch.setattr(publish_mod, "_compare_and_swap", racer)

    with pytest.raises(OutputRefConflictError, match="concurrent write"):
        publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d", attempt=1)

    # No partial state: the attempt ref this call created was rolled back, and the current ref
    # is the other writer's commit, never our output.
    assert read_ref(repo, attempt_ref(RUN, NODE, 1)) is None
    assert read_ref(repo, node_ref(RUN, NODE)) == other


def test_current_ref_cas_race_preserves_preexisting_attempt_ref(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = make_capture(repo, tmp_path)
    first = publish(
        repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d", attempt=1
    )
    target = attempt_ref(RUN, NODE, 1)

    # A prior partial write: the attempt ref exists but the current ref was lost.
    git(repo, "update-ref", "-d", node_ref(RUN, NODE))

    head = git(repo, "rev-parse", "HEAD")
    other = _sibling_commit(repo, head)
    real_cas = _REAL_CAS

    def racer(repo_root: Path, ref: str, new: str, old: str) -> bool:
        if ref == node_ref(RUN, NODE):
            write_ref(repo_root, ref, other)
            return False
        return real_cas(repo_root, ref, new, old)

    monkeypatch.setattr(publish_mod, "_compare_and_swap", racer)

    with pytest.raises(OutputRefConflictError, match="concurrent write"):
        publish(repo, RUN, NODE, capture, passed_verification(), node_dir=tmp_path / "d", attempt=1)

    # The pre-existing attempt ref is preserved; the current ref is the other writer's commit.
    assert read_ref(repo, target) == first.output_commit
    assert read_ref(repo, node_ref(RUN, NODE)) == other
