"""Verification policy tests (design section 10, task 1.16).

Every test builds a real temp git repository, creates a node worktree, captures the delta and
runs the Verifier against the candidate checkout. Acceptance commands are local Python scripts
run via ``sys.executable`` so no network or external tool is required.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import pytest

from harness import git, init_repo
from relay.core.capture import Change, capture_changes
from relay.core.specs.task import Commands, RunCheck, ScopeCheck, TaskSpec
from relay.core.verify import (
    NoExecutableChecksError,
    effective_scope,
    executable_commands,
    path_in_scope,
    scope_violations,
    split_command,
    verify,
)

# --- helpers -----------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def py_cmd(script: Path, *args: str) -> str:
    """A command string running `script` with the current interpreter (no shell)."""
    return shlex.join([sys.executable, str(script), *args])


def script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def ok_script(tmp_path: Path) -> Path:
    return script(tmp_path, "ok.py", "print('ok')\n")


def fail_script(tmp_path: Path) -> Path:
    return script(
        tmp_path,
        "fail.py",
        "import sys\nprint('boom', file=sys.stderr)\nprint('out')\nsys.exit(1)\n",
    )


def worktree(repo: Path, tmp_path: Path, name: str = "node") -> tuple[Path, str]:
    head = git(repo, "rev-parse", "HEAD")
    wt = tmp_path / name
    git(repo, "worktree", "add", "--detach", str(wt), head)
    return wt, head


def capture(repo: Path, wt: Path, head: str, tmp_path: Path, name: str = "candidate"):
    return capture_changes(wt, head, candidate_dir=tmp_path / name)


def make_task(**kwargs: object) -> TaskSpec:
    data: dict[str, object] = {
        "id": "n1",
        "run_id": "2026-10-22-test",
        "category": "implement",
        "goal": "do the thing",
    }
    data.update(kwargs)
    return TaskSpec.model_validate(data)


# --- default mode: executable resolution -------------------------------------------------------


def test_executable_commands_prefer_acceptance_over_commands(tmp_path: Path) -> None:
    ok = ok_script(tmp_path)
    task = make_task(
        acceptance=[RunCheck(run=py_cmd(ok))],
        commands=Commands(build="true", test="true"),
    )
    commands = executable_commands(task)
    assert [c.label for c in commands] == [py_cmd(ok)]
    assert commands[0].argv == (sys.executable, str(ok))


def test_executable_commands_fall_back_to_build_test_lint(tmp_path: Path) -> None:
    ok = ok_script(tmp_path)
    task = make_task(commands=Commands(build=py_cmd(ok), test=py_cmd(ok), lint=None))
    commands = executable_commands(task)
    assert [c.label for c in commands] == ["commands.build", "commands.test"]


def test_executable_commands_ignore_scope_entries(tmp_path: Path) -> None:
    task = make_task(acceptance=[ScopeCheck(diff_scope=["src/**"])], commands=Commands(test="true"))
    commands = executable_commands(task)
    # A scope check never counts as an executable check; the default command still applies.
    assert [c.label for c in commands] == ["commands.test"]


def test_no_executable_checks_is_rejected(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    with pytest.raises(NoExecutableChecksError):
        verify(cap, make_task())


# --- default mode: running commands ------------------------------------------------------------


def test_default_mode_runs_build_test_lint(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    ok = ok_script(tmp_path)
    task = make_task(commands=Commands(build=py_cmd(ok), test=py_cmd(ok), lint=None))
    result = verify(cap, task)
    assert result.outcome == "passed"
    assert result.publication_allowed
    assert [c.name for c in result.checks] == [
        "scope",
        "commands.build",
        "commands.test",
        "candidate-unchanged",
    ]


def test_passing_acceptance(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(acceptance=[RunCheck(run=py_cmd(ok_script(tmp_path)))])
    result = verify(cap, task)
    assert result.outcome == "passed"
    assert result.candidate_unchanged
    command = result.checks[1]
    assert command.kind == "command"
    assert command.status == "passed"
    assert command.exit_code == 0
    assert "ok" in command.stdout


def test_failing_acceptance_captures_exit_and_output(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(acceptance=[RunCheck(run=py_cmd(fail_script(tmp_path)))])
    result = verify(cap, task)
    assert result.outcome == "failed"
    assert not result.publication_allowed
    command = result.checks[1]
    assert command.status == "failed"
    assert command.exit_code == 1
    assert "out" in command.stdout
    assert "boom" in command.stderr


def test_acceptance_runs_in_candidate_worktree(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "feature.txt").write_text("f\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    check = script(
        tmp_path,
        "check.py",
        "import sys\n"
        "from pathlib import Path\n"
        "sys.exit(0 if Path('feature.txt').is_file() else 1)\n",
    )
    task = make_task(acceptance=[RunCheck(run=py_cmd(check))])
    assert verify(cap, task).outcome == "passed"


def test_expected_output_substring(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    ok = ok_script(tmp_path)
    passed = make_task(acceptance=[RunCheck(run=py_cmd(ok), expect_output="ok")])
    assert verify(cap, passed).outcome == "passed"
    missing = make_task(acceptance=[RunCheck(run=py_cmd(ok), expect_output="absent")])
    assert verify(cap, missing).outcome == "failed"


def test_nonzero_expected_exit(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    fail = fail_script(tmp_path)
    task = make_task(acceptance=[RunCheck(run=py_cmd(fail), expect_exit=1)])
    assert verify(cap, task).outcome == "passed"


def test_command_not_found_is_a_failure(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(acceptance=[RunCheck(run="relay-definitely-not-a-command-xyz")])
    result = verify(cap, task)
    assert result.outcome == "failed"
    assert result.checks[1].status == "failed"


# --- timeouts ----------------------------------------------------------------------------------


def test_per_command_timeout(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    sleeper = script(tmp_path, "sleep.py", "import time\ntime.sleep(5)\n")
    task = make_task(acceptance=[RunCheck(run=py_cmd(sleeper), timeout_s=1)])
    result = verify(cap, task)
    assert result.outcome == "failed"
    assert result.timed_out
    command = result.checks[1]
    assert command.status == "timed_out"
    assert command.timed_out
    assert command.exit_code is None


def test_overall_timeout(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    sleeper = script(tmp_path, "sleep.py", "import time\ntime.sleep(5)\n")
    task = make_task(acceptance=[RunCheck(run=py_cmd(sleeper))])
    result = verify(cap, task, overall_timeout_s=1.0)
    assert result.outcome == "failed"
    assert result.timed_out
    assert result.checks[1].status == "timed_out"


# --- scope: owns vs diff_scope -----------------------------------------------------------------


def test_effective_scope_precedence() -> None:
    scoped = make_task(
        acceptance=[ScopeCheck(diff_scope=["src/**"])],
        owns=["src/owned.py"],
    )
    assert effective_scope(scoped) == ("src/**",)
    owned_only = make_task(owns=["src/**"])
    assert effective_scope(owned_only) == ("src/**",)
    unrestricted = make_task()
    assert effective_scope(unrestricted) is None


def test_change_outside_diff_scope_fails(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "x.py").write_text("x\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(
        acceptance=[
            RunCheck(run=py_cmd(ok_script(tmp_path))),
            ScopeCheck(diff_scope=["tests/**"]),
        ],
    )
    result = verify(cap, task)
    assert result.outcome == "failed"
    assert result.failed_paths == ("src/x.py",)
    assert result.checks[0].status == "failed"


def test_change_inside_diff_scope_outside_owns_passes(repo: Path, tmp_path: Path) -> None:
    """diff_scope is the acceptance constraint; owns is only a planning constraint."""
    wt, head = worktree(repo, tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "other.py").write_text("o\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(
        acceptance=[
            RunCheck(run=py_cmd(ok_script(tmp_path))),
            ScopeCheck(diff_scope=["src/**"]),
        ],
        owns=["src/owned.py"],
    )
    result = verify(cap, task)
    assert result.outcome == "passed"
    assert result.failed_paths == ()


def test_allowed_diff_scope_passes(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "y.py").write_text("y\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(
        acceptance=[
            RunCheck(run=py_cmd(ok_script(tmp_path))),
            ScopeCheck(diff_scope=["src/**"]),
        ],
    )
    assert verify(cap, task).outcome == "passed"


def test_owns_used_as_default_scope(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "z.py").write_text("z\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(
        acceptance=[RunCheck(run=py_cmd(ok_script(tmp_path)))],
        owns=["src/**"],
    )
    assert verify(cap, task).outcome == "passed"

    outside = make_task(
        acceptance=[RunCheck(run=py_cmd(ok_script(tmp_path)))],
        owns=["other/**"],
    )
    result = verify(cap, outside)
    assert result.outcome == "failed"
    assert result.failed_paths == ("src/z.py",)


def test_no_scope_restricts_to_whole_repo(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "anywhere.txt").write_text("a\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(acceptance=[RunCheck(run=py_cmd(ok_script(tmp_path)))])
    assert verify(cap, task).outcome == "passed"


# --- scope unit coverage: deleted, renamed, binary, directory prefixes ------------------------


def test_scope_violations_cover_rename_both_paths() -> None:
    renamed = [Change(path="src/b.txt", status="renamed", old_path="src/a.txt")]
    assert scope_violations(renamed, ("src/**",)) == ()
    assert scope_violations(renamed, ("src/b.txt",)) == ("src/a.txt",)


def test_scope_violations_cover_deleted_paths() -> None:
    deleted = [Change(path="src/gone.txt", status="deleted")]
    assert scope_violations(deleted, ("src/**",)) == ()
    assert scope_violations(deleted, ("other/**",)) == ("src/gone.txt",)


def test_scope_violations_cover_binary_paths() -> None:
    binary = [Change(path="bin/blob.bin", status="added", binary=True)]
    assert scope_violations(binary, ("bin/**",)) == ()
    assert scope_violations(binary, ("text/**",)) == ("bin/blob.bin",)


def test_path_in_scope_directory_prefix_and_globs() -> None:
    assert path_in_scope("src/editor/autosave.ts", "src/editor")
    assert path_in_scope("src/editor/autosave.ts", "src/editor/**")
    assert path_in_scope("src/editor/autosave.ts", "src/**")
    assert not path_in_scope("tests/x.ts", "src/**")
    assert path_in_scope("src/editor/autosave.ts", "src/editor/autosave.ts")
    assert not path_in_scope("src/editor/autosave.ts", "src/editor/other.ts")


# --- explicit skip (verify: none) -------------------------------------------------------------


def test_verify_none_skips_commands_and_records_reason(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    # A failing command that is never run because commands are skipped.
    task = make_task(
        verify="none",
        verification_skip_reason="docs-only change",
        acceptance=[RunCheck(run=py_cmd(fail_script(tmp_path)))],
    )
    result = verify(cap, task)
    assert result.outcome == "skipped"
    assert result.publication_allowed
    assert result.skip_reason == "docs-only change"
    assert result.checks[1].status == "skipped"
    assert "docs-only change" in result.checks[1].details[0]


def test_verify_none_scope_still_applies(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "src").mkdir()
    (wt / "src" / "x.py").write_text("x\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    task = make_task(
        verify="none",
        verification_skip_reason="skip commands",
        acceptance=[ScopeCheck(diff_scope=["tests/**"])],
    )
    result = verify(cap, task)
    assert result.outcome == "failed"
    assert result.failed_paths == ("src/x.py",)


# --- candidate-unchanged check -----------------------------------------------------------------


def test_clean_candidate_remains_unchanged(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    result = verify(cap, make_task(acceptance=[RunCheck(run=py_cmd(ok_script(tmp_path)))]))
    assert result.candidate_unchanged
    mutation = result.checks[-1]
    assert mutation.kind == "mutation"
    assert mutation.status == "passed"


def test_candidate_mutation_is_a_failure(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    mutator = script(
        tmp_path,
        "mutate.py",
        "from pathlib import Path\nPath('README.md').write_text('mutated\\n')\n",
    )
    result = verify(cap, make_task(acceptance=[RunCheck(run=py_cmd(mutator))]))
    assert result.outcome == "failed"
    assert not result.candidate_unchanged
    assert not result.publication_allowed
    mutation = result.checks[-1]
    assert mutation.kind == "mutation"
    assert mutation.status == "failed"
    assert any("README.md" in detail for detail in mutation.details)


def test_untracked_verification_output_is_not_a_mutation(repo: Path, tmp_path: Path) -> None:
    """Build artifacts created during verification are not part of the deliverable."""
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    generator = script(
        tmp_path,
        "gen.py",
        "from pathlib import Path\nPath('build-output.txt').write_text('artifact\\n')\n",
    )
    result = verify(cap, make_task(acceptance=[RunCheck(run=py_cmd(generator))]))
    assert result.outcome == "passed"
    assert result.candidate_unchanged


# --- deterministic ordering --------------------------------------------------------------------


def test_checks_are_deterministically_ordered(repo: Path, tmp_path: Path) -> None:
    wt, head = worktree(repo, tmp_path)
    (wt / "new.txt").write_text("n\n", encoding="utf-8", newline="\n")
    cap = capture(repo, wt, head, tmp_path)
    ok = ok_script(tmp_path)
    task = make_task(
        acceptance=[
            RunCheck(run=py_cmd(ok)),
            ScopeCheck(diff_scope=["**"]),
            RunCheck(run=py_cmd(ok), expect_output="ok"),
        ],
    )
    result = verify(cap, task)
    assert [c.name for c in result.checks] == [
        "scope",
        py_cmd(ok),
        py_cmd(ok),
        "candidate-unchanged",
    ]


# --- command splitting (F1: platform-correct, testable on POSIX) -------------------------------


def test_split_windows_backslash_path_is_literal() -> None:
    # Backslashes are not escapes on Windows, so a bare path survives intact.
    assert split_command(r"C:\tools\check.cmd --flag", windows=True) == [
        r"C:\tools\check.cmd",
        "--flag",
    ]


def test_split_windows_quoted_arg_with_space() -> None:
    assert split_command(r'.\scripts\run.bat "arg with space"', windows=True) == [
        r".\scripts\run.bat",
        "arg with space",
    ]


def test_split_windows_quoted_path_with_trailing_backslash() -> None:
    # An even run of backslashes before the closing quote collapses to half and the quote
    # closes the argument (CommandLineToArgvW rules).
    assert split_command(r'"C:\Program Files\\" --x', windows=True) == [
        "C:\\Program Files\\",
        "--x",
    ]


def test_split_windows_empty_args_are_preserved() -> None:
    assert split_command('cmd "" ""', windows=True) == ["cmd", "", ""]


def test_split_windows_double_quote_escape_and_single_quote_is_ordinary() -> None:
    # `""` inside quotes is a literal quote; single quotes group nothing on Windows.
    assert split_command(r'cmd "say ""hi""" --x', windows=True) == ["cmd", 'say "hi"', "--x"]
    assert split_command(r".\run.bat 'a b'", windows=True) == [r".\run.bat", "'a", "b'"]


def test_split_posix_matches_shlex() -> None:
    assert split_command("echo 'a b' \"c d\"") == ["echo", "a b", "c d"]


def test_split_posix_treats_backslash_as_escape() -> None:
    assert split_command(r"echo a\ b") == ["echo", "a b"]
    assert split_command(r"echo a\ b", windows=False) == ["echo", "a b"]


def test_split_windows_result_differs_from_shlex() -> None:
    # The same string parses differently: shlex eats the backslashes, the Windows parser
    # keeps them (this is exactly the bug the splitter fixes).
    assert shlex.split(r"C:\tools\check.cmd --flag") == ["C:toolscheck.cmd", "--flag"]
    assert split_command(r"C:\tools\check.cmd --flag", windows=True) == [
        r"C:\tools\check.cmd",
        "--flag",
    ]


def test_split_empty_command_has_no_executable() -> None:
    assert split_command("") == []
    assert split_command("   ") == []


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing integration")
def test_split_command_default_uses_windows_rules_on_windows() -> None:
    # On a real Windows host the default parser is the CommandLineToArgvW tokenizer.
    assert split_command(r".\scripts\run.bat 'arg with space'") == [
        r".\scripts\run.bat",
        "'arg",
        "with",
        "space'",
    ]
