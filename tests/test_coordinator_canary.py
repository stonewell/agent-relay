"""Coordinator canary classification (task 2.32), with the CLI patched out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from relay.core import coordinator_canary as canary
from relay.core.platform.proc import CommandNotFound, ProcessResult, ResolvedCommand

GOOD = json.dumps(
    {"structured_output": {"response": {"action": "finish", "summary": "not readable"}}}
)


def patch(monkeypatch: pytest.MonkeyPatch, stdout: str, *, touch: tuple[str, ...] = ()) -> None:
    def resolve(cmd: str, _args: Any, env: Any = None) -> ResolvedCommand:
        return ResolvedCommand(argv=(cmd,))

    monkeypatch.setattr(canary, "resolve_command", resolve)

    def run(_argv: Any, *, cwd: Path, **_kw: Any) -> ProcessResult:
        for name in touch:
            (cwd / name).write_text("x")
        return ProcessResult(exit_code=0, stdout=stdout)

    monkeypatch.setattr(canary, "run_process", run)


def test_enforced_needs_a_valid_response_and_no_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch(monkeypatch, GOOD)
    assert canary.run_coordinator_canary().outcome == "enforced"


@pytest.mark.parametrize("marker", ["hook-marker.txt", "hook-prompt-marker.txt", "mcp-marker.txt"])
def test_any_trap_marker_is_a_bypass(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    patch(monkeypatch, GOOD, touch=(marker,))
    assert canary.run_coordinator_canary().outcome == "bypassed"


def test_directive_in_output_is_a_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    patch(monkeypatch, "PWNED-BY-CLAUDE-MD " + GOOD)
    assert canary.run_coordinator_canary().outcome == "bypassed"


def test_no_valid_response_is_inconclusive_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    patch(monkeypatch, "")
    assert canary.run_coordinator_canary().outcome == "inconclusive"


def test_missing_cli_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_a: Any, **_k: Any) -> ResolvedCommand:
        raise CommandNotFound("claude")

    monkeypatch.setattr(canary, "resolve_command", missing)
    assert canary.run_coordinator_canary().outcome == "unavailable"
