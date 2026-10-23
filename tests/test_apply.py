"""Git-mode delivery tests for ``relay apply`` (design sections 8.3 and 14, task 1.24).

Every test builds a real temp git repository and a run store whose integration commit is a real
commit. Preparation is exercised against fast-forward, no-op, real-merge and conflict cases; the
installation half only performs the expected-ref compare-and-swap. No CLI, confirmation prompt or
verification runs here (those are task 1.24b).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from harness import git, init_repo
from relay.core.apply import (
    DirtyOverlapError,
    IntegrationMissingError,
    MergeConflictError,
    RunMissingError,
    RunNotDoneError,
    TargetMovedError,
    TargetRefError,
    commit_ref,
    prepare_apply,
    resolve_integration_commit,
)
from relay.core.events import EventLog
from relay.core.gitbase import base_ref, read_ref, resolve_base
from relay.core.publish import integration_branch
from relay.core.store import RunStore

RUN = "2026-10-22-a1b2"
ORCHESTRATOR_NAME = "relay"
ORCHESTRATOR_EMAIL = "relay@example.invalid"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def commit_in_worktree(
    repo: Path,
    tmp_path: Path,
    parent: str,
    files: dict[str, str | None],
    *,
    message: str = "change",
) -> str:
    """Create a commit on top of ``parent`` in a throwaway worktree; returns its sha."""
    worktree = Path(tempfile.mkdtemp(prefix="wt-", dir=tmp_path))
    git(repo, "worktree", "add", "--detach", str(worktree), parent)
    try:
        for name, content in files.items():
            target = worktree / name
            if content is None:
                target.unlink()
            else:
                write(target, content)
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", message)
        return git(worktree, "rev-parse", "HEAD")
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))


def dummy_commit(repo: Path, parent: str, message: str) -> str:
    tree = git(repo, "rev-parse", f"{parent}^{{tree}}")
    return git(repo, "commit-tree", tree, "-p", parent, "-m", message)


def create_run(
    repo: Path,
    *,
    base: str,
    integration: str | None = None,
    state: str = "done",
    final: bool = True,
    started: bool = True,
    finished: bool = True,
    finished_commit: str | None = None,
    branch: bool = True,
) -> RunStore:
    """Write a run store for tests: ``run.json``, optional events and the integration branch."""
    store = RunStore.create(repo, run_id=RUN)
    record = store.read_run()
    record.state = state  # type: ignore[assignment]
    if final and state == "done":
        record.final_commit = integration
    store.write_run(record)

    if started or finished:
        with EventLog(store.events_path) as log:
            if started:
                log.append(
                    "run.started",
                    {"config_hash": "test", "base_commit": base, "allow_dirty": False},
                )
            if finished:
                log.append(
                    "run.finished",
                    {
                        "state": "done" if state == "done" else state,
                        "integration_commit": finished_commit or integration,
                    },
                )
    if branch and integration is not None:
        git(repo, "update-ref", integration_branch(RUN), integration)
    return store


def worktree_count(repo: Path) -> int:
    listed = git(repo, "worktree", "list", "--porcelain")
    return len([line for line in listed.splitlines() if line.startswith("worktree ")])


def snapshot(repo: Path) -> tuple[str, str, str]:
    return (
        git(repo, "rev-parse", "HEAD"),
        git(repo, "rev-parse", "refs/heads/main"),
        git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
    )


# --- fast-forward -----------------------------------------------------------------------------


def test_fast_forward_prepares_and_installs(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "feature\n"})
    create_run(repo, base=base, integration=integration)
    before = snapshot(repo)

    plan = prepare_apply(repo, RUN)

    assert plan.integration_commit == integration
    assert plan.prepared_commit == integration
    assert plan.target_ref == "refs/heads/main"
    assert plan.target_tip == base
    assert plan.expected_old == base
    assert plan.base_commit == base
    assert plan.prepared_tree == git(repo, "rev-parse", f"{integration}^{{tree}}")
    assert plan.changed_paths == ("feature.txt",)
    assert plan.overlaps == () and plan.conflicts == ()
    assert plan.clean is True
    assert plan.noop is False
    # Preparation changed nothing in the user's checkout.
    assert snapshot(repo) == before
    assert worktree_count(repo) == 1

    result = commit_ref(repo, plan)
    assert result.changed is True
    assert result.prepared_commit == integration
    assert result.target_ref == "refs/heads/main"
    assert read_ref(repo, "refs/heads/main") == integration


# --- real merge --------------------------------------------------------------------------------


def test_real_merge_has_both_parents_and_prepares_deterministically(
    repo: Path, tmp_path: Path
) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"integration.txt": "integration\n"})
    # Advance main independently so the delivery needs a real merge commit.
    write(repo / "target.txt", "target\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "target work")
    target_tip = head(repo)
    create_run(repo, base=base, integration=integration)

    first = prepare_apply(repo, RUN)
    second = prepare_apply(repo, RUN)

    assert first == second  # pinned identity/timestamps make preparation deterministic
    assert first.prepared_commit not in (target_tip, integration)
    parents = git(repo, "show", "-s", "--format=%P", first.prepared_commit).split()
    assert set(parents) == {target_tip, integration}
    assert git(repo, "show", "-s", "--format=%an", first.prepared_commit) == ORCHESTRATOR_NAME
    assert git(repo, "show", "-s", "--format=%ae", first.prepared_commit) == ORCHESTRATOR_EMAIL
    assert (
        git(repo, "show", "-s", "--format=%s", first.prepared_commit)
        == f"relay: apply {RUN} to refs/heads/main"
    )
    # Only the run's own delta is delivered; the target's existing work is not "changed".
    assert first.changed_paths == ("integration.txt",)
    files = git(repo, "ls-tree", "-r", "--name-only", first.prepared_commit).splitlines()
    assert "integration.txt" in files and "target.txt" in files
    assert worktree_count(repo) == 1


# --- already applied ---------------------------------------------------------------------------


def test_already_applied_is_a_noop(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)

    first = prepare_apply(repo, RUN)
    assert commit_ref(repo, first).changed is True

    second = prepare_apply(repo, RUN)
    assert second.noop is True
    assert second.prepared_commit == second.target_tip == integration
    assert second.changed_paths == ()
    result = commit_ref(repo, second)
    assert result.changed is False
    assert read_ref(repo, "refs/heads/main") == integration


def test_noop_still_guards_the_expected_ref(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    plan = prepare_apply(repo, RUN)
    assert commit_ref(repo, plan).changed is True

    noop = prepare_apply(repo, RUN)
    assert noop.noop is True
    git(repo, "update-ref", "-d", "refs/heads/main")  # concurrent deletion

    with pytest.raises(TargetMovedError) as exc:
        commit_ref(repo, noop)
    assert exc.value.observed is None
    assert read_ref(repo, "refs/heads/main") is None


def test_new_branch_created_concurrently_is_refused(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    plan = prepare_apply(repo, RUN, target="feature")
    racer = dummy_commit(repo, base, "concurrent feature")
    git(repo, "update-ref", "refs/heads/feature", racer)

    with pytest.raises(TargetMovedError) as exc:
        commit_ref(repo, plan)
    assert exc.value.expected is None
    assert exc.value.observed == racer
    assert read_ref(repo, "refs/heads/feature") == racer


# --- integration resolution --------------------------------------------------------------------


def test_resolution_falls_back_to_the_integration_branch(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"x.txt": "x\n"})
    create_run(
        repo,
        base=base,
        integration=integration,
        final=False,
        started=False,
        finished=False,
    )

    assert resolve_integration_commit(repo, RUN) == integration


def test_missing_run_is_reported(repo: Path) -> None:
    with pytest.raises(RunMissingError, match="not found"):
        resolve_integration_commit(repo, RUN)


def test_incomplete_run_is_refused(repo: Path) -> None:
    base = head(repo)
    create_run(repo, base=base, integration=base, state="running", final=False, finished=False)
    with pytest.raises(RunNotDoneError, match="not 'done'"):
        resolve_integration_commit(repo, RUN)


def test_done_run_without_integration_is_refused(repo: Path) -> None:
    base = head(repo)
    create_run(repo, base=base, integration=None, final=False, started=False, finished=False)
    with pytest.raises(IntegrationMissingError, match="no integration commit"):
        resolve_integration_commit(repo, RUN)


def test_resolution_order_is_record_then_event_then_branch(repo: Path) -> None:
    base = head(repo)
    record_commit = dummy_commit(repo, base, "record")
    event_commit = dummy_commit(repo, base, "event")
    branch_commit = dummy_commit(repo, base, "branch")
    store = create_run(
        repo,
        base=base,
        integration=record_commit,
        finished_commit=event_commit,
        branch=False,
    )
    git(repo, "update-ref", integration_branch(RUN), branch_commit)

    assert resolve_integration_commit(repo, RUN) == record_commit

    store.update_run(lambda record: setattr(record, "final_commit", None))
    assert resolve_integration_commit(repo, RUN) == event_commit

    store.events_path.unlink()
    assert resolve_integration_commit(repo, RUN) == branch_commit


# --- merge conflicts ---------------------------------------------------------------------------


def test_merge_conflict_reports_paths_and_leaves_the_branch_alone(
    repo: Path, tmp_path: Path
) -> None:
    base = head(repo)
    write(repo / "conflict.txt", "base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add conflict file")
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"conflict.txt": "integration side\n"})
    write(repo / "conflict.txt", "target side\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "target side")
    target_tip = head(repo)
    create_run(repo, base=base, integration=integration)
    before = snapshot(repo)

    with pytest.raises(MergeConflictError) as exc:
        prepare_apply(repo, RUN)

    assert exc.value.conflicts == ["conflict.txt"]
    assert "conflict.txt" in str(exc.value)
    assert snapshot(repo) == before
    assert read_ref(repo, "refs/heads/main") == target_tip
    assert worktree_count(repo) == 1


# --- dirty working tree ------------------------------------------------------------------------


def test_dirty_paths_outside_the_delta_are_allowed(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    write(repo / "README.md", "# user edit\n")  # tracked, untouched by the run

    plan = prepare_apply(repo, RUN)

    assert plan.changed_paths == ("feature.txt",)
    assert plan.overlaps == ()
    assert plan.clean is True


def test_dirty_overlap_is_refused_with_paths(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    write(repo / "feature.txt", "user edit\n")  # untracked file the run also delivers

    with pytest.raises(DirtyOverlapError) as exc:
        prepare_apply(repo, RUN)

    assert exc.value.overlaps == ["feature.txt"]
    assert "feature.txt" in str(exc.value)
    assert read_ref(repo, "refs/heads/main") == base


def test_rename_overlap_covers_both_sides(repo: Path, tmp_path: Path) -> None:
    write(repo / "old.txt", "content\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "add old")
    base = head(repo)
    integration = commit_in_worktree(
        repo, tmp_path, base, {"old.txt": None, "new.txt": "content\n"}
    )
    create_run(repo, base=base, integration=integration)
    git(repo, "mv", "old.txt", "new.txt")  # staged rename in the user's checkout

    with pytest.raises(DirtyOverlapError) as exc:
        prepare_apply(repo, RUN)

    assert set(exc.value.overlaps) == {"old.txt", "new.txt"}


def test_reserved_runtime_dirt_is_ignored(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    write(repo / ".agents" / "apply" / "op-1" / "plan.json", "{}\n")

    plan = prepare_apply(repo, RUN)

    assert plan.clean is True
    assert plan.changed_paths == ("feature.txt",)


def test_baseline_state_is_not_treated_as_user_dirt(repo: Path, tmp_path: Path) -> None:
    write(repo / "pre-existing.txt", "pre-existing\n")  # user's dirty file at run start
    synthetic = resolve_base(repo, RUN, allow_dirty=True).commit
    integration = commit_in_worktree(
        repo,
        tmp_path,
        synthetic,
        {"pre-existing.txt": "delivered\n"},
    )
    create_run(repo, base=synthetic, integration=integration)

    plan = prepare_apply(repo, RUN)

    # The working-tree blob equals the captured baseline blob, so the edited file is accepted
    # as pre-existing state rather than a concurrent edit.
    assert plan.changed_paths == ("pre-existing.txt",)
    assert plan.clean is True


# --- target updates ----------------------------------------------------------------------------


def test_target_moved_after_prepare_is_refused(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    plan = prepare_apply(repo, RUN)

    write(repo / "later.txt", "later\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later work")
    moved_to = head(repo)

    with pytest.raises(TargetMovedError) as exc:
        commit_ref(repo, plan)

    assert exc.value.observed == moved_to
    assert exc.value.expected == base
    assert read_ref(repo, "refs/heads/main") == moved_to


def test_new_branch_is_created_without_touching_main(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    before = snapshot(repo)

    plan = prepare_apply(repo, RUN, target="feature")

    assert plan.target_ref == "refs/heads/feature"
    assert plan.expected_old is None
    assert plan.target_tip == base
    assert plan.prepared_commit == integration
    assert snapshot(repo) == before
    assert read_ref(repo, "refs/heads/feature") is None

    result = commit_ref(repo, plan)
    assert result.changed is True
    assert read_ref(repo, "refs/heads/feature") == integration
    assert read_ref(repo, "refs/heads/main") == base
    assert git(repo, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert snapshot(repo) == before  # HEAD, index and working tree are untouched


def test_full_target_ref_is_accepted_and_others_are_refused(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)

    plan = prepare_apply(repo, RUN, target="refs/heads/feature")
    assert plan.target_ref == "refs/heads/feature"

    with pytest.raises(TargetRefError):
        prepare_apply(repo, RUN, target="refs/remotes/origin/main")
    with pytest.raises(TargetRefError):
        prepare_apply(repo, RUN, target="refs/tags/v1")


@pytest.mark.parametrize("bad", ["", "-bad", "HEAD", "../x", "a..b", "x.lock"])
def test_unsafe_target_names_are_refused(repo: Path, tmp_path: Path, bad: str) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)

    with pytest.raises(TargetRefError):
        prepare_apply(repo, RUN, target=bad)


def test_detached_head_needs_an_explicit_target(repo: Path, tmp_path: Path) -> None:
    base = head(repo)
    integration = commit_in_worktree(repo, tmp_path, base, {"feature.txt": "f\n"})
    create_run(repo, base=base, integration=integration)
    git(repo, "checkout", "--detach", "HEAD")

    with pytest.raises(TargetRefError, match="detached"):
        prepare_apply(repo, RUN)

    plan = prepare_apply(repo, RUN, target="detached-apply")
    assert plan.target_ref == "refs/heads/detached-apply"


def test_base_ref_fallback_is_used_for_baseline(repo: Path, tmp_path: Path) -> None:
    write(repo / "pre-existing.txt", "pre-existing\n")
    synthetic = resolve_base(repo, RUN, allow_dirty=True).commit
    integration = commit_in_worktree(repo, tmp_path, synthetic, {"pre-existing.txt": "delivered\n"})
    # No run.started event: the synthetic base ref is the only baseline record.
    create_run(repo, base=synthetic, integration=integration, started=False)

    assert read_ref(repo, base_ref(RUN)) == synthetic
    plan = prepare_apply(repo, RUN)
    assert plan.base_commit == synthetic
    assert plan.clean is True
