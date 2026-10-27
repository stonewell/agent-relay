from __future__ import annotations

from pathlib import Path

import pytest

from harness import git, init_repo
from relay.core.gitexclude import (
    END,
    START,
    ExcludeError,
    block_lines,
    check_exclude_block,
    ensure_exclude_block,
    exclude_path,
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def make_runtime_state(root: Path) -> None:
    for sub in ("runs/r1", "locks", "worktrees/r1/n1", "scratch", "apply", "shadow-git"):
        target = root / ".agents" / sub
        target.mkdir(parents=True, exist_ok=True)
        (target / "file.txt").write_text("state\n")


def test_block_lists_every_runtime_subtree_but_not_git() -> None:
    lines = block_lines()
    assert lines[0] == START
    assert lines[-1] == END
    patterns = [line for line in lines if line.startswith("/")]
    assert patterns == [
        "/.agents/locks/",
        "/.agents/runs/",
        "/.agents/worktrees/",
        "/.agents/shadow-git/",
        "/.agents/scratch/",
        "/.agents/sessions/",
        "/.agents/apply/",
    ]


def test_adds_the_block_once_and_leaves_gitignore_alone(repo: Path) -> None:
    gitignore = repo / ".gitignore"
    gitignore.write_bytes(b"node_modules/\n")
    first = ensure_exclude_block(repo)
    assert first.changed
    second = ensure_exclude_block(repo)
    assert not second.changed
    text = first.path.read_text()
    assert text.count(START) == 1
    assert text.count(END) == 1
    assert gitignore.read_bytes() == b"node_modules/\n"
    assert git(repo, "status", "--porcelain") == "?? .gitignore"  # only our own fixture file


def test_git_ignores_runtime_state_and_add_all_stages_none_of_it(repo: Path) -> None:
    ensure_exclude_block(repo)
    make_runtime_state(repo)
    (repo / ".agents" / "agents.yaml").write_text("version: 1\n")  # a normal input stays visible
    status = git(repo, "status", "--porcelain", "-uall")
    assert status == "?? .agents/agents.yaml"
    git(repo, "add", "-A")
    staged = git(repo, "diff", "--cached", "--name-only")
    assert staged == ".agents/agents.yaml"


def test_other_exclude_lines_are_preserved_in_place(repo: Path) -> None:
    path = exclude_path(repo)
    path.write_text("# mine\n*.log\n")
    ensure_exclude_block(repo)
    text = path.read_text()
    assert text.startswith("# mine\n*.log\n" + START)
    path.write_text(text + "*.tmp\n")  # user adds lines after the block
    ensure_exclude_block(repo)
    assert path.read_text().endswith(END + "\n*.tmp\n")
    assert "*.log" in path.read_text()


def test_missing_trailing_newline_and_empty_file(repo: Path) -> None:
    path = exclude_path(repo)
    path.write_text("*.log")  # no trailing newline
    ensure_exclude_block(repo)
    assert path.read_text().startswith("*.log\n" + START)
    path.write_text("")
    ensure_exclude_block(repo)
    assert path.read_text().count(START) == 1


def test_outdated_block_is_replaced_in_its_position(repo: Path) -> None:
    path = exclude_path(repo)
    path.write_text(f"before\n{START}\n/.agents/runs/\n{END}\nafter\n")
    assert check_exclude_block(repo)[0] == "outdated"
    assert ensure_exclude_block(repo).changed
    text = path.read_text()
    assert text.startswith("before\n" + START)
    assert text.endswith(END + "\nafter\n")
    assert "/.agents/apply/" in text
    assert check_exclude_block(repo)[0] == "current"


def test_duplicate_blocks_collapse_to_one(repo: Path) -> None:
    path = exclude_path(repo)
    one = "\n".join(block_lines()) + "\n"
    path.write_text(f"a\n{one}b\n{one}c\n")
    ensure_exclude_block(repo)
    text = path.read_text()
    assert text.count(START) == 1
    assert [line for line in text.splitlines() if line in ("a", "b", "c")] == ["a", "b", "c"]


def test_crlf_files_keep_crlf(repo: Path) -> None:
    path = exclude_path(repo)
    path.write_bytes(b"*.log\r\n")
    ensure_exclude_block(repo)
    data = path.read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n")  # no bare LF mixed in
    assert not ensure_exclude_block(repo).changed


@pytest.mark.parametrize(
    "content",
    [f"{START}\n/.agents/runs/\n", f"{END}\n", f"{START}\n{START}\n{END}\n"],
)
def test_malformed_blocks_are_reported_not_guessed(repo: Path, content: str) -> None:
    exclude_path(repo).write_text(content)
    assert check_exclude_block(repo)[0] == "malformed"
    with pytest.raises(ExcludeError):
        ensure_exclude_block(repo)
    assert exclude_path(repo).read_text() == content  # untouched


def test_linked_worktrees_share_the_one_block(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "other", str(linked))
    assert exclude_path(linked) == exclude_path(repo)
    result = ensure_exclude_block(linked)  # run from the linked worktree
    assert result.path == exclude_path(repo)
    assert not ensure_exclude_block(repo).changed
    make_runtime_state(linked)
    assert git(linked, "status", "--porcelain", "-uall") == ""


def test_nested_worktrees_under_agents_are_not_offered_for_staging(repo: Path) -> None:
    ensure_exclude_block(repo)
    nested = repo / ".agents" / "worktrees" / "r1" / "n1"
    git(repo, "worktree", "add", "-q", "-b", "relay-n1", str(nested))
    (nested / "change.txt").write_text("x\n")
    assert git(repo, "status", "--porcelain", "-uall") == ""
    git(repo, "add", "-A")
    assert git(repo, "diff", "--cached", "--name-only") == ""


def test_creates_the_info_directory_when_the_template_lacks_it(tmp_path: Path) -> None:
    bare_template = tmp_path / "template"
    bare_template.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main", f"--template={bare_template}")
    assert not (repo / ".git" / "info").exists()
    ensure_exclude_block(repo)
    assert (repo / ".git" / "info" / "exclude").read_text().count(START) == 1


def test_not_a_repository(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ExcludeError, match="not inside a git repository"):
        ensure_exclude_block(plain)
    assert check_exclude_block(plain)[0] == "no-repository"


def test_check_states(repo: Path) -> None:
    assert check_exclude_block(repo)[0] == "missing"
    ensure_exclude_block(repo)
    assert check_exclude_block(repo)[0] == "current"
