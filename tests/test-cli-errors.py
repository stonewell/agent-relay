"""Machine-readable errors at the public CLI boundary (task 1.29)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay.cli.commands import doctor
from relay.cli.main import main


@pytest.mark.parametrize("command", ["opencode-config", "replay"])
def test_future_commands_prioritize_phase_errors(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([command, "--unknown", "--json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "available from phase" in json.loads(output.err)["error"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--max-attempts", "0", "--json"],
        ["reply", "--json"],
        ["status", "--unknown", "--json"],
        ["unknown", "--json"],
    ],
)
def test_argument_errors_are_json(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]


def test_unknown_doctor_check_is_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["doctor", "--check", "no-such-check", "--json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "unknown check" in json.loads(output.err)["error"]


@pytest.mark.parametrize("command", ["status", "result", "apply", "reply", "resume"])
def test_missing_run_is_invalid_input(
    tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["-C", str(tmp_path), command, "2026-10-23-abcd", "--json"]
    if command == "reply":
        arguments.extend(["n1", "answer", "--yes"])
    assert main(arguments) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "not found" in json.loads(output.err)["error"]
    assert not (tmp_path / ".agents").exists()


def test_handler_failure_has_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*args: object) -> int:
        raise RuntimeError("cannot read the run store")

    monkeypatch.setattr(doctor, "run", broken)
    assert main(["doctor", "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"] == "relay doctor: cannot read the run store"


def test_keyboard_cancellation_has_exit_four(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*args: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(doctor, "run", interrupted)
    assert main(["doctor", "--json"]) == 4
    assert "cancelled" in json.loads(capsys.readouterr().err)["error"]
