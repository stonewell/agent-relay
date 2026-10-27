"""TUI entry points and headless compatibility (task 2.28; design 14.1, 15)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import init_repo
from relay.cli.commands import tui as tui_command
from relay.cli.main import build_parser, main
from relay.core.sessions import SessionStore


def _never(repo_root: Path, session_id: str | None) -> int:
    pytest.fail("must not launch the TUI")


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str | None]]:
    """Record TUI launches instead of starting Textual; the terminal counts as interactive."""
    seen: list[tuple[Path, str | None]] = []

    def launch(repo_root: Path, session_id: str | None) -> int:
        seen.append((repo_root, session_id))
        return 0

    monkeypatch.setattr(tui_command, "launch", launch)
    monkeypatch.setattr(tui_command, "interactive", lambda: True)
    return seen


def test_bare_relay_on_a_terminal_opens_the_tui(
    tmp_path: Path, launches: list[tuple[Path, str | None]]
) -> None:
    repo = init_repo(tmp_path / "repo")
    assert main(["-C", str(repo)]) == 0
    assert launches == [(repo.resolve(), None)]


def test_bare_relay_without_a_terminal_keeps_the_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tui_command, "interactive", lambda: False)
    monkeypatch.setattr(tui_command, "launch", _never)
    assert main([]) == 2
    err = capsys.readouterr().err
    assert "usage: relay" in err and "choose a command" in err


def test_bare_relay_json_is_unchanged_even_on_a_terminal(
    launches: list[tuple[Path, str | None]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:  # unchanged: `--json` is not a top-level option
        main(["--json"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]
    assert launches == []


def test_explicit_tui_and_repository_selection(
    tmp_path: Path, launches: list[tuple[Path, str | None]]
) -> None:
    repo = init_repo(tmp_path / "repo")
    assert main(["-C", str(repo), "tui"]) == 0
    assert launches == [(repo.resolve(), None)]


def test_explicit_tui_without_a_terminal_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tui_command, "interactive", lambda: False)
    monkeypatch.setattr(tui_command, "launch", _never)
    assert main(["-C", str(tmp_path), "tui"]) == 2
    assert "needs a terminal" in capsys.readouterr().err
    assert main(["-C", str(tmp_path), "resume", "--session", "2026-10-27-s1234"]) == 2


def test_tui_has_no_json_mode(
    launches: list[tuple[Path, str | None]], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:  # `tui` does not accept --json at all
        main(["tui", "--json"])
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]
    assert launches == []


def test_reopen_a_saved_session_with_tui_or_resume(
    tmp_path: Path, launches: list[tuple[Path, str | None]]
) -> None:
    repo = init_repo(tmp_path / "repo")
    sid = SessionStore.create(repo).session_id
    assert main(["-C", str(repo), "tui", "--session", sid]) == 0
    assert main(["-C", str(repo), "resume", "--session", sid]) == 0
    assert launches == [(repo.resolve(), sid), (repo.resolve(), sid)]


def test_unknown_session_is_invalid_input(
    tmp_path: Path, launches: list[tuple[Path, str | None]], capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    assert main(["-C", str(repo), "tui", "--session", "2026-10-27-s0000"]) == 2
    assert "not found" in capsys.readouterr().err
    assert launches == []


def test_resume_run_and_session_are_mutually_exclusive(
    tmp_path: Path, launches: list[tuple[Path, str | None]], capsys: pytest.CaptureFixture[str]
) -> None:
    repo = init_repo(tmp_path / "repo")
    sid = SessionStore.create(repo).session_id
    assert main(["-C", str(repo), "resume", "2026-10-27-abcd", "--session", sid]) == 2
    assert "either a run or --session" in capsys.readouterr().err
    assert main(["-C", str(repo), "resume", "2026-10-27-abcd", "--session", sid, "--json"]) == 2
    assert "either a run or --session" in json.loads(capsys.readouterr().err)["error"]
    assert main(["-C", str(repo), "resume"]) == 2
    assert "give a run" in capsys.readouterr().err
    assert launches == []


def test_resume_of_a_run_keeps_its_headless_behaviour(
    tmp_path: Path, launches: list[tuple[Path, str | None]], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["-C", str(tmp_path), "resume", "2026-10-23-abcd", "--json"]) == 2
    assert "not found" in json.loads(capsys.readouterr().err)["error"]
    assert launches == []


def test_help_lists_the_tui() -> None:
    assert "tui" in build_parser().format_help()


HEADLESS = r"""
import contextlib, io, json, sys
from relay.cli.main import main

repo = sys.argv[1]
for argv in (["--version"], ["--help"], ["tui", "--help"], ["status", "--json"],
             ["-C", repo, "status", "--json"], ["-C", repo, "routes", "--json"],
             ["-C", repo, "doctor", "--check", "python", "--json"], ["--json"], [],
             ["-C", repo, "resume", "2026-10-23-abcd", "--json"],
             ["-C", repo, "tui"]):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            main(argv)
        except SystemExit:
            pass
print(json.dumps(sorted(m for m in sys.modules if m == "textual" or m.startswith("textual."))))
"""


def test_headless_commands_never_import_textual(tmp_path: Path) -> None:
    """Help, version, JSON output, headless commands and a non-terminal `tui` stay Textual-free."""
    repo = init_repo(tmp_path / "repo")
    proc = subprocess.run(
        [sys.executable, "-c", HEADLESS, str(repo)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
