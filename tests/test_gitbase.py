from __future__ import annotations

from pathlib import Path

import pytest

from harness import git, init_repo
from relay.core.gitbase import (
    DirtyTreeError,
    EmptyRepositoryError,
    InvalidRefError,
    NotARepositoryError,
    base_ref,
    is_reserved_path,
    read_ref,
    ref_namespace,
    resolve_base,
    scan_sensitive_paths,
    write_ref,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def tree_files(repo: Path, commit: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()


# --- clean-tree pinning -------------------------------------------------------------


def test_clean_tree_pins_head(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    result = resolve_base(repo, "2026-10-22-a1b2")
    assert result.commit == head
    assert not result.synthetic
    assert result.parent is None
    assert result.branch == "main"
    assert result.sensitive_paths == ()


def test_dirty_tree_without_allow_dirty_reports_paths(repo: Path) -> None:
    (repo / "new.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text("# edited\n", encoding="utf-8", newline="\n")
    with pytest.raises(DirtyTreeError) as exc:
        resolve_base(repo, "2026-10-22-a1b2")
    assert "new.txt" in str(exc.value)
    assert "README.md" in str(exc.value)
    assert "allow-dirty" in str(exc.value)


# --- synthetic base for --allow-dirty -----------------------------------------------


def test_dirty_tree_allow_dirty_builds_synthetic_base(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    (repo / "new.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    (repo / "README.md").write_text("# edited\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert result.synthetic
    assert result.commit != head
    assert result.parent == head
    files = tree_files(repo, result.commit)
    assert "new.txt" in files
    assert git(repo, "show", f"{result.commit}:new.txt") == "hello"
    assert git(repo, "show", f"{result.commit}:README.md") == "# edited"


def test_synthetic_base_leaves_branch_and_tree_untouched(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    before = git(repo, "status", "--porcelain", "--untracked-files=all")
    (repo / "new.txt").write_text("x\n", encoding="utf-8", newline="\n")
    resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == "?? new.txt"
    assert before == ""


def test_sensitive_paths_are_excluded_and_reported(repo: Path) -> None:
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8", newline="\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "id_rsa").write_text("KEY\n", encoding="utf-8", newline="\n")
    (repo / "normal.txt").write_text("ok\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert set(result.sensitive_paths) == {".env", "sub/id_rsa"}
    files = tree_files(repo, result.commit)
    assert ".env" not in files
    assert "sub/id_rsa" not in files
    assert "normal.txt" in files


def test_exclude_patterns_omit_paths(repo: Path) -> None:
    (repo / "keep.txt").write_text("k\n", encoding="utf-8", newline="\n")
    (repo / "drop.log").write_text("d\n", encoding="utf-8", newline="\n")
    (repo / "tmp").mkdir()
    (repo / "tmp" / "x.tmp").write_text("t\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True, exclude=["*.log", "tmp/"])
    files = tree_files(repo, result.commit)
    assert "keep.txt" in files
    assert "drop.log" not in files
    assert "tmp/x.tmp" not in files
    assert "drop.log" in result.excluded_paths
    assert "tmp/x.tmp" in result.excluded_paths


def test_exclude_star_stays_within_one_segment(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "top.log").write_text("t\n", encoding="utf-8", newline="\n")
    (repo / "sub" / "deep.log").write_text("d\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True, exclude=["*.log"])
    files = tree_files(repo, result.commit)
    assert "top.log" not in files
    assert "sub/deep.log" in files


def test_exclude_double_star_crosses_segments(repo: Path) -> None:
    (repo / "sub").mkdir()
    (repo / "top.log").write_text("t\n", encoding="utf-8", newline="\n")
    (repo / "sub" / "deep.log").write_text("d\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True, exclude=["**/*.log"])
    files = tree_files(repo, result.commit)
    assert "top.log" not in files
    assert "sub/deep.log" not in files


def test_runtime_state_is_never_captured(repo: Path) -> None:
    state = repo / ".agents" / "runs" / "r1"
    state.mkdir(parents=True)
    (state / "state.txt").write_text("s\n", encoding="utf-8", newline="\n")
    (repo / ".agents" / "agents.yaml").write_text("version: 1\n", encoding="utf-8", newline="\n")
    (repo / "normal.txt").write_text("ok\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    files = tree_files(repo, result.commit)
    assert ".agents/runs/r1/state.txt" not in files
    assert ".agents/agents.yaml" in files
    assert "normal.txt" in files


# --- ref namespace ------------------------------------------------------------------


def test_base_ref_points_at_synthetic_commit(repo: Path) -> None:
    (repo / "new.txt").write_text("x\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert base_ref("2026-10-22-a1b2") == "refs/relay/2026-10-22-a1b2/base"
    assert ref_namespace("2026-10-22-a1b2") == "refs/relay/2026-10-22-a1b2"
    assert read_ref(repo, base_ref("2026-10-22-a1b2")) == result.commit


def test_synthetic_base_is_idempotent(repo: Path) -> None:
    (repo / "new.txt").write_text("hello\n", encoding="utf-8", newline="\n")
    first = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    (repo / "new.txt").write_text("changed later\n", encoding="utf-8", newline="\n")
    second = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert second.commit == first.commit
    assert git(repo, "show", f"{second.commit}:new.txt") == "hello"


def test_missing_ref_reads_as_none(repo: Path) -> None:
    assert read_ref(repo, "refs/relay/nope/base") is None


# --- failure modes ------------------------------------------------------------------


def test_not_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotARepositoryError, match="not inside a git repository"):
        resolve_base(plain, "2026-10-22-a1b2")


def test_empty_repository(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    with pytest.raises(EmptyRepositoryError, match="no commits"):
        resolve_base(empty, "2026-10-22-a1b2")


@pytest.mark.parametrize("bad", ["../x", "a b", "a/b", ".hidden", "x.lock", "x~1"])
def test_unsafe_run_ids_are_rejected(repo: Path, bad: str) -> None:
    with pytest.raises(InvalidRefError):
        resolve_base(repo, bad)


def test_invalid_exclude_patterns_are_rejected(repo: Path) -> None:
    with pytest.raises(ValueError):
        resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True, exclude=["../etc"])
    with pytest.raises(ValueError):
        resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True, exclude=["/etc"])
    with pytest.raises(InvalidRefError):
        resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True, exclude=["   "])


# --- ref safety ---------------------------------------------------------------------


def test_read_ref_rejects_leading_dash(repo: Path) -> None:
    with pytest.raises(InvalidRefError):
        read_ref(repo, "-x")


def test_read_ref_rejects_whitespace(repo: Path) -> None:
    with pytest.raises(InvalidRefError):
        read_ref(repo, "refs/relay/a b")


def test_write_ref_rejects_leading_dash(repo: Path) -> None:
    with pytest.raises(InvalidRefError):
        write_ref(repo, "-x", "0" * 40)


def test_write_ref_rejects_outside_namespace(repo: Path) -> None:
    with pytest.raises(InvalidRefError):
        write_ref(repo, "refs/heads/main", "0" * 40)


def test_write_ref_accepts_relay_namespace_ref(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD")
    ref = base_ref("2026-10-22-a1b2")
    write_ref(repo, ref, head)
    assert read_ref(repo, ref) == head


# --- submodules ---------------------------------------------------------------------


def _submodule_repo(tmp_path: Path) -> Path:
    """A repo with a committed submodule at `mod`."""
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
    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(sub),
        "mod",
    )
    git(repo, "commit", "-q", "-m", "add submodule")
    return repo


def test_clean_submodule_is_not_reported_dirty(tmp_path: Path) -> None:
    repo = _submodule_repo(tmp_path)
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert result.dirty_submodules == ()


def test_dirty_submodule_is_reported(tmp_path: Path) -> None:
    repo = _submodule_repo(tmp_path)
    (repo / "mod" / "s.txt").write_text("dirty\n", encoding="utf-8", newline="\n")
    result = resolve_base(repo, "2026-10-22-a1b2", allow_dirty=True)
    assert result.dirty_submodules == ("mod",)


# --- pure helpers -------------------------------------------------------------------


def test_scan_sensitive_paths_matches_any_component() -> None:
    paths = [
        ".env",
        "config/.env.local",
        "server.pem",
        "keys/id_rsa.pub",
        "credentials/aws.json",
        "src/secret.txt",
        "readme.md",
        "main.py",
    ]
    assert scan_sensitive_paths(paths) == [
        ".env",
        "config/.env.local",
        "credentials/aws.json",
        "keys/id_rsa.pub",
        "server.pem",
        "src/secret.txt",
    ]


def test_is_reserved_path() -> None:
    assert is_reserved_path(".git/config")
    assert is_reserved_path(".agents/runs/r1/events.jsonl")
    assert is_reserved_path(".agents/worktrees/r1/n1/a.py")
    assert not is_reserved_path(".agents/agents.yaml")
    assert not is_reserved_path("src/main.py")
    assert not is_reserved_path(".gitignore")
