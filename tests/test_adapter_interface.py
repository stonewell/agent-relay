"""Adapter interface: `resolveCommand`, and the stdin / prompt-file invocation path (task 1.7)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from relay.adapters.base import CommandNotFound, Invocation, resolveCommand, run_invocation

WINDOWS = sys.platform == "win32"
PY = sys.executable


# --- resolveCommand: explicit paths, PATH, missing --------------------------------------------


def test_resolve_command_returns_an_explicit_path() -> None:
    assert resolveCommand(sys.executable, ["-V"]) == [sys.executable, "-V"]


def test_resolve_command_finds_an_executable_on_path(tmp_path: Path) -> None:
    script = tmp_path / "mytool"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path)}
    assert resolveCommand("mytool", ["--flag"], env=env) == [str(script), "--flag"]


def test_resolve_command_missing_executable_raises(tmp_path: Path) -> None:
    with pytest.raises(CommandNotFound, match="not found"):
        resolveCommand("no-such-command-9f3a", env={**os.environ, "PATH": str(tmp_path)})


def test_resolve_command_rejects_an_empty_command() -> None:
    with pytest.raises(CommandNotFound):
        resolveCommand("")


@pytest.mark.skipif(WINDOWS, reason="POSIX: a non-executable file is not a command")
def test_resolve_command_rejects_a_non_executable_file(tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("not a program", encoding="utf-8")
    with pytest.raises(CommandNotFound, match="not executable"):
        resolveCommand(str(plain))


# --- resolveCommand: Windows PATHEXT and shims (skip cleanly on Linux) ------------------------


@pytest.mark.skipif(not WINDOWS, reason="PATHEXT lookup is Windows-only")
def test_resolve_command_uses_pathext(tmp_path: Path) -> None:
    exe = tmp_path / "tool.EXE"
    exe.write_text("dummy", encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
    assert resolveCommand("tool", env=env) == [str(exe)]


@pytest.mark.skipif(not WINDOWS, reason=".cmd shims are Windows-only")
def test_resolve_command_wraps_a_cmd_shim(tmp_path: Path) -> None:
    shim = tmp_path / "agent.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".CMD"}
    argv = resolveCommand("agent", ["-p", "hello world"], env=env)
    assert argv[0] == env.get("COMSPEC", "cmd.exe")
    assert argv[1:4] == ["/d", "/s", "/c"]
    wrapped = argv[3]
    assert str(shim) in wrapped
    assert "hello world" in wrapped


@pytest.mark.skipif(not WINDOWS, reason=".bat shims are Windows-only")
def test_resolve_command_wraps_a_bat_shim(tmp_path: Path) -> None:
    shim = tmp_path / "runme.bat"
    shim.write_text("@echo off\n", encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".BAT"}
    argv = resolveCommand("runme", env=env)
    assert argv[0] == env.get("COMSPEC", "cmd.exe")
    assert argv[1:4] == ["/d", "/s", "/c"]
    assert str(shim) in argv[3]


# --- the invocation path: prompt on stdin or a file -------------------------------------------


def test_invocation_stdin_round_trips_quotes_and_newlines() -> None:
    prompt = 'line one\nline "two"\n\n'
    invocation = Invocation(
        cmd=PY,
        args=("-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"),
        cwd=Path.cwd(),
        stdin=prompt,
    )
    result = run_invocation(invocation)
    assert result.exit_code == 0
    assert result.stdout == repr(prompt)


def test_invocation_prompt_file_round_trips_exactly(tmp_path: Path) -> None:
    prompt = 'first\nline "two"\n'
    prompt_path = tmp_path / "prompt.txt"
    script = "import sys; sys.stdout.write(open(sys.argv[1], encoding='utf-8').read())"
    invocation = Invocation(
        cmd=PY,
        args=("-c", script, str(prompt_path)),
        cwd=tmp_path,
        stdin=prompt,
        prompt_file=prompt_path,
    )
    result = run_invocation(invocation)
    assert result.exit_code == 0
    assert result.stdout == prompt
    # The file on disk keeps the exact bytes (no newline translation).
    assert prompt_path.read_bytes() == prompt.encode("utf-8")


def test_invocation_prompt_file_replaces_stdin(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.txt"
    invocation = Invocation(
        cmd=PY,
        args=("-c", "import sys; sys.stdout.write(repr(sys.stdin.read()))"),
        cwd=tmp_path,
        stdin="SECRET-PROMPT",
        prompt_file=prompt_path,
    )
    result = run_invocation(invocation)
    assert result.exit_code == 0
    assert result.stdout == repr("")  # stdin was closed; the prompt went to the file


def test_invocation_prompt_file_requires_the_prompt_text(tmp_path: Path) -> None:
    invocation = Invocation(
        cmd=PY,
        args=("-c", "pass"),
        cwd=tmp_path,
        prompt_file=tmp_path / "prompt.txt",
    )
    with pytest.raises(ValueError, match="prompt_file requires"):
        run_invocation(invocation)


def test_invocation_is_frozen_with_a_prompt_file(tmp_path: Path) -> None:
    inv = Invocation(cmd="x", args=(), cwd=tmp_path, stdin="p", prompt_file=tmp_path / "p.txt")
    with pytest.raises(AttributeError):
        inv.prompt_file = None  # type: ignore[misc]
