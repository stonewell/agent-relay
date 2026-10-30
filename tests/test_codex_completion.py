"""Codex completion transport, terminal status, usage and policy boundaries."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from relay.adapters import codex_completion as module
from relay.adapters.codex_completion import CodexCompletionAdapter, parse_completion
from relay.adapters.completion import CompletionError, CompletionInvalid, CompletionRequest
from relay.core.env import CompleteEnv
from relay.core.platform.proc import (
    CommandNotFound,
    OutputLimitExceeded,
    ProcessCancelled,
    ProcessDrainError,
    ProcessResult,
)
from relay.core.registry import Agent

HOOK_TRUST_NOTICE = (
    "`--dangerously-bypass-hook-trust` is enabled. "
    "Enabled hooks may run without review for this invocation."
)


def result(content: str = "hello", **terminal: Any) -> ProcessResult:
    return ProcessResult(
        0,
        "\n".join(
            json.dumps(event)
            for event in [
                {"type": "thread.started", "thread_id": "t"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": content}},
                {"type": "turn.completed", **terminal},
            ]
        ),
    )


def test_recorded_plain_usage_includes_cached_input_once() -> None:
    raw = Path("tests/fixtures/recorded/codex_plain.jsonl").read_text(encoding="utf-8")
    completion = parse_completion(ProcessResult(0, raw), structured=False)
    assert completion.content == "OK"
    assert completion.observed is None
    assert completion.usage is not None
    assert completion.usage.prompt_tokens == 13990
    assert completion.usage.cached_tokens == 12288
    assert completion.usage.completion_tokens == 5
    assert parse_completion(result(), structured=False).usage is None


def test_exact_hook_trust_advisory_is_allowed_only_before_turn_start() -> None:
    advisory = {
        "type": "item.completed",
        "item": {
            "id": "item_0",
            "type": "error",
            "message": HOOK_TRUST_NOTICE,
        },
    }
    events = [json.loads(line) for line in result().stdout.splitlines()]
    # Actual CLI emits the same advisory twice during startup.
    allowed = [events[0], advisory, advisory, *events[1:]]
    proc = ProcessResult(0, "\n".join(json.dumps(event) for event in allowed))
    assert parse_completion(proc, structured=False).content == "hello"
    rejected = [*events[:2], advisory, *events[2:]]
    with pytest.raises(CompletionError):
        parse_completion(
            ProcessResult(0, "\n".join(json.dumps(event) for event in rejected)), structured=False
        )


@pytest.mark.parametrize("kind", ["item.started", "item.updated"])
def test_hook_trust_advisory_requires_completed_item(kind: str) -> None:
    item = json.dumps({"type": kind, "item": {"type": "error", "message": HOOK_TRUST_NOTICE}})
    with pytest.raises(CompletionError):
        parse_completion(ProcessResult(0, item + "\n" + result().stdout), structured=False)


@pytest.mark.parametrize(
    "message",
    [
        "Unknown startup failure",
        "PreToolUse hook failed to start",
        HOOK_TRUST_NOTICE + " A hook failed.",
        " " + HOOK_TRUST_NOTICE,
    ],
)
def test_other_startup_error_items_are_not_ignored(message: str) -> None:
    item = json.dumps({"type": "item.completed", "item": {"type": "error", "message": message}})
    with pytest.raises(CompletionError) as caught:
        parse_completion(ProcessResult(0, item + "\n" + result().stdout), structured=False)
    assert caught.value.code == "completion_failed"


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": True, "output_tokens": 1},
        {"input_tokens": 2, "output_tokens": 1, "cached_input_tokens": 3},
        {"input_tokens": 2},
    ],
)
def test_invalid_usage_stays_unknown(usage: dict[str, Any]) -> None:
    assert parse_completion(result(usage=usage), structured=False).usage is None


@pytest.mark.parametrize(
    "kind",
    ["command_execution", "mcp_tool_call", "file_change", "web_search", "todo_list", "future_tool"],
)
def test_tool_activity_is_rejected_even_with_successful_final_text(kind: str) -> None:
    activity = json.dumps({"type": "item.started", "item": {"type": kind}})
    proc = result()
    with pytest.raises(CompletionError) as caught:
        parse_completion(ProcessResult(0, activity + "\n" + proc.stdout), structured=False)
    assert caught.value.code == "backend_tool_activity"


@pytest.mark.parametrize(
    "proc",
    [
        ProcessResult(
            0, '{"type":"item.completed","item":{"type":"agent_message","text":"partial"}}'
        ),
        ProcessResult(0, '{"type":"turn.failed","error":{"message":"failed"}}'),
        ProcessResult(0, '{"type":"turn.cancelled"}'),
        ProcessResult(0, "not JSON"),
        result(status="cancelled"),
        result(stop_reason="interrupted"),
        ProcessResult(
            0,
            result().stdout
            + '\n{"type":"item.completed","item":{"type":"agent_message","text":"late"}}',
        ),
        ProcessResult(0, result().stdout + '\n{"type":"turn.started"}'),
        ProcessResult(0, result().stdout, stderr="PreToolUse hook failed to start"),
    ],
)
def test_failure_or_missing_terminal_result_cannot_become_success(proc: ProcessResult) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(proc, structured=False)
    assert not isinstance(caught.value, CompletionInvalid)


def test_tool_argument_transport_decodes_without_losing_nulls_or_dynamic_keys() -> None:
    arguments = {"anything": [1, None, "雪"], "nested": {"key": True}}
    envelope = {
        "content": None,
        "tool_calls": [{"name": "edit", "arguments": json.dumps(arguments)}],
    }
    completion = parse_completion(result(json.dumps(envelope)), structured=True)
    assert completion.envelope == {
        "content": None,
        "tool_calls": [{"name": "edit", "arguments": arguments}],
    }
    assert json.loads(completion.raw) == completion.envelope
    with pytest.raises(CompletionInvalid):
        parse_completion(result("not an envelope"), structured=True)
    with pytest.raises(CompletionError) as caught:
        parse_completion(result(json.dumps(envelope), stop_reason="length"), structured=True)
    assert caught.value.code == "incomplete_tool_output"
    assert (
        parse_completion(result("partial", stop_reason="length"), structured=False).finish_reason
        == "length"
    )


def test_fixed_invocation_preserves_prompt_and_env_and_installs_deny_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def no_models(*_: object) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(module, "_enumerated_models", no_models)
    agent = Agent(
        cmd=sys.executable, env={"OPENAI_API_KEY": "configured"}, models={"deep": "gpt-deep"}
    )
    adapter = CodexCompletionAdapter(
        agent,
        parent_env={
            "PATH": "inherited",
            "OPENAI_API_KEY": "parent",
            "UNRELATED_SECRET": "no",
            "RELAY_GATEWAY_TOKEN": "no",
        },
    )
    prompt = "quotes \"' ! $() and Unicode 雪\n"
    request = CompletionRequest(prompt, "gpt-deep", "high")
    seen: dict[str, Any] = {}

    def run(command: Any, **kwargs: Any) -> ProcessResult:
        seen.update(kwargs)
        return result()

    def resolve(cmd: str, args: tuple[str, ...], **kwargs: Any) -> str:
        seen["args"] = args
        return cmd

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    assert (
        adapter.complete(
            request, cwd=tmp_path, cancel=threading.Event(), timeout_s=5, max_output_bytes=10000
        ).content
        == "hello"
    )
    assert adapter.default_aliases == ("default", "gpt-deep")
    assert seen["stdin"] == prompt and prompt not in seen["args"]
    assert isinstance(seen["env"], CompleteEnv)
    assert seen["env"]["OPENAI_API_KEY"] == "configured"
    assert "UNRELATED_SECRET" not in seen["env"] and "RELAY_GATEWAY_TOKEN" not in seen["env"]
    assert seen["args"][seen["args"].index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in seen["args"] and "--ignore-user-config" in seen["args"]
    assert 'model_reasoning_effort="high"' in seen["args"]
    assert "features.shell_tool=false" in seen["args"]
    assert "features.view_image=false" in seen["args"]
    assert "tools.view_image=false" not in seen["args"]
    assert 'web_search="disabled"' in seen["args"]
    policy = next(arg for arg in seen["args"] if arg.startswith("hooks.PreToolUse="))
    assert "command_windows=" in policy and "relay-deny-tools.py" in policy
    process = subprocess.run(
        [sys.executable, str(tmp_path / "relay-deny-tools.py")], capture_output=True, check=False
    )
    assert process.returncode == 2 and (tmp_path / "relay-tool-denied").exists()
    with pytest.raises(CompletionError) as caught:
        adapter.complete(
            request, cwd=tmp_path, cancel=threading.Event(), timeout_s=5, max_output_bytes=10000
        )
    assert caught.value.code == "backend_tool_activity"


@pytest.mark.parametrize("forced", [False, True])
def test_output_schema_encodes_only_dynamic_arguments_and_remains_strict(
    tmp_path: Path, forced: bool
) -> None:
    from relay.gateway.envelope import prepare_envelope
    from relay.gateway.protocol import parse_request

    request: dict[str, Any] = {
        "model": "codex",
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "edit", "parameters": {"type": "object"}},
            }
        ],
    }
    if forced:
        request["tool_choice"] = {"type": "function", "function": {"name": "edit"}}
    schema = prepare_envelope(parse_request(request))
    assert schema is not None
    arguments = CodexCompletionAdapter.arguments(
        CompletionRequest("x", output_schema=schema), tmp_path
    )
    stored = json.loads(
        Path(arguments[arguments.index("--output-schema") + 1]).read_text(encoding="utf-8")
    )
    assert stored["properties"]["tool_calls"]["items"]["properties"]["arguments"] == {
        "type": "string"
    }
    assert schema["properties"]["tool_calls"]["items"]["properties"]["arguments"] == {
        "type": "object"
    }
    assert stored["additionalProperties"] is False
    assert stored["properties"]["tool_calls"]["items"]["properties"]["name"] == {
        "type": "string",
        "enum": ["edit"],
    }


@pytest.mark.parametrize(
    ("proc", "status", "code"),
    [
        (ProcessResult(None, timed_out=True), 504, "timeout"),
        (ProcessResult(1, stderr="context_length_exceeded"), 400, "context_length_exceeded"),
        (ProcessResult(1, stderr="429 quota"), 429, "rate_limit_exceeded"),
    ],
)
def test_error_mapping(proc: ProcessResult, status: int, code: str) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(proc, structured=False)
    assert (caught.value.status, caught.value.code) == (status, code)


def test_unconfirmed_process_drain_is_quarantined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = CodexCompletionAdapter(Agent(cmd=sys.executable))

    def resolve(*args: Any, **kwargs: Any) -> str:
        return sys.executable

    monkeypatch.setattr(module, "resolve_command", resolve)

    def run(*args: Any, **kwargs: Any) -> ProcessResult:
        raise ProcessDrainError("undrained")

    monkeypatch.setattr(module, "run_process", run)
    with pytest.raises(CompletionError) as caught:
        adapter.complete(
            CompletionRequest("x"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=10000,
        )
    assert caught.value.code == "termination_unknown"


def test_debug_models_json_lists_visible_valid_slugs_in_cli_order() -> None:
    # Shape from the real `codex debug models` response; context metadata alone is
    # deliberately not treated as a complete context/output observation.
    payload = {
        "models": [
            {"slug": "gpt-main", "visibility": "list", "context_window": 500000},
            {"slug": "gpt-internal", "visibility": "hide"},
            {"slug": "gpt-main", "visibility": "list"},
            {"slug": "gpt-compatible"},
            {"slug": "--unsafe"},
            {"slug": 123},
            None,
        ]
    }
    assert module._model_slugs(json.dumps(payload)) == ("gpt-main", "gpt-compatible")  # pyright: ignore[reportPrivateUsage]


def test_debug_models_enumeration_uses_configured_command_and_environment_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_ENUMERATED", {})
    calls: list[dict[str, Any]] = []

    def resolve(cmd: str, args: tuple[str, ...], **kwargs: Any) -> tuple[str, ...]:
        assert args == ("debug", "models")
        return (cmd, *args)

    def run(argv: tuple[str, ...], **kwargs: Any) -> ProcessResult:
        assert kwargs["timeout_s"] == module.ENUMERATION_TIMEOUT_S
        calls.append({"argv": argv, **kwargs})
        return ProcessResult(0, '{"models":[{"slug":"gpt-listed"}]}')

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    agent = Agent(cmd="configured-codex", models={"fallback": "configured-only"})
    first = CodexCompletionAdapter(agent, parent_env={"OPENAI_API_KEY": "first"})
    second = CodexCompletionAdapter(agent, parent_env={"OPENAI_API_KEY": "first"})
    assert first.default_aliases == second.default_aliases == ("default", "gpt-listed")
    assert len(calls) == 1
    assert calls[0]["argv"] == ("configured-codex", "debug", "models")
    assert calls[0]["env"]["OPENAI_API_KEY"] == "first"
    third = CodexCompletionAdapter(agent, parent_env={"OPENAI_API_KEY": "second"})
    assert third.default_aliases == ("default", "gpt-listed")
    assert len(calls) == 2
    assert all("first" not in fingerprint for _, fingerprint in module._ENUMERATED)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "proc",
    [
        ProcessResult(1, stderr="private provider diagnostic"),
        ProcessResult(0, '{"models":[{"slug":"gpt-partial"}]}', timed_out=True),
        ProcessResult(0, "not json"),
        ProcessResult(0, "[]"),
        ProcessResult(0, '{"models":{}}'),
        ProcessResult(0, '{"models":[]}'),
        ProcessResult(0, '{"models":[{"slug":"hidden","visibility":"hide"}]}'),
    ],
)
def test_failed_enumeration_warns_once_and_falls_back_to_configured_models(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, proc: ProcessResult
) -> None:
    monkeypatch.setattr(module, "_ENUMERATED", {})
    calls = 0

    def run(*_: Any, **__: Any) -> ProcessResult:
        nonlocal calls
        calls += 1
        return proc

    def resolve(*_: object, **__: object) -> tuple[str, ...]:
        return ("codex",)

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    agent = Agent(cmd="codex", models={"custom": "gpt-configured"})
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert CodexCompletionAdapter(agent, parent_env={}).default_aliases == (
            "default",
            "gpt-configured",
        )
        assert CodexCompletionAdapter(Agent(cmd="codex"), parent_env={}).default_aliases == (
            "default",
        )
    assert calls == 1
    assert len(caplog.records) == 1
    assert "using configured models" in caplog.text
    assert "private provider diagnostic" not in caplog.text


@pytest.mark.parametrize(
    "error", [CommandNotFound("missing"), OSError("failed"), OutputLimitExceeded("too large")]
)
def test_enumeration_process_failures_warn_and_fall_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    monkeypatch.setattr(module, "_ENUMERATED", {})

    def run(*_: Any, **__: Any) -> ProcessResult:
        raise error

    def resolve(*_: object, **__: object) -> tuple[str, ...]:
        return ("codex",)

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert CodexCompletionAdapter(Agent(cmd="codex"), parent_env={}).default_aliases == (
            "default",
        )
    assert len(caplog.records) == 1


@pytest.mark.parametrize("error", [ProcessCancelled("stop"), ProcessDrainError("undrained")])
def test_enumeration_preserves_shutdown_and_process_ownership_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(module, "_ENUMERATED", {})

    def run(*_: Any, **__: Any) -> ProcessResult:
        raise error

    def resolve(*_: object, **__: object) -> tuple[str, ...]:
        return ("codex",)

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    with pytest.raises(type(error)):
        _ = CodexCompletionAdapter(Agent(cmd="codex"), parent_env={}).default_aliases
    assert not module._ENUMERATED  # pyright: ignore[reportPrivateUsage]
