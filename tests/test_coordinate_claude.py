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
from relay.adapters.coordination import CoordinatorInvalid, coordinator_schema
from relay.core.platform.proc import ProcessResult, ResolvedCommand
from relay.core.registry import Agent


def adapter() -> ClaudeAdapter:
    return ClaudeAdapter(Agent(cmd="claude"), parent_env={"PATH": "/bin"})


class Recorder:
    def __init__(self, stdout: str, *, timed_out: bool = False) -> None:
        self.stdout = stdout
        self.timed_out = timed_out
        self.argv: list[str] = []
        self.calls: list[list[str]] = []
        self.stdin: str | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def resolve(cmd: str, args: Any, env: Any = None) -> ResolvedCommand:
            return ResolvedCommand(argv=[cmd, *args], windows_command_line=None)  # type: ignore[call-arg]

        def run(argv: Any, **kwargs: Any) -> ProcessResult:
            args = list(argv.argv)
            self.calls.append(args)
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


def call(a: ClaudeAdapter, tmp: Path):  # type: ignore[no-untyped-def]
    return asyncio.run(a.coordinate("plan it", cwd=tmp))


def test_invocation_is_tool_free_with_prompt_on_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = Recorder(envelope({"response": {"action": "finish", "summary": "ok"}}))
    rec.install(monkeypatch)
    a = adapter()
    result = call(a, tmp_path)
    assert result.response.response.action == "finish"
    assert result.model == "claude-sonnet-5-5" and result.cli_version is None
    assert len(rec.calls) == 1
    assert "--version" not in rec.calls[0]
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
        call(a, tmp_path)


def test_timeout_is_invalid_not_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    Recorder("", timed_out=True).install(monkeypatch)
    a = adapter()
    with pytest.raises(CoordinatorInvalid, match="timed out"):
        call(a, tmp_path)
