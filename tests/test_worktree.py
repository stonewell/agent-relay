from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import relay.core.worktree as wt
from harness import git, init_repo
from relay.core.worktree import (
    CARGO_TARGET_DIR,
    InvalidWorktreeConfig,
    WorktreeError,
    WorktreeManager,
    WorktreePathInUse,
    node_dir_name,
    resolve_worktree_root,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def manager(repo: Path, run_id: str = "2026-10-22-a1b2", **kwargs: object) -> WorktreeManager:
    return WorktreeManager(repo, run_id, **kwargs)  # type: ignore[arg-type]


def head_of(repo: Path, path: Path) -> str:
    return git(path, "rev-parse", "HEAD")


def admin_dir(path: Path) -> str:
    gitfile = (path / ".git").read_text(encoding="utf-8")
    return Path(gitfile.split(":", 1)[1].strip()).name


def tree_files(path: Path) -> list[str]:
    return git(path, "ls-files").splitlines()


# --- creation and removal -------------------------------------------------------------


def test_create_checks_out_input_commit(repo: Path) -> None:
    m = manager(repo)
    head = git(repo, "rev-parse", "HEAD")
    wt_path = m.create("n1", head)
    assert wt_path.path.is_dir()
    assert head_of(repo, wt_path.path) == head
    assert "README.md" in tree_files(wt_path.path)
    assert wt_path.detached
    assert wt_path.branch is None


def test_create_pins_to_the_given_commit_not_branch_tip(repo: Path) -> None:
    first = git(repo, "rev-parse", "HEAD")
    (repo / "second.txt").write_text("2\n", encoding="utf-8", newline="\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "second")
    tip = git(repo, "rev-parse", "HEAD")
    assert tip != first

    wt_path = manager(repo).create("n1", first)
    assert head_of(repo, wt_path.path) == first
    assert "second.txt" not in tree_files(wt_path.path)


def test_decommission_removes_worktree(repo: Path) -> None:
    m = manager(repo)
    created = m.create("n1", git(repo, "rev-parse", "HEAD"))
    result = m.decommission("n1")
    assert result.removed
    assert not created.path.exists()
    assert created.path not in [Path(line) for line in git(repo, "worktree", "list").splitlines()]


def test_create_with_branch_checks_out_on_branch(repo: Path) -> None:
    wt_path = manager(repo).create("n1", git(repo, "rev-parse", "HEAD"), branch="relay/x/n1")
    assert not wt_path.detached
    assert wt_path.branch == "relay/x/n1"
    assert git(repo, "rev-parse", "relay/x/n1") == head_of(repo, wt_path.path)


# --- unique, collision-free directories ------------------------------------------------------


def test_distinct_nodes_get_distinct_directories(repo: Path) -> None:
    m = manager(repo)
    assert m.node_path("n1-impl") != m.node_path("n2-impl")


def test_short_layout_truncates_and_hashes_shared_prefixes(repo: Path, tmp_path: Path) -> None:
    long_a = "a" * 30 + "-x"
    long_b = "a" * 30 + "-y"
    short = manager(repo, worktree_root=str(tmp_path / "wt"), platform="win32")
    path_a = short.node_path(long_a)
    path_b = short.node_path(long_b)
    assert path_a != path_b
    assert path_a.name.startswith("a" * 16)
    assert len(path_a.name) < len(long_a)


def test_node_dir_name_keeps_short_ids_and_full_ids(repo: Path) -> None:
    assert node_dir_name("n1", short=True) == "n1"
    assert node_dir_name("n1", short=False) == "n1"
    long = "x" * 40
    shortened = node_dir_name(long, short=True)
    assert shortened != long
    assert shortened.startswith("x" * 16)
    assert node_dir_name(long, short=False) == long


def test_same_node_maps_to_same_path_deterministically(repo: Path) -> None:
    a = manager(repo).node_path("n1")
    b = manager(repo).node_path("n1")
    assert a == b


def test_different_runs_never_share_a_worktree_path(repo: Path, tmp_path: Path) -> None:
    short = manager(
        repo, run_id="2026-10-22-aaaa", worktree_root=str(tmp_path / "wt"), platform="win32"
    )
    other = manager(
        repo, run_id="2026-10-22-bbbb", worktree_root=str(tmp_path / "wt"), platform="win32"
    )
    assert short.node_path("n1") != other.node_path("n1")


# --- input commit and reuse ----------------------------------------------------------------


def test_unknown_input_commit_is_rejected(repo: Path) -> None:
    with pytest.raises(WorktreeError, match="does not exist"):
        manager(repo).create("n1", "0" * 40)


def test_recreate_reuses_an_existing_worktree(repo: Path) -> None:
    m = manager(repo)
    first = m.create("n1", git(repo, "rev-parse", "HEAD"))
    (first.path / "edit.txt").write_text("edit\n", encoding="utf-8", newline="\n")
    again = m.create("n1", git(repo, "rev-parse", "HEAD"))
    assert again.path == first.path
    assert "reused existing worktree" in again.setup_notes
    assert (again.path / "edit.txt").is_file()


def test_recreate_onto_an_unrelated_directory_fails(repo: Path) -> None:
    m = manager(repo)
    path = m.node_path("n1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    (path / "junk.txt").write_text("junk\n", encoding="utf-8", newline="\n")
    with pytest.raises(WorktreePathInUse):
        m.create("n1", git(repo, "rev-parse", "HEAD"))


@pytest.mark.parametrize("bad", ["HEAD~1", "a b", "a..b", "-x"])
def test_create_rejects_unsafe_input_commit(repo: Path, bad: str) -> None:
    with pytest.raises(InvalidWorktreeConfig):
        manager(repo).create("n1", bad)


@pytest.mark.parametrize("bad", ["a b", "a..b", "a~b"])
def test_node_id_rejects_unsafe_components(repo: Path, bad: str) -> None:
    with pytest.raises(InvalidWorktreeConfig):
        manager(repo).node_path(bad)


def test_creation_failure_cleans_up_worktree_and_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = manager(repo)
    head = git(repo, "rev-parse", "HEAD")
    path = m.node_path("n1")

    def fail_setup(self: WorktreeManager, _path: Path) -> tuple[str, ...]:
        raise RuntimeError("forced submodule failure")

    monkeypatch.setattr(WorktreeManager, "_setup_aux", fail_setup)
    with pytest.raises(RuntimeError, match="forced submodule failure"):
        m.create("n1", head, branch="relay/x/n1")

    # The just-created worktree, its admin metadata and the branch are all rolled back.
    assert not path.exists()
    listed = git(repo, "worktree", "list", "--porcelain").splitlines()
    assert f"worktree {path}" not in listed
    assert git(repo, "branch", "--list", "relay/x/n1") == ""


# --- submodules ---------------------------------------------------------------------------


def _submodule_repo(tmp_path: Path) -> Path:
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
    return repo


def test_submodules_are_initialized_in_the_worktree(tmp_path: Path) -> None:
    repo = _submodule_repo(tmp_path)
    wt_path = manager(repo).create("n1", git(repo, "rev-parse", "HEAD"))
    assert (wt_path.path / "mod" / "s.txt").read_text(encoding="utf-8").strip() == "s"
    assert "submodules initialized" in wt_path.setup_notes


def test_no_submodules_are_reported_not_an_error(repo: Path) -> None:
    wt_path = manager(repo).create("n1", git(repo, "rev-parse", "HEAD"))
    assert "no submodules" in wt_path.setup_notes


# --- LFS ----------------------------------------------------------------------------------


def test_lfs_absent_degrades_with_a_note(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8", newline="\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "declare lfs")

    def no_lfs(_repo: Path) -> bool:
        return False

    monkeypatch.setattr(wt, "_git_lfs_available", no_lfs)
    wt_path = manager(repo).create("n1", git(repo, "rev-parse", "HEAD"))
    assert wt_path.path.is_dir()
    assert any("git-lfs not installed" in note for note in wt_path.setup_notes)


@pytest.mark.skipif(shutil.which("git-lfs") is None, reason="git-lfs is not installed")
def test_lfs_pulled_when_declared_and_available(repo: Path) -> None:
    (repo / ".gitattributes").write_text(
        "*.bin filter=lfs diff=lfs merge=lfs -text\n", encoding="utf-8", newline="\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "declare lfs")
    wt_path = manager(repo).create("n1", git(repo, "rev-parse", "HEAD"))
    assert "lfs pulled" in wt_path.setup_notes


def test_lfs_not_declared_is_skipped_without_error(repo: Path) -> None:
    wt_path = manager(repo).create("n1", git(repo, "rev-parse", "HEAD"))
    assert not any(note.startswith("lfs") for note in wt_path.setup_notes)


# --- cache isolation ----------------------------------------------------------------------


def test_cache_env_points_into_per_node_scratch(repo: Path) -> None:
    m = manager(repo)
    env = m.cache_env("n1")
    assert env[CARGO_TARGET_DIR] == str(repo / ".agents" / "scratch" / m.run_id / "n1" / "target")


def test_cache_env_is_distinct_per_node(repo: Path) -> None:
    m = manager(repo)
    assert m.cache_env("n1") != m.cache_env("n2")


def test_worktree_carries_its_cache_env(repo: Path) -> None:
    m = manager(repo)
    wt_path = m.create("n1", git(repo, "rev-parse", "HEAD"))
    assert wt_path.cache_env == m.cache_env("n1")


# --- decommission: idempotency, leaks, safety ---------------------------------------------


def test_double_decommission_is_idempotent(repo: Path) -> None:
    m = manager(repo)
    m.create("n1", git(repo, "rev-parse", "HEAD"))
    first = m.decommission("n1")
    second = m.decommission("n1")
    assert first.removed
    assert not second.removed
    assert first.path == second.path


def test_decommission_leaves_no_git_worktrees_entry(repo: Path) -> None:
    m = manager(repo)
    created = m.create("n1", git(repo, "rev-parse", "HEAD"))
    name = admin_dir(created.path)
    assert (repo / ".git" / "worktrees" / name).is_dir()
    m.decommission("n1")
    assert not (repo / ".git" / "worktrees" / name).exists()
    listed = git(repo, "worktree", "list", "--porcelain")
    assert f"worktree {created.path}" not in listed.splitlines()


def test_decommission_forces_removal_of_a_dirty_worktree(repo: Path) -> None:
    m = manager(repo)
    created = m.create("n1", git(repo, "rev-parse", "HEAD"))
    (created.path / "dirty.txt").write_text("d\n", encoding="utf-8", newline="\n")
    assert m.decommission("n1").removed
    assert not created.path.exists()


def test_failed_node_worktree_is_retained_until_decommissioned(repo: Path) -> None:
    """Retention is a caller policy (design section 6): not decommissioning keeps the tree."""
    m = manager(repo)
    created = m.create("n1", git(repo, "rev-parse", "HEAD"))
    (created.path / "broken.txt").write_text("x\n", encoding="utf-8", newline="\n")
    # Simulate a failed node: the caller leaves it in place for debugging.
    assert created.path.is_dir()
    assert "broken.txt" in tree_files(created.path) or (created.path / "broken.txt").is_file()
    # Decommissioning later still removes it.
    assert m.decommission("n1").removed
    assert not created.path.exists()


def test_decommission_refuses_the_main_worktree(repo: Path) -> None:
    with pytest.raises(WorktreeError, match="main worktree"):
        manager(repo).decommission(repo)


def test_decommission_refuses_a_path_outside_the_root(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("k\n", encoding="utf-8", newline="\n")
    with pytest.raises(WorktreeError, match="outside this manager's worktree root"):
        manager(repo).decommission(outside)
    assert (outside / "keep.txt").is_file()


def test_decommission_refuses_a_registered_worktree_outside_the_root(
    repo: Path, tmp_path: Path
) -> None:
    m = manager(repo)
    head = git(repo, "rev-parse", "HEAD")
    outside = tmp_path / "outside-wt"
    git(repo, "worktree", "add", "--detach", str(outside), head)
    assert (outside / ".git").exists()
    with pytest.raises(WorktreeError, match="outside this manager's worktree root"):
        m.decommission(outside)
    assert outside.is_dir()  # the registered worktree is left intact


def test_decommission_refuses_a_dotdot_path_escaping_the_root(repo: Path, tmp_path: Path) -> None:
    m = manager(repo)
    head = git(repo, "rev-parse", "HEAD")
    outside = tmp_path / "outside-wt"
    git(repo, "worktree", "add", "--detach", str(outside), head)
    # m.root is <tmp>/repo/.agents/worktrees; three ".." levels reach <tmp>.
    sneaky = m.root / ".." / ".." / ".." / "outside-wt"
    with pytest.raises(WorktreeError, match="outside this manager's worktree root"):
        m.decommission(sneaky)
    assert outside.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs elevation on Windows")
def test_decommission_refuses_a_symlink_escaping_the_root(repo: Path, tmp_path: Path) -> None:
    m = manager(repo)
    head = git(repo, "rev-parse", "HEAD")
    outside = tmp_path / "outside-wt"
    git(repo, "worktree", "add", "--detach", str(outside), head)
    m.root.mkdir(parents=True, exist_ok=True)
    link = m.root / "sneaky"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorktreeError, match="outside this manager's worktree root"):
        m.decommission(link)
    assert outside.is_dir()


def test_decommission_candidate_removes_only_its_registered_candidate(
    repo: Path, tmp_path: Path
) -> None:
    m = manager(repo)
    head = git(repo, "rev-parse", "HEAD")
    candidate = tmp_path / "candidate"
    git(repo, "worktree", "add", "--detach", str(candidate), head)
    assert (candidate / ".git").exists()
    owned = m.create("n1", head)  # a worktree this manager owns, which must be untouched

    result = m.decommission_candidate(candidate)
    assert result.removed
    assert not candidate.exists()
    assert owned.path.is_dir()  # only the candidate was removed
    listed = git(repo, "worktree", "list", "--porcelain").splitlines()
    assert f"worktree {candidate}" not in listed
    assert f"worktree {owned.path}" in listed
    assert not m.decommission_candidate(candidate).removed  # idempotent


def test_decommission_candidate_never_touches_an_unrelated_directory(
    repo: Path, tmp_path: Path
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("k\n", encoding="utf-8", newline="\n")
    result = manager(repo).decommission_candidate(unrelated)
    assert not result.removed
    assert (unrelated / "keep.txt").is_file()


# --- root resolution ----------------------------------------------------------------------


def test_auto_root_is_repo_agents_worktrees_on_posix(repo: Path) -> None:
    resolved = resolve_worktree_root(repo, platform="linux")
    assert resolved.root == repo / ".agents" / "worktrees"
    assert not resolved.short
    assert not resolved.explicit


def test_auto_root_is_short_and_outside_repo_on_windows(repo: Path) -> None:
    resolved = resolve_worktree_root(
        repo, platform="win32", parent_env={"LOCALAPPDATA": "/tmp/localapp"}
    )
    assert resolved.root == Path("/tmp/localapp") / "relay" / "wt"
    assert resolved.short
    assert not resolved.explicit


def test_windows_auto_root_falls_back_when_localappdata_unset(repo: Path) -> None:
    resolved = resolve_worktree_root(repo, platform="win32", parent_env={})
    assert resolved.short
    assert resolved.note is not None


def test_explicit_absolute_root_is_used_verbatim(repo: Path, tmp_path: Path) -> None:
    explicit = tmp_path / "my" / "wt"
    resolved = resolve_worktree_root(repo, worktree_root=str(explicit), platform="linux")
    assert resolved.root == explicit
    assert resolved.explicit


def test_explicit_relative_root_is_rejected(repo: Path) -> None:
    with pytest.raises(InvalidWorktreeConfig, match="absolute"):
        resolve_worktree_root(repo, worktree_root="relative/path")


def test_short_layout_path_stays_bounded(repo: Path, tmp_path: Path) -> None:
    m = manager(
        repo, run_id="2026-10-22-aaaa", worktree_root=str(tmp_path / "wt"), platform="win32"
    )
    path = m.node_path("n" * 100)
    # root (short) + repo hash (8) + run-short + node-short, all bounded.
    assert len(path.name) <= 16 + 1 + 4  # 16-char prefix + '-' + 4-char hash
    assert len(path.parent.name) <= 8 + 1 + 4


def _config_get(repo: Path, key: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key], capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def test_short_create_sets_per_worktree_longpaths_not_shared(repo: Path, tmp_path: Path) -> None:
    """The Windows create path (no-checkout + per-worktree longpaths + checkout) is exercised
    on every OS: `git`'s worktree-config machinery is cross-platform even though the 260-char
    limit itself is not."""
    m = manager(repo, worktree_root=str(tmp_path / "wt"), platform="win32")
    head = git(repo, "rev-parse", "HEAD")
    created = m.create("n1", head)
    assert created.path.is_dir()
    assert head_of(repo, created.path) == head
    assert "README.md" in tree_files(created.path)
    assert _config_get(created.path, "core.longpaths") == "true"
    assert _config_get(repo, "core.longpaths") is None


# --- Windows-only long-path round trip (not faked on Linux) ------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="long-path behaviour is a Windows concern")
def test_windows_300_char_path_round_trips(repo: Path, tmp_path: Path) -> None:
    m = manager(repo, worktree_root=str(tmp_path / "wt"), platform="win32")
    created = m.create("n1", git(repo, "rev-parse", "HEAD"))
    deep = created.path
    while len(str(deep)) < 300:
        deep = deep / ("a" * 40)
    deep.mkdir(parents=True)
    (deep / "payload.txt").write_text("ok\n", encoding="utf-8", newline="\n")
    assert (deep / "payload.txt").read_text(encoding="utf-8") == "ok"
    m.decommission("n1")
