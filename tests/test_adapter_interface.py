"""Adapter interface: `resolveCommand`, and the stdin / prompt-file invocation path (task 1.7)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from relay.adapters.base import CommandNotFound, Invocation, resolveCommand, run_invocation
from relay.core.platform.proc import (
    _wrap_cmd_shim,  # pyright: ignore[reportPrivateUsage]
    resolve_command,
    run_process,
)

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


@pytest.mark.skipif(WINDOWS, reason="POSIX: executability is not part of Windows resolution")
def test_resolve_command_skips_a_non_executable_path_entry(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    # An earlier PATH entry has a non-executable file with the right name; resolution must
    # keep searching later entries instead of failing at the first hit.
    (first / "mytool").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script = second / "mytool"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    env = {**os.environ, "PATH": os.pathsep.join([str(first), str(second)])}
    assert resolveCommand("mytool", ["--flag"], env=env) == [str(script), "--flag"]


# --- resolveCommand: Windows PATHEXT and shims (skip cleanly on Linux) ------------------------


@pytest.mark.skipif(not WINDOWS, reason="PATHEXT lookup is Windows-only")
def test_resolve_command_uses_pathext(tmp_path: Path) -> None:
    exe = tmp_path / "tool.EXE"
    exe.write_text("dummy", encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}
    assert resolveCommand("tool", env=env) == [str(exe)]


def test_cmd_shim_raw_command_line_has_no_double_quote_escaping() -> None:
    """The raw command line is a single string with the `cmd /s` quote pair unescaped.

    A second `list2cmdline` pass (the bug) would escape that pair as `\"`, which
    `cmd.exe /s` cannot strip. `_wrap_cmd_shim` runs on every OS because it only builds a
    string, so the risky construction is unit-tested here without needing Windows.
    """
    shim = r"tools\agent.cmd"
    resolved = _wrap_cmd_shim(shim, ["-p", "hello world"], {"COMSPEC": "cmd.exe"})
    raw = resolved.windows_command_line
    assert raw is not None
    assert '\\"' not in raw
    assert raw == 'cmd.exe /d /s /v:off /c "tools\\agent.cmd ^^^"-p^^^" ^^^"hello^^^ world^^^""'
    assert resolved.argv == (
        "cmd.exe",
        "/d",
        "/s",
        "/v:off",
        "/c",
        '"tools\\agent.cmd ^^^"-p^^^" ^^^"hello^^^ world^^^""',
    )


def test_cmd_shim_raw_command_line_preserves_embedded_quotes() -> None:
    shim = r"tools\agent.cmd"
    resolved = _wrap_cmd_shim(shim, ["-p", 'hello "world"'], {"COMSPEC": "cmd.exe"})
    raw = resolved.windows_command_line
    assert raw is not None
    assert r'\^^^"world\^^^"' in raw


@pytest.mark.skipif(not WINDOWS, reason=".cmd shims are Windows-only")
def test_resolve_command_wraps_a_cmd_shim(tmp_path: Path) -> None:
    shim = tmp_path / "agent.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".CMD"}
    argv = resolveCommand("agent", ["-p", "hello world"], env=env)
    assert argv[0] == env.get("COMSPEC", "cmd.exe")
    assert argv[1:5] == ["/d", "/s", "/v:off", "/c"]
    wrapped = argv[5]
    assert "agent.cmd" in wrapped.lower()
    assert "hello^" in wrapped
    resolved = resolve_command("agent", ["-p", "hello world"], env=env)
    assert resolved.windows_command_line is not None
    # `argv[5]` is the inner line wrapped in one extra quote pair; the raw command line is
    # that pair around the inner line only, unescaped by any second quoting pass.
    assert resolved.windows_command_line == (
        f'{env.get("COMSPEC", "cmd.exe")} /d /s /v:off /c "{wrapped[1:-1]}"'
    )


@pytest.mark.skipif(not WINDOWS, reason=".bat shims are Windows-only")
def test_resolve_command_wraps_a_bat_shim(tmp_path: Path) -> None:
    shim = tmp_path / "runme.bat"
    shim.write_text("@echo off\n", encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".BAT"}
    argv = resolveCommand("runme", env=env)
    assert argv[0] == env.get("COMSPEC", "cmd.exe")
    assert argv[1:5] == ["/d", "/s", "/v:off", "/c"]
    assert "runme.bat" in argv[5].lower()


@pytest.mark.skipif(not WINDOWS, reason="the canary runs a real .cmd shim on Windows only")
def test_cmd_shim_canary_round_trips_quotes_and_newlines(tmp_path: Path) -> None:
    import json

    script = tmp_path / "echo.py"
    script.write_text(
        "import json, sys; print(json.dumps([sys.argv[1:], sys.stdin.read()]))",
        encoding="utf-8",
    )
    shim = tmp_path / "echo.cmd"
    shim.write_text(f'@echo off\r\n"{PY}" "{script}" %*\r\n', encoding="utf-8")
    env = {**os.environ, "PATH": str(tmp_path), "PATHEXT": ".CMD"}
    args = ['a"b', "c d", "safe&echo.INJECTED", "%COMSPEC%", "!COMSPEC!", "a^b", "", "end\\"]
    prompt = 'a"b\nc d'
    resolved = resolve_command("echo", args, env=env)
    result = run_process(resolved, cwd=tmp_path, env=env, stdin=prompt)
    assert result.exit_code == 0
    assert json.loads(result.stdout) == [args, prompt]


@pytest.mark.skipif(not WINDOWS, reason="batch argument validation is Windows-only")
def test_cmd_shim_rejects_newline_arguments(tmp_path: Path) -> None:
    shim = tmp_path / "echo.cmd"
    shim.write_text("@echo off\n", encoding="utf-8")
    with pytest.raises(ValueError, match="use stdin or a file"):
        resolve_command(str(shim), ["safe\necho INJECTED"])


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
