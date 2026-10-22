from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from orch.cli.exitcodes import Exit, combine
from orch.cli.main import main
from orch.core import registry as registry_module
from orch.core.doctor import (
    Check,
    CheckRegistry,
    CheckResult,
    DoctorContext,
    agent_cli_check,
    default_registry,
    run_checks,
)


def ctx(tmp: Path) -> DoctorContext:
    return DoctorContext(repo_root=tmp, user_config=None)


def fixed(status: str, summary: str = "s") -> Check:
    async def run(_c: DoctorContext) -> CheckResult:
        return CheckResult(status, summary, remedy="do x")  # type: ignore[arg-type]

    return Check(f"t.{status}", status, run)


def go(registry: CheckRegistry, tmp: Path, **kw: object):  # noqa: ANN201
    return asyncio.run(run_checks(registry, ctx(tmp), **kw))  # type: ignore[arg-type]


# --- runner -----------------------------------------------------------------------------------


def test_runs_checks_in_order_and_counts_statuses(tmp_path: Path) -> None:
    reg = CheckRegistry()
    for status in ("pass", "warn", "unsupported", "pass"):
        reg.register(Check(f"c{len(reg)}", status, fixed(status).run))
    report = go(reg, tmp_path)
    assert [c.id for c in report.checks] == ["c0", "c1", "c2", "c3"]
    assert report.counts() == {"pass": 2, "warn": 1, "fail": 0, "unsupported": 1, "skipped": 0}


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        (["pass", "warn", "unsupported", "skipped"], 0),
        (["pass", "fail"], 1),
    ],
)
def test_only_failures_make_the_exit_code_nonzero(
    tmp_path: Path, statuses: list[str], code: int
) -> None:
    reg = CheckRegistry()
    for i, status in enumerate(statuses):
        reg.register(Check(f"c{i}", status, fixed(status).run))
    assert go(reg, tmp_path).exit_code == code


def test_an_exception_becomes_a_failure_and_does_not_stop_the_run(tmp_path: Path) -> None:
    async def boom(_c: DoctorContext) -> CheckResult:
        raise RuntimeError("kaput")

    reg = CheckRegistry()
    reg.register(Check("bad", "bad", boom))
    reg.register(fixed("pass"))
    report = go(reg, tmp_path)
    assert [c.status for c in report.checks] == ["fail", "pass"]
    assert "RuntimeError: kaput" in report.checks[0].summary
    assert "Traceback" in report.checks[0].data["traceback"]


def test_a_slow_check_times_out_as_a_failure(tmp_path: Path) -> None:
    async def slow(_c: DoctorContext) -> CheckResult:
        await asyncio.sleep(5)
        return CheckResult("pass", "never")

    reg = CheckRegistry()
    reg.register(Check("slow", "slow", slow, timeout_s=0.1))
    report = go(reg, tmp_path)
    assert report.checks[0].status == "fail"
    assert "timed out" in report.checks[0].summary


def test_a_check_is_skipped_when_a_prerequisite_did_not_pass(tmp_path: Path) -> None:
    reg = CheckRegistry()
    reg.register(fixed("fail"))
    reg.register(Check("dep", "dep", fixed("pass").run, requires=("t.fail",)))
    reg.register(Check("orphan", "orphan", fixed("pass").run, requires=("never-ran",)))
    report = go(reg, tmp_path)
    assert [c.status for c in report.checks] == ["fail", "skipped", "skipped"]
    assert "requires t.fail" in report.checks[1].summary


def test_a_warning_or_unsupported_prerequisite_still_lets_dependents_run(tmp_path: Path) -> None:
    reg = CheckRegistry()
    reg.register(fixed("warn"))
    reg.register(Check("dep", "dep", fixed("pass").run, requires=("t.warn",)))
    assert go(reg, tmp_path).checks[1].status == "pass"


def test_paid_checks_run_only_when_asked(tmp_path: Path) -> None:
    ran: list[str] = []

    def make(check_id: str, paid: bool) -> Check:
        async def run(_c: DoctorContext) -> CheckResult:
            ran.append(check_id)
            return CheckResult("pass", "ok")

        return Check(check_id, check_id, run, paid=paid)

    reg = CheckRegistry()
    reg.register(make("free", False))
    reg.register(make("paid", True))
    go(reg, tmp_path)
    assert ran == ["free"]
    go(reg, tmp_path, include_paid=True)
    assert ran == ["free", "free", "paid"]
    ran.clear()
    go(reg, tmp_path, only=["paid"])  # naming a paid check is explicit consent
    assert ran == ["paid"]


def test_unknown_check_id_is_an_error_and_ids_are_unique(tmp_path: Path) -> None:
    reg = CheckRegistry()
    reg.register(fixed("pass"))
    with pytest.raises(KeyError):
        go(reg, tmp_path, only=["nope"])
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(fixed("pass"))


# --- report formats ---------------------------------------------------------------------------


def test_json_and_text_reports(tmp_path: Path) -> None:
    reg = CheckRegistry()
    reg.register(fixed("pass", "all good"))
    reg.register(fixed("fail", "broken"))
    report = go(reg, tmp_path)
    body = json.loads(report.to_json())
    assert body["ok"] is False
    assert body["counts"]["fail"] == 1
    assert body["checks"][1] == {
        "id": "t.fail",
        "title": "fail",
        "status": "fail",
        "summary": "broken",
        "remedy": "do x",
        "data": {},
        "duration_s": body["checks"][1]["duration_s"],
    }
    text = report.render_text()
    assert "pass        t.pass: all good" in text
    assert "fix: do x" in text  # remedies are shown for problems only
    assert text.count("fix:") == 1
    assert text.rstrip().endswith("2 checks: 1 pass, 1 fail")


