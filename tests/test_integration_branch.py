"""Integration branch and per-attempt ref tests (design section 8.3, task 1.17 part B).

Every test builds a real temp git repository. Node outputs are produced with the real
``capture_changes`` + ``publish`` chain, then ``publish_integration`` merges the successful
outputs onto the pinned base and points ``refs/heads/relay/<run>/integration`` at the result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import relay.core.publish as publish_mod
from harness import git, init_repo
from relay.core.capture import capture_changes
from relay.core.gitbase import read_ref
from relay.core.publish import (
    IntegrationConflictError,
    OutputRefConflictError,
    PublicationError,
    PublicationRefusedError,
    attempt_ref,
    integration_branch,
    node_ref,
    publish,
    publish_integration,
)
from relay.core.verify import VerifyResult

RUN = "2026-10-22-a1b2"
NODE = "n1"

# Test seam: the CAS helper and its absence sentinel are module-private; reach them once here so
# the race tests can monkeypatch the helper and match on the sentinel without flagging
# reportPrivateUsage at every use site.
_REAL_CAS = publish_mod._compare_and_swap  # pyright: ignore[reportPrivateUsage]
_ZERO_OID = publish_mod._ZERO_OID  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def passed() -> VerifyResult:
    return VerifyResult(outcome="passed", mode="default", checks=())


def failed() -> VerifyResult:
    return VerifyResult(outcome="failed", mode="default", checks=())


def publish_node(
    repo: Path,
    tmp_path: Path,
    node: str,
    filename: str,
    content: str,
    *,
    worktree: Path | None = None,
    attempt: int | None = None,
) -> tuple[str, str]:
    """Capture one added file in a node worktree and publish it; returns (output_commit, ref)."""
    head = git(repo, "rev-parse", "HEAD")
    wt = worktree or tmp_path / f"{node}-wt"
    if not wt.exists():
        git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / filename).write_text(content, encoding="utf-8", newline="\n")
    capture = capture_changes(wt, head, candidate_dir=tmp_path / f"{node}-cand")
    result = publish(
        repo,
        RUN,
        node,
        capture,
        passed(),
        node_dir=tmp_path / f"{node}-dir",
        attempt=attempt,
    )
    return result.output_commit, result.ref


def tree_files(repo: Path, sha: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", sha).splitlines()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return git(repo, "merge-base", descendant, ancestor) == ancestor


# --- single node ---------------------------------------------------------------------------


def test_single_node_integration_points_at_output(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    output, _ = publish_node(repo, tmp_path, NODE, "feature.txt", "f\n")

    result = publish_integration(repo, RUN, base_commit=head, outputs=[(NODE, output)])

    assert result.commit == output
    assert result.branch == integration_branch(RUN)
    assert read_ref(repo, result.branch) == output
    assert result.merged is False  # pure fast-forward


def test_no_change_single_node_integration_points_at_base(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    capture = capture_changes(wt, head, candidate_dir=tmp_path / "cand")
    output = publish(repo, RUN, NODE, capture, passed(), node_dir=tmp_path / "d").output_commit
    assert output == head  # no-change node reuses the input commit

    result = publish_integration(repo, RUN, base_commit=head, outputs=[(NODE, output)])
    assert result.commit == head
    assert read_ref(repo, result.branch) == head


# --- multi-node deterministic merge --------------------------------------------------------


def test_multiple_nodes_deterministic_merge(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n2", "a.txt", "a\n")
    b_out, _ = publish_node(repo, tmp_path, "n1", "b.txt", "b\n")

    result = publish_integration(
        repo, RUN, base_commit=head, outputs=[("n2", a_out), ("n1", b_out)]
    )

    # Deterministic ascending node-id order regardless of input order.
    assert result.nodes == (("n1", b_out), ("n2", a_out))
    files = tree_files(repo, result.commit)
    assert "a.txt" in files and "b.txt" in files
    assert read_ref(repo, result.branch) == result.commit


def test_merge_commit_preserves_ancestry(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    b_out, _ = publish_node(repo, tmp_path, "n2", "b.txt", "b\n")

    result = publish_integration(
        repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)]
    )

    parents = git(repo, "show", "-s", "--format=%P", result.commit).split()
    assert result.merged is True
    assert len(parents) == 2  # real merge commit, never flattened to a single parent
    assert set(parents) == {a_out, b_out}


def test_integration_order_is_deterministic(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    b_out, _ = publish_node(repo, tmp_path, "n2", "b.txt", "b\n")

    first = publish_integration(repo, RUN, base_commit=head, outputs=[("n2", b_out), ("n1", a_out)])
    second = publish_integration(
        repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)]
    )

    assert first.commit == second.commit
    assert first.nodes == second.nodes == (("n1", a_out), ("n2", b_out))


# --- failed node absent --------------------------------------------------------------------


def test_failed_node_absent_from_integration(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")

    # n2 fails verification: nothing is published, so it contributes no output commit.
    wt = tmp_path / "n2-wt"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
    capture = capture_changes(wt, head, candidate_dir=tmp_path / "n2-cand")
    with pytest.raises(PublicationRefusedError):
        publish(repo, RUN, "n2", capture, failed(), node_dir=tmp_path / "n2-dir")
    assert read_ref(repo, node_ref(RUN, "n2")) is None

    result = publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])
    files = tree_files(repo, result.commit)
    assert "a.txt" in files
    assert "b.txt" not in files


# --- conflict policy -----------------------------------------------------------------------


def test_merge_conflict_is_a_run_failure(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "shared.txt", "one\n")
    b_out, _ = publish_node(repo, tmp_path, "n2", "shared.txt", "two\n")

    with pytest.raises(IntegrationConflictError, match="conflicts"):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)])

    # No half-merged branch is left behind.
    assert read_ref(repo, integration_branch(RUN)) is None


def test_conflict_leaves_existing_branch_untouched(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])
    before = read_ref(repo, integration_branch(RUN))

    # A conflicting second update must not move the branch.
    b_out, _ = publish_node(repo, tmp_path, "n2", "a.txt", "changed\n")
    with pytest.raises(IntegrationConflictError):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)])

    assert read_ref(repo, integration_branch(RUN)) == before


# --- user HEAD/branch untouched ------------------------------------------------------------


def test_user_head_and_branch_untouched(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    main = git(repo, "rev-parse", "refs/heads/main")
    output, _ = publish_node(repo, tmp_path, NODE, "x.txt", "x\n")

    publish_integration(repo, RUN, base_commit=head, outputs=[(NODE, output)])

    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "rev-parse", "refs/heads/main") == main
    assert git(repo, "symbolic-ref", "--quiet", "HEAD") == "refs/heads/main"
    assert read_ref(repo, integration_branch(RUN)) is not None


# --- idempotency ---------------------------------------------------------------------------


def test_integration_is_idempotent(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    b_out, _ = publish_node(repo, tmp_path, "n2", "b.txt", "b\n")

    first = publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)])
    second = publish_integration(
        repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)]
    )

    assert second.commit == first.commit
    assert read_ref(repo, integration_branch(RUN)) == first.commit


# --- integration branch guard: never move backward or race (F4) ---------------------------


def test_integration_branch_forward_advance_allowed(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    first = publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])

    # Adding a second node's output advances the branch to a descendant of the first commit.
    b_out, _ = publish_node(repo, tmp_path, "n2", "b.txt", "b\n")
    second = publish_integration(
        repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)]
    )

    assert second.commit != first.commit
    assert read_ref(repo, integration_branch(RUN)) == second.commit
    assert is_ancestor(repo, first.commit, second.commit)


def test_integration_branch_equal_is_noop(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    first = publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])
    before = read_ref(repo, integration_branch(RUN))

    second = publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])

    assert second.commit == first.commit
    assert read_ref(repo, integration_branch(RUN)) == before


def test_integration_branch_backward_move_refused(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    output, _ = publish_node(repo, tmp_path, NODE, "a.txt", "a\n")
    first = publish_integration(repo, RUN, base_commit=head, outputs=[(NODE, output)])

    # A later commit (a descendant) now occupies the branch; re-publishing the single-node output
    # would move the branch back to an ancestor, losing that later work.
    tree = git(repo, "rev-parse", f"{first.commit}^{{tree}}")
    later = git(repo, "commit-tree", tree, "-p", first.commit, "-m", "later")
    git(repo, "update-ref", integration_branch(RUN), later)

    with pytest.raises(IntegrationConflictError, match="not a descendant"):
        publish_integration(repo, RUN, base_commit=head, outputs=[(NODE, output)])

    assert read_ref(repo, integration_branch(RUN)) == later


def test_integration_missing_output_rebuild_refused(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    b_out, _ = publish_node(repo, tmp_path, "n2", "b.txt", "b\n")
    first = publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n2", b_out)])

    # Re-publishing with only n1 omits n2's already-integrated output, so the new commit is not a
    # descendant of the existing branch; the rebuild must be refused, not silently done.
    with pytest.raises(IntegrationConflictError, match="not a descendant"):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])

    assert read_ref(repo, integration_branch(RUN)) == first.commit


def test_integration_branch_cas_race_leaves_no_partial_branch(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    branch = integration_branch(RUN)
    real_cas = _REAL_CAS

    # A concurrent writer creates the branch first (a divergent value) and our creation CAS loses.
    other = git(
        repo, "commit-tree", git(repo, "rev-parse", f"{a_out}^{{tree}}"), "-p", a_out, "-m", "other"
    )

    def racer(repo_root: Path, ref: str, new: str, old: str) -> bool:
        if ref == branch and old == _ZERO_OID:
            # The integration branch lives under refs/heads/, outside gitbase's write guard, so
            # the competing writer uses update-ref directly.
            git(repo_root, "update-ref", ref, other)
            return False
        return real_cas(repo_root, ref, new, old)

    monkeypatch.setattr(publish_mod, "_compare_and_swap", racer)

    with pytest.raises(IntegrationConflictError, match="concurrently"):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out)])

    # The branch is left at the foreign writer's commit, never our output or a half-written value.
    assert read_ref(repo, branch) == other


# --- duplicate node ids (F5) ---------------------------------------------------------------


def test_duplicate_node_ids_rejected(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")

    # Identical duplicate entries are still rejected, never silently merged twice.
    with pytest.raises(PublicationError, match="duplicate node id"):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n1", a_out)])

    assert read_ref(repo, integration_branch(RUN)) is None


def test_duplicate_node_ids_different_content_rejected(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    a_out, _ = publish_node(repo, tmp_path, "n1", "a.txt", "a\n")
    b_out, _ = publish_node(repo, tmp_path, "n2", "b.txt", "b\n")

    # The same node id listed twice, even with different commits, is a duplicate id.
    with pytest.raises(PublicationError, match="duplicate node id"):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", a_out), ("n1", b_out)])

    assert read_ref(repo, integration_branch(RUN)) is None


# --- retry: per-attempt refs and ref advance -----------------------------------------------


def test_retry_advances_ref_and_preserves_prior_attempt(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / "f.txt").write_text("v1\n", encoding="utf-8", newline="\n")
    capture1 = capture_changes(wt, head, candidate_dir=tmp_path / "cand1")
    first = publish(repo, RUN, NODE, capture1, passed(), node_dir=tmp_path / "d", attempt=1)

    assert first.attempt_ref == attempt_ref(RUN, NODE, 1)
    assert read_ref(repo, attempt_ref(RUN, NODE, 1)) == first.output_commit
    assert read_ref(repo, node_ref(RUN, NODE)) == first.output_commit

    # Continue editing in the same worktree (the retry builds on the prior attempt's content).
    (wt / "f.txt").write_text("v2\n", encoding="utf-8", newline="\n")
    capture2 = capture_changes(wt, head, candidate_dir=tmp_path / "cand2")
    second = publish(repo, RUN, NODE, capture2, passed(), node_dir=tmp_path / "d", attempt=2)

    assert second.output_commit != first.output_commit
    assert second.attempt_ref == attempt_ref(RUN, NODE, 2)
    # Every output commit is parented on the node's input commit, so the retry is a *sibling*,
    # not a descendant, of the prior attempt.
    assert git(repo, "rev-parse", f"{second.output_commit}^") == head
    assert not is_ancestor(repo, first.output_commit, second.output_commit)
    # The current ref moved forward; both per-attempt refs stay reachable.
    assert read_ref(repo, node_ref(RUN, NODE)) == second.output_commit
    assert read_ref(repo, attempt_ref(RUN, NODE, 1)) == first.output_commit
    assert read_ref(repo, attempt_ref(RUN, NODE, 2)) == second.output_commit

    # Republishing the same retry is idempotent: same sha, no ref movement.
    again = publish(repo, RUN, NODE, capture2, passed(), node_dir=tmp_path / "d", attempt=2)
    assert again.output_commit == second.output_commit
    assert again.reused is True
    assert read_ref(repo, node_ref(RUN, NODE)) == second.output_commit

    # Republishing attempt 1 after attempt 2 returns the prior commit and leaves the current ref
    # at the later attempt (never moved backward).
    first_again = publish(repo, RUN, NODE, capture1, passed(), node_dir=tmp_path / "d", attempt=1)
    assert first_again.output_commit == first.output_commit
    assert first_again.reused is True
    assert read_ref(repo, node_ref(RUN, NODE)) == second.output_commit


def test_same_attempt_republish_with_different_content_is_refused(
    repo: Path, tmp_path: Path
) -> None:
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / "f.txt").write_text("v1\n", encoding="utf-8", newline="\n")
    capture1 = capture_changes(wt, head, candidate_dir=tmp_path / "cand1")
    first = publish(repo, RUN, NODE, capture1, passed(), node_dir=tmp_path / "d", attempt=1)

    # Reusing the same attempt number with different content is a divergent republish, refused.
    (wt / "f.txt").write_text("v2\n", encoding="utf-8", newline="\n")
    capture2 = capture_changes(wt, head, candidate_dir=tmp_path / "cand2")
    with pytest.raises(OutputRefConflictError):
        publish(repo, RUN, NODE, capture2, passed(), node_dir=tmp_path / "d", attempt=1)

    # The refused republish left both the current ref and the attempt ref untouched.
    assert read_ref(repo, node_ref(RUN, NODE)) == first.output_commit
    assert read_ref(repo, attempt_ref(RUN, NODE, 1)) == first.output_commit


def test_output_sha_is_independent_of_ref_history(repo: Path, tmp_path: Path) -> None:
    """The output sha depends only on input, tree and message -- never the current ref history."""
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    (wt / "f.txt").write_text("v1\n", encoding="utf-8", newline="\n")
    capture1 = capture_changes(wt, head, candidate_dir=tmp_path / "cand1")
    publish(repo, RUN, NODE, capture1, passed(), node_dir=tmp_path / "d1", attempt=1)

    (wt / "f.txt").write_text("v2\n", encoding="utf-8", newline="\n")
    capture2 = capture_changes(wt, head, candidate_dir=tmp_path / "cand2")
    msg = "relay: output commit for node n1"
    retry = publish(
        repo, RUN, NODE, capture2, passed(), node_dir=tmp_path / "d2", attempt=2, message=msg
    )

    # The same delta published as attempt 1 into a fresh node with no prior ref yields the same
    # sha: the parent is always the input commit, never the prior output ref.
    fresh = publish(
        repo, RUN, "n2", capture2, passed(), node_dir=tmp_path / "d3", attempt=1, message=msg
    )

    assert retry.output_commit == fresh.output_commit


# --- missing / failed node handling -------------------------------------------------------


def test_missing_output_commit_is_an_error(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(PublicationError, match="not a commit"):
        publish_integration(repo, RUN, base_commit=head, outputs=[("n1", "0" * 40)])


def test_bad_base_commit_is_an_error(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(PublicationError, match="not a commit"):
        publish_integration(repo, RUN, base_commit="0" * 40, outputs=[("n1", head)])


def test_empty_outputs_is_an_error(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(PublicationError, match="no successful node outputs"):
        publish_integration(repo, RUN, base_commit=head, outputs=[])


# --- ref name shapes -----------------------------------------------------------------------


def test_integration_branch_shape() -> None:
    assert integration_branch(RUN) == f"refs/heads/relay/{RUN}/integration"


def test_attempt_ref_shape() -> None:
    assert attempt_ref(RUN, NODE, 1) == f"refs/relay/{RUN}/{NODE}@1"


def test_attempt_ref_rejects_bad_attempt() -> None:
    with pytest.raises(PublicationError):
        attempt_ref(RUN, NODE, 0)
    with pytest.raises(PublicationError):
        attempt_ref(RUN, NODE, -1)
