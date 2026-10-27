"""Permission canaries (task 1.20 part B).

These tests drive :func:`relay.core.doctor.run_permission_canary` and the `paid` doctor
checks it backs, using fake CLI executables that emulate an enforced profile, a silent
bypass, a missing command or a hang. No real CLI, network, credentials or paid call is used;
each canary runs in a throwaway directory and is removed afterwards.

The classification contract under test (design section 9.3):

- ``enforced``: the forbidden mutation was denied while the permitted operation succeeded.
- ``bypassed``: the forbidden mutation succeeded (the profile does not enforce).
- ``inconclusive``: neither outcome could be established (e.g. the agent did nothing).
- ``unavailable``: the command could not be resolved (CLI not installed).
- ``error``: the canary itself failed or timed out.

The child's exit code is never evidence; only the marker/outside files and the echoed token
decide the outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from relay.cli.main import main
from relay.core import registry as registry_module
from relay.core.doctor import (
    PERMISSION_PROFILES,
    DoctorContext,
    PermissionProfile,
    default_registry,
    run_checks,
    run_permission_canary,
)


def cli(directory: Path, name: str, sh_body: str, cmd_body: str) -> Path:
    """A fake agent CLI: a shell script on POSIX, a `.cmd` shim on Windows (like Cursor)."""
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(f"@echo off\r\n{cmd_body}\r\n")
    else:
        path = directory / name
        path.write_text(f"#!/bin/sh\n{sh_body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def profile(adapter: str, level: str) -> PermissionProfile:
    for candidate in PERMISSION_PROFILES:
        if candidate.adapter == adapter and candidate.level == level:
            return candidate
    raise AssertionError(f"no profile for {adapter}.{level}")


def isolated(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> None:
    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp / "no-user-config.yaml")


# --- classification via fake CLIs -------------------------------------------------------------


def test_readonly_enforced(tmp_path: Path) -> None:
    fake = cli(tmp_path, "claude-enforced", "cat repo/marker.txt", "type repo\\marker.txt")
    result = run_permission_canary(str(fake), profile("claude", "readonly"), parent=tmp_path)
    assert result.outcome == "enforced"
    assert result.adapter == "claude"
    assert result.level == "readonly"


def test_readonly_bypassed(tmp_path: Path) -> None:
    fake = cli(
        tmp_path,
        "claude-bypass",
        "echo MUTATED > repo/marker.txt",
        "echo MUTATED>repo\\marker.txt",
    )
    result = run_permission_canary(str(fake), profile("claude", "readonly"), parent=tmp_path)
    assert result.outcome == "bypassed"


def test_readonly_cli_state_outside_repo_is_enforced(tmp_path: Path) -> None:
    """A CLI writing its own state in `work/` (outside `work/repo/`) is not a bypass (F2)."""
    fake = cli(
        tmp_path,
        "claude-state",
        "mkdir -p .claude && echo state > .claude/session.txt && cat repo/marker.txt",
        "mkdir .claude 2>nul & echo state>.claude\\session.txt & type repo\\marker.txt",
    )
    result = run_permission_canary(str(fake), profile("claude", "readonly"), parent=tmp_path)
    assert result.outcome == "enforced"


def test_readonly_cli_write_into_repo_is_bypassed(tmp_path: Path) -> None:
    """Creating a new file inside `work/repo/` is a repository mutation and a bypass (F2)."""
    fake = cli(
        tmp_path,
        "claude-repo-write",
        "cat repo/marker.txt; echo EXTRA > repo/new-file.txt",
        "type repo\\marker.txt & echo EXTRA>repo\\new-file.txt",
    )
    result = run_permission_canary(str(fake), profile("claude", "readonly"), parent=tmp_path)
    assert result.outcome == "bypassed"


def test_readonly_inconclusive_when_the_agent_does_nothing(tmp_path: Path) -> None:
    fake = cli(tmp_path, "claude-silent", ":", "rem")
    result = run_permission_canary(str(fake), profile("claude", "readonly"), parent=tmp_path)
    assert result.outcome == "inconclusive"


def test_workspace_enforced(tmp_path: Path) -> None:
    fake = cli(tmp_path, "codex-enforced", "echo OK > ok.txt", "echo OK>ok.txt")
    result = run_permission_canary(str(fake), profile("codex", "workspace"), parent=tmp_path)
    assert result.outcome == "enforced"


def test_workspace_bypassed_via_write_outside_the_workspace(tmp_path: Path) -> None:
    fake = cli(
        tmp_path,
        "codex-bypass",
        "mkdir -p ../outside && echo MUTATED > ../outside/escape.txt",
        "echo MUTATED>..\\outside\\escape.txt",
    )
    result = run_permission_canary(str(fake), profile("codex", "workspace"), parent=tmp_path)
    assert result.outcome == "bypassed"


def test_antigravity_readonly_is_mapped_via_mode_plan(tmp_path: Path) -> None:
    fake = cli(tmp_path, "agy-enforced", "cat repo/marker.txt", "type repo\\marker.txt")
    result = run_permission_canary(str(fake), profile("agy", "readonly"), parent=tmp_path)
    assert result.outcome == "enforced"
    assert profile("agy", "readonly").flags == ("--mode", "plan")


def test_missing_command_is_unavailable(tmp_path: Path) -> None:
    result = run_permission_canary(
        "definitely-not-installed-xyz", profile("agy", "workspace"), parent=tmp_path
    )
    assert result.outcome == "unavailable"


def test_a_hanging_canary_times_out_as_an_error(tmp_path: Path) -> None:
    fake = cli(tmp_path, "slow-agent", "sleep 5", "ping -n 6 127.0.0.1 >nul")
    result = run_permission_canary(
        str(fake), profile("claude", "readonly"), parent=tmp_path, timeout_s=0.5
    )
    assert result.outcome == "error"
    assert "timed out" in result.note


def test_the_throwaway_directory_is_removed_afterwards(tmp_path: Path) -> None:
    fake = cli(tmp_path, "claude-enforced", "cat repo/marker.txt", "type repo\\marker.txt")
    run_permission_canary(str(fake), profile("claude", "readonly"), parent=tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("relay-canary-")]
    assert leftovers == []


# --- doctor integration ------------------------------------------------------------------------


def test_permission_profiles_register_paid_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    # Cursor is opt-in (disabled in the built-in defaults); its profiles need the agent.
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        "agents:\n  cursor:\n    enabled: true\n", encoding="utf-8"
    )
    registry = default_registry(DoctorContext(repo_root=tmp_path))
    by_id = {c.id: c for c in registry}
    for candidate in PERMISSION_PROFILES:
        check = by_id[f"permission.{candidate.adapter}.{candidate.level}"]
        assert check.paid is True  # runs only with `--paid` or an explicit `--check`
        assert check.requires == ()


def test_default_doctor_does_not_run_paid_permission_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    ctx = DoctorContext(repo_root=tmp_path)
    report = asyncio.run(run_checks(default_registry(ctx), ctx))
    assert not any(c.id.startswith("permission.") for c in report.checks)


def test_naming_a_permission_check_runs_it_without_paid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    fake = cli(tmp_path, "claude-enforced", "cat repo/marker.txt", "type repo\\marker.txt")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        f"agents:\n  claude:\n    cmd: {fake}\n", encoding="utf-8"
    )
    ctx = DoctorContext(repo_root=tmp_path)
    report = asyncio.run(
        run_checks(default_registry(ctx), ctx, only=["permission.claude.readonly"])
    )
    (check,) = report.checks
    assert check.id == "permission.claude.readonly"
    assert check.status == "pass"
    assert check.data["outcome"] == "enforced"


def test_doctor_permission_check_human_and_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    fake = cli(tmp_path, "claude-enforced", "cat repo/marker.txt", "type repo\\marker.txt")
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        f"agents:\n  claude:\n    cmd: {fake}\n", encoding="utf-8"
    )
    code = main(["-C", str(tmp_path), "doctor", "--check", "permission.claude.readonly"])
    out = capsys.readouterr().out
    assert code == 0
    assert "permission.claude.readonly" in out
    assert "readonly" in out
    assert "enforced" in out
    assert "not advertised" in out

    assert (
        main(["-C", str(tmp_path), "doctor", "--check", "permission.claude.readonly", "--json"])
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    (check,) = body["checks"]
    assert check["id"] == "permission.claude.readonly"
    assert check["status"] == "pass"
    assert check["data"] == {
        "adapter": "claude",
        "level": "readonly",
        "mapped_flags": ["--permission-mode", "plan"],
        "advertised": False,
        "outcome": "enforced",
        "note": "enforced in a disposable dir; promote manually via probe().levels",
    }


def test_doctor_permission_check_reports_bypass_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    fake = cli(
        tmp_path,
        "claude-bypass",
        "echo MUTATED > repo/marker.txt",
        "echo MUTATED>repo\\marker.txt",
    )
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        f"agents:\n  claude:\n    cmd: {fake}\n", encoding="utf-8"
    )
    code = main(["-C", str(tmp_path), "doctor", "--check", "permission.claude.readonly"])
    out = capsys.readouterr().out
    assert code == 1
    assert "bypassed" in out
    assert "silent bypass" in out