# --- built-in checks --------------------------------------------------------------------------


def isolated(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> None:
    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp / "no-user-config.yaml")


def test_default_checks_pass_on_a_clean_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    context = DoctorContext(repo_root=tmp_path)
    report = asyncio.run(run_checks(default_registry(context), context))
    by_id = {c.id: c for c in report.checks}
    assert by_id["python"].status == "pass"
    assert by_id["git"].status == "pass"
    assert by_id["config"].status == "pass"
    assert "agent.claude.cli" in by_id  # one per enabled agent
    assert "agent.cursor.cli" not in by_id  # disabled by default
    assert all(c.status in ("pass", "unsupported") for c in report.checks)


def test_invalid_config_fails_with_a_remedy_and_skips_agent_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text("defaults: { concurrency: null }\n")
    context = DoctorContext(repo_root=tmp_path)
    report = asyncio.run(run_checks(default_registry(context), context))
    config = next(c for c in report.checks if c.id == "config")
    assert config.status == "fail"
    assert config.remedy is not None
    assert "null" in config.remedy
    assert not any(c.id.startswith("agent.") for c in report.checks)
    assert report.exit_code == 1


def stub_cli(directory: Path, name: str, version: str, exit_code: int = 0) -> Path:
    """A fake agent CLI: a `.cmd` shim on Windows (like Cursor's), a shell script elsewhere."""
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text(f"@echo off\r\necho {version}\r\nexit /b {exit_code}\r\n")
    else:
        path = directory / name
        path.write_text(f"#!/bin/sh\necho {version}\nexit {exit_code}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def test_agent_cli_check_reports_version_of_a_shim(tmp_path: Path) -> None:
    shim = stub_cli(tmp_path, "stubagent", "stubagent 1.2.3")
    result = asyncio.run(agent_cli_check("stub", str(shim)).run(ctx(tmp_path)))
    assert result.status == "pass"
    assert result.data["version"] == "stubagent 1.2.3"


def test_agent_cli_check_flags_a_missing_command_as_unsupported(tmp_path: Path) -> None:
    result = asyncio.run(
        agent_cli_check("ghost", "definitely-not-installed-xyz").run(ctx(tmp_path))
    )
    assert result.status == "unsupported"
    assert result.remedy is not None
    assert "enabled: false" in result.remedy


def test_agent_cli_check_fails_when_version_command_fails(tmp_path: Path) -> None:
    shim = stub_cli(tmp_path, "brokenagent", "boom", exit_code=3)
    result = asyncio.run(agent_cli_check("broken", str(shim)).run(ctx(tmp_path)))
    assert result.status == "fail"
    assert "exited 3" in result.summary


def test_git_exclude_check_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from harness import init_repo
    from orch.core.gitexclude import ensure_exclude_block

    isolated(monkeypatch, tmp_path)

    def status_in(directory: Path) -> str:
        context = DoctorContext(repo_root=directory)
        report = asyncio.run(
            run_checks(default_registry(context), context, only=["git", "git.exclude"])
        )
        return next(c for c in report.checks if c.id == "git.exclude").status

    plain = tmp_path / "plain"
    plain.mkdir()
    assert status_in(plain) == "unsupported"
    repo = init_repo(tmp_path / "repo")
    assert status_in(repo) == "warn"
    ensure_exclude_block(repo)
    assert status_in(repo) == "pass"


# --- CLI and exit codes -----------------------------------------------------------------------


def test_exit_code_precedence_for_mixed_outcomes() -> None:
    order = [4, 5, 1, 3, 6, 7]  # cancellation, verification, failure, no agent, blocked, approval
    for i, winner in enumerate(order):
        assert combine(order[i:]) == winner
        assert combine([0, *order[i:], 0]) == winner
    assert combine([]) == Exit.OK
    assert combine([0, 0]) == Exit.OK
    assert combine([7, 2, 4]) == Exit.INVALID  # invalid input is not a run outcome


def test_exit_code_values_match_the_design() -> None:
    assert [int(e) for e in Exit] == [0, 1, 2, 3, 4, 5, 6, 7]


def test_cli_version_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == "0.0.0"
    assert main([]) == Exit.INVALID
    assert "doctor" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("command", "phase"),
    [("plan", 2), ("graph", 2), ("cancel", 2), ("opencode-config", 3), ("replay", 4)],
)
def test_later_phase_commands_are_rejected_with_the_phase(
    command: str, phase: int, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command, "anything", "--flag"]) == Exit.INVALID
    assert f"available from phase {phase}" in capsys.readouterr().err


def test_unfinished_phase_one_commands_say_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "do something"]) == Exit.INVALID
    assert "not implemented yet" in capsys.readouterr().err


def test_doctor_command_text_and_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    code = main(["-C", str(tmp_path), "doctor", "--check", "python", "--check", "config"])
    out = capsys.readouterr().out
    assert code == 0
    assert "python:" in out
    assert "2 checks: 2 pass" in out
    assert main(["-C", str(tmp_path), "doctor", "--json", "--check", "git"]) == 0
    assert json.loads(capsys.readouterr().out)["checks"][0]["id"] == "git"


def test_doctor_exits_one_on_a_failed_check_and_two_on_an_unknown_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text("bogus: 1\n")
    assert main(["-C", str(tmp_path), "doctor"]) == Exit.FAILURE
    capsys.readouterr()
    assert main(["-C", str(tmp_path), "doctor", "--check", "nope"]) == Exit.INVALID
    assert "unknown check 'nope'" in capsys.readouterr().out
