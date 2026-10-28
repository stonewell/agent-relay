"""Claude `coordinate` capability (task 2.32; design 5.6, 7.4), offline.

The CLI is replaced by patched `resolve_command`/`run_process`; no model is contacted. The live
behaviour (tool-free structured output, hook/MCP suppression) is covered by the opt-in canary.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from relay.adapters import claude as claude_module
from relay.adapters.claude import COORDINATOR_FLAGS, ClaudeAdapter
from relay.adapters.coordination import (
    CoordinatorInvalid,
    CoordinatorUnavailable,
    coordinator_schema,
)
from relay.core.coordinator import Qualification
from relay.core.platform.proc import ProcessResult, ResolvedCommand
from relay.core.registry import Agent

VERSION = "9.9.9 (Claude Code)"


def adapter() -> ClaudeAdapter:
    return ClaudeAdapter(Agent(cmd="claude"), parent_env={"PATH": "/bin"})


def qualified(a: ClaudeAdapter, status: str = "qualified", version: str = VERSION) -> Qualification:
    return Qualification(a.coordinator_profile(version), "tool-free", status, "test")  # type: ignore[arg-type]


class Recorder:
    def __init__(self, stdout: str, *, timed_out: bool = False) -> None:
        self.stdout = stdout
        self.timed_out = timed_out
        self.argv: list[str] = []
        self.stdin: str | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def resolve(cmd: str, args: Any, env: Any = None) -> ResolvedCommand:
            return ResolvedCommand(argv=[cmd, *args], windows_command_line=None)  # type: ignore[call-arg]

        def run(argv: Any, **kwargs: Any) -> ProcessResult:
            args = list(argv.argv)
            if args[1:] == ["--version"]:
                return ProcessResult(exit_code=0, stdout=VERSION + "\n")
            self.argv = args
            self.stdin = kwargs.get("stdin")
            return ProcessResult(
                exit_code=0, stdout=self.stdout, duration_s=1.5, timed_out=self.timed_out
            )

        monkeypatch.setattr(claude_module, "resolve_command", resolve)
        monkeypatch.setattr(claude_module, "run_process", run)


def envelope(structured: Any) -> str:
    return json.dumps(
        {
            "type": "result",
            "structured_output": structured,
            "modelUsage": {"claude-sonnet-5-5": {}},
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "total_cost_usd": 0.01,
        }
    )


def call(a: ClaudeAdapter, q: Qualification, tmp: Path):  # type: ignore[no-untyped-def]
    return asyncio.run(a.coordinate("plan it", cwd=tmp, qualification=q))


def test_invocation_is_tool_free_with_prompt_on_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = Recorder(envelope({"response": {"action": "finish", "summary": "ok"}}))
    rec.install(monkeypatch)
    a = adapter()
    result = call(a, qualified(a), tmp_path)
    assert result.response.response.action == "finish"
    assert result.model == "claude-sonnet-5-5" and result.cli_version == VERSION
    assert (result.tokens_in, result.tokens_out, result.cost_usd) == (3, 4, 0.01)
    assert rec.stdin == "plan it"
    argv = rec.argv
    for index in range(0, len(COORDINATOR_FLAGS)):
        assert COORDINATOR_FLAGS[index] in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--setting-sources" in argv and "--no-session-persistence" in argv
    assert "--dangerously-skip-permissions" not in argv and "--permission-mode" not in argv


def test_schema_is_inline_safe() -> None:
    text = json.dumps(coordinator_schema())
    assert '"$schema"' not in text and '"discriminator"' not in text and '"title"' not in text
    assert coordinator_schema()["type"] == "object"


@pytest.mark.parametrize("status", ["unqualified"])
def test_unqualified_or_changed_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str
) -> None:
    rec = Recorder(envelope({"response": {"action": "finish", "summary": "ok"}}))
    rec.install(monkeypatch)
    a = adapter()
    with pytest.raises(CoordinatorUnavailable):
        call(a, qualified(a, status), tmp_path)
    with pytest.raises(CoordinatorUnavailable):
        call(a, qualified(a, version="1.0.0"), tmp_path)  # version drift
    assert rec.argv == []  # the restricted call was never started


def test_different_env_overrides_invalidate_qualification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    Recorder("").install(monkeypatch)
    plain = adapter()
    tweaked = ClaudeAdapter(Agent(cmd="claude", env={"FOO": "1"}), parent_env={"PATH": "/bin"})
    with pytest.raises(CoordinatorUnavailable):
        call(tweaked, qualified(plain), tmp_path)


@pytest.mark.parametrize(
    "stdout",
    [
        "",  # no output
        "not json",
        envelope(None),
        envelope({"response": {"action": "run_shell"}}),
        envelope({"response": {"action": "finish", "summary": "x", "permissions": "full"}}),
    ],
)
def test_malformed_responses_raise_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
) -> None:
    Recorder(stdout).install(monkeypatch)
    a = adapter()
    with pytest.raises(CoordinatorInvalid):
        call(a, qualified(a), tmp_path)


def test_timeout_is_invalid_not_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    Recorder("", timed_out=True).install(monkeypatch)
    a = adapter()
    with pytest.raises(CoordinatorInvalid, match="timed out"):
        call(a, qualified(a), tmp_path)


def test_profile_version_is_the_bare_number() -> None:
    """The probe prints `2.1.312 (Claude Code)`; records are written with the number alone."""
    a = adapter()
    assert a.coordinator_profile("2.1.312 (Claude Code)").cli_version == "2.1.312"
    assert a.coordinator_profile("2.1.312").cli_version == "2.1.312"
    assert a.coordinator_profile(None).cli_version == "unknown"
    assert a.coordinator_profile("not a version").cli_version == "unknown"
