from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from harness import git, init_repo
from relay.core.capture import (
    CandidateExistsError,
    CaptureResult,
    Change,
    InvalidInputCommitError,
    MissingWorktreeError,
    NotARepositoryError,
    capture_changes,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def make_worktree(repo: Path, tmp_path: Path, name: str = "node") -> tuple[Path, str]:
    """A detached worktree at the repo HEAD; returns (path, input_commit)."""
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / name
    git(repo, "worktree", "add", "--detach", str(wt), head)
    return wt, head


def capture(wt: Path, input_commit: str, tmp_path: Path, name: str = "candidate"):
    return capture_changes(wt, input_commit, candidate_dir=tmp_path / name)


def paths_by_status(result: CaptureResult) -> dict[str, str]:
    return {change.path: change.status for change in result.changes}


# --- empty and clean worktrees --------------------------------------------------------------


def test_clean_worktree_is_empty_delta(repo: Path, tmp_path: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    wt, input_commit = make_worktree(repo, tmp_path)
    assert input_commit == head
    result = capture(wt, input_commit, tmp_path)
    assert result.empty
    assert result.counts.total == 0
    assert result.changes == ()
    assert result.agent_commits == ()
    # The candidate tree is the input commit's tree, byte for byte.
    assert result.candidate_tree == git(repo, "rev-parse", f"{head}^{{tree}}")
    assert git(result.candidate_path, "status", "--porcelain") == ""


def test_candidate_matches_input_when_no_changes(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    result = capture(wt, input_commit, tmp_path)
    assert (result.candidate_path / "README.md").read_text(encoding="utf-8") == "# fixture\n"


# --- committed, staged and unstaged mixes ----------------------------------------------------


def test_committed_staged_and_unstaged_are_all_captured(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    # committed
    (wt / "committed.txt").write_text("c\n", encoding="utf-8", newline="\n")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "committed")
    # staged (not committed)
    (wt / "README.md").write_text("# staged edit\n", encoding="utf-8", newline="\n")
    git(wt, "add", "-A")
    # unstaged on top
    (wt / "README.md").write_text("# unstaged edit\n", encoding="utf-8", newline="\n")

    result = capture(wt, input_commit, tmp_path)
    assert paths_by_status(result) == {
        "committed.txt": "added",
        "README.md": "modified",
    }
    # the candidate reflects the final working-tree content
    assert (result.candidate_path / "committed.txt").read_text(encoding="utf-8") == "c\n"
    assert (result.candidate_path / "README.md").read_text(encoding="utf-8") == "# unstaged edit\n"
    assert len(result.agent_commits) == 1


def test_new_commits_on_top_of_input(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    for name in ("one.txt", "two.txt"):
        (wt / name).write_text(f"{name}\n", encoding="utf-8", newline="\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-q", "-m", name)
    result = capture(wt, input_commit, tmp_path)
    assert len(result.agent_commits) == 2
    assert {change.path for change in result.changes} == {"one.txt", "two.txt"}
    assert all(change.status == "added" for change in result.changes)


# --- untracked, deletion, rename ------------------------------------------------------------


def test_untracked_file_and_directory(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    (wt / "newfile.txt").write_text("n\n", encoding="utf-8", newline="\n")
    (wt / "newdir").mkdir()
    (wt / "newdir" / "inner.txt").write_text("i\n", encoding="utf-8", newline="\n")
    result = capture(wt, input_commit, tmp_path)
    assert paths_by_status(result) == {
        "newfile.txt": "added",
        "newdir/inner.txt": "added",
    }
    assert (result.candidate_path / "newdir" / "inner.txt").read_text(encoding="utf-8") == "i\n"


def test_deletion(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    (wt / "README.md").unlink()
    result = capture(wt, input_commit, tmp_path)
    (change,) = result.changes
    assert change == Change(
        path="README.md", status="deleted", old_mode="100644", new_mode="000000"
    )
    assert not (result.candidate_path / "README.md").exists()


def test_rename_staged(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    git(wt, "mv", "README.md", "RENAMED.md")
    result = capture(wt, input_commit, tmp_path)
    (change,) = result.changes
    assert change.status == "renamed"
    assert change.path == "RENAMED.md"
    assert change.old_path == "README.md"
    assert (result.candidate_path / "RENAMED.md").exists()
    assert not (result.candidate_path / "README.md").exists()


def test_rename_unstaged(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    os.rename(wt / "README.md", wt / "RENAMED.md")
    result = capture(wt, input_commit, tmp_path)
    (change,) = result.changes
    assert change.status == "renamed"
    assert change.path == "RENAMED.md"
    assert change.old_path == "README.md"


# --- binary and mode changes ----------------------------------------------------------------


def test_binary_file(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    payload = b"\x00\x01\x02\xff\xfe\x00binary\x00"
    (wt / "blob.bin").write_bytes(payload)
    result = capture(wt, input_commit, tmp_path)
    (change,) = result.changes
    assert change.status == "added"
    assert change.binary
    assert (result.candidate_path / "blob.bin").read_bytes() == payload


@pytest.mark.skipif(sys.platform == "win32", reason="executable bit is a POSIX concept")
def test_executable_bit_mode_change(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    os.chmod(wt / "README.md", 0o755)
    result = capture(wt, input_commit, tmp_path)
    (change,) = result.changes
    assert change.status == "modified"
    assert change.old_mode == "100644"
    assert change.new_mode == "100755"
    assert change.mode_changed
    mode = stat.S_IMODE((result.candidate_path / "README.md").stat().st_mode)
    assert mode & 0o111


# --- runtime-state exclusion -----------------------------------------------------------------


def test_runtime_state_excluded_from_delta_and_candidate(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    (wt / ".agents" / "runs" / "r1").mkdir(parents=True)
    (wt / ".agents" / "runs" / "r1" / "state.txt").write_text("s\n", encoding="utf-8", newline="\n")
    (wt / ".agents" / "scratch" / "x").mkdir(parents=True)
    (wt / ".agents" / "scratch" / "x" / "cache.bin").write_text(
        "c\n", encoding="utf-8", newline="\n"
    )
    (wt / ".agents" / "agents.yaml").write_text("version: 1\n", encoding="utf-8", newline="\n")
    (wt / "normal.txt").write_text("n\n", encoding="utf-8", newline="\n")

    result = capture(wt, input_commit, tmp_path)
    statuses = paths_by_status(result)
    assert "normal.txt" in statuses
    assert ".agents/agents.yaml" in statuses  # ordinary input is not runtime state
    assert not any(path.startswith(".agents/runs") for path in statuses)
    assert not any(path.startswith(".agents/scratch") for path in statuses)
    assert ".agents/runs/r1/state.txt" in result.excluded
    assert ".agents/scratch/x/cache.bin" in result.excluded
    # the candidate checkout has no runtime state
    assert not (result.candidate_path / ".agents" / "runs").exists()
    assert not (result.candidate_path / ".agents" / "scratch").exists()
    assert (result.candidate_path / ".agents" / "agents.yaml").exists()
    assert (result.candidate_path / "normal.txt").exists()


# --- candidate checkout fidelity -------------------------------------------------------------


def test_candidate_content_matches_worktree(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    (wt / "new.txt").write_text("new content\n", encoding="utf-8", newline="\n")
    (wt / "README.md").write_text("# edited\n", encoding="utf-8", newline="\n")
    result = capture(wt, input_commit, tmp_path)
    candidate = result.candidate_path
    assert (candidate / "new.txt").read_text(encoding="utf-8") == "new content\n"
    assert (candidate / "README.md").read_text(encoding="utf-8") == "# edited\n"
    # the candidate commit's tree is exactly the captured tree
    assert result.candidate_tree == git(repo, "rev-parse", f"{result.candidate_commit}^{{tree}}")
    assert result.candidate_commit != input_commit
    assert git(candidate, "status", "--porcelain") == ""


# --- errors ---------------------------------------------------------------------------------


def test_missing_worktree_path(tmp_path: Path) -> None:
    with pytest.raises(MissingWorktreeError, match="does not exist"):
        capture_changes(tmp_path / "gone", "0" * 40, candidate_dir=tmp_path / "candidate")


def test_non_repository_worktree(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotARepositoryError, match="not inside a git work tree"):
        capture_changes(plain, "0" * 40, candidate_dir=tmp_path / "candidate")


def test_unknown_input_commit(repo: Path, tmp_path: Path) -> None:
    wt, _ = make_worktree(repo, tmp_path)
    with pytest.raises(InvalidInputCommitError, match="does not exist"):
        capture(wt, "0" * 40, tmp_path)


def test_candidate_dir_must_not_exist(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    target = tmp_path / "candidate"
    target.mkdir()
    with pytest.raises(CandidateExistsError, match="already exists"):
        capture_changes(wt, input_commit, candidate_dir=target)


# --- canonical input commit (C1) -----------------------------------------------------------


def test_symbolic_input_commit_is_stored_as_sha(repo: Path, tmp_path: Path) -> None:
    wt, head = make_worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    result = capture_changes(wt, "main", candidate_dir=tmp_path / "candidate")
    # The symbolic input is resolved once and stored as the canonical sha, never verbatim.
    assert result.input_commit == head
    assert result.input_commit != "main"
    # The floating candidate commit is parented on that exact sha.
    assert git(repo, "rev-parse", f"{result.candidate_commit}^") == head


def test_publish_parent_is_the_canonical_input_sha(repo: Path, tmp_path: Path) -> None:
    from relay.core.publish import publish
    from relay.core.verify import VerifyResult

    wt, head = make_worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    capture = capture_changes(wt, "HEAD", candidate_dir=tmp_path / "candidate")
    assert capture.input_commit == head

    result = publish(
        repo,
        "2026-10-22-a1b2",
        "n1",
        capture,
        VerifyResult(outcome="passed", mode="default", checks=()),
        node_dir=tmp_path / "node-dir",
    )
    # Publication parents its output commit on the immutable canonical sha, not the symbolic rev.
    assert git(repo, "show", "-s", "--format=%P", result.output_commit) == head


@pytest.mark.parametrize("bad", ["HEAD~1", "main..dev", "has space", "-x", ""])
def test_unsafe_input_commit_is_rejected(repo: Path, tmp_path: Path, bad: str) -> None:
    wt, _ = make_worktree(repo, tmp_path)
    with pytest.raises(InvalidInputCommitError):
        capture_changes(wt, bad, candidate_dir=tmp_path / "candidate")


# --- deterministic ordering ------------------------------------------------------------------


def test_changes_are_deterministically_ordered(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    for name in ("z.txt", "a.txt", "m.txt"):
        (wt / name).write_text("x\n", encoding="utf-8", newline="\n")
    result = capture(wt, input_commit, tmp_path)
    paths = [change.path for change in result.changes]
    assert paths == sorted(paths)
    assert paths == ["a.txt", "m.txt", "z.txt"]


# --- what git cannot represent --------------------------------------------------------------


def test_empty_directory_is_reported_not_captured(repo: Path, tmp_path: Path) -> None:
    wt, input_commit = make_worktree(repo, tmp_path)
    (wt / "empty-dir").mkdir()
    result = capture(wt, input_commit, tmp_path)
    assert "empty-dir" in result.unrepresentable
    assert "empty-dir" not in {change.path for change in result.changes}
    assert not (result.candidate_path / "empty-dir").exists()


def _submodule_repo(tmp_path: Path) -> tuple[Path, Path]:
    sub = tmp_path / "sub"
    sub.mkdir()
    git(sub, "init", "-q", "-b", "main")
    (sub / "s.txt").write_text("s\n", encoding="utf-8", newline="\n")
    git(sub, "add", "-A")
    git(sub, "commit", "-q", "-m", "sub initial")

    repo = tmp_path / "parent"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "mod")
    git(repo, "commit", "-q", "-m", "add submodule")
    return repo, sub


def test_submodule_gitlink_change_is_captured(tmp_path: Path) -> None:
    repo, _sub = _submodule_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "node"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    (wt / "mod" / "s2.txt").write_text("s2\n", encoding="utf-8", newline="\n")
    git(wt / "mod", "add", "-A")
    git(wt / "mod", "commit", "-q", "-m", "advance")
    git(wt, "add", "mod")  # stage the moved gitlink in the superproject

    result = capture(wt, head, tmp_path / "candidate")
    (change,) = result.changes
    assert change.path == "mod"
    assert change.old_mode == "160000"
    assert change.new_mode == "160000"
    assert not result.dirty_submodules  # committed inside the submodule => clean
    # the candidate gitlink records the advanced submodule commit
    new_gitlink = git(repo, "ls-tree", result.candidate_commit, "mod").split()[2]
    assert new_gitlink == git(wt / "mod", "rev-parse", "HEAD")


def test_dirty_submodule_is_reported_not_captured(tmp_path: Path) -> None:
    repo, _sub = _submodule_repo(tmp_path)
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / "node"
    git(repo, "worktree", "add", "--detach", str(wt), head)
    git(wt, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
    (wt / "mod" / "s.txt").write_text("uncommitted\n", encoding="utf-8", newline="\n")

    result = capture(wt, head, tmp_path / "candidate")
    assert result.changes == ()  # gitlink unchanged, internal edit is not a superproject change
    assert "mod" in result.dirty_submodules
