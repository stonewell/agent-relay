from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import relay.adapters.cursor_completion as cursor_completion
from relay.adapters.completion import (
    CompletionCapable,
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionTerminationError,
    CompletionUsage,
    ObservedModel,
    stream_completion_chunks,
)
from relay.adapters.cursor_completion import CursorCompletionAdapter, parse_completion
from relay.core.env import CompleteEnv
from relay.core.platform.proc import (
    _OUTPUT_SINK,  # pyright: ignore[reportPrivateUsage]
    CommandNotFound,
    OutputLimitExceeded,
    ProcessCancelled,
    ProcessDrainError,
    ProcessResult,
)
from relay.core.registry import Agent


def response(*events: dict[str, Any], **values: Any) -> ProcessResult:
    terminal = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "hello",
        **values,
    }
    return ProcessResult(0, "\n".join(json.dumps(event) for event in (*events, terminal)))


def fake_command(*_: Any, **__: Any) -> tuple[str, ...]:
    return ("cursor",)


def enumeration_command(cmd: str, args: tuple[str, ...], **_: Any) -> tuple[str, ...]:
    return (cmd, *args)


def legacy_command(*_: Any) -> str:
    return "cursor-agent"


def no_models(*_: Any) -> tuple[str, ...]:
    return ()


def test_cursor_model_enumeration_parses_cli_rows_and_caches_by_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cursor_completion, "_ENUMERATED", {})
    monkeypatch.setattr(cursor_completion, "resolve_command", enumeration_command)
    calls: list[dict[str, Any]] = []
    limits: list[int] = []

    @contextmanager
    def capture(_: Any, *, max_bytes: int) -> Generator[None]:
        limits.append(max_bytes)
        yield

    monkeypatch.setattr(cursor_completion, "stream_output", capture)

    def run(argv: tuple[str, ...], **kwargs: Any) -> ProcessResult:
        assert argv == ("cursor-agent", "models")
        calls.append(kwargs)
        return ProcessResult(
            0,
            "Available models\n\n"
            "auto - Auto (default)\n"
            "\x1b[32mgemini-3.8-flash-low\x1b[0m - Gemini 3.8 Flash Low\n"
            "gemini-3.8-flash-high - Gemini 3.8 Flash High\n"
            "auto - Auto duplicate\n"
            "--bad - Invalid ID\n"
            "missing-label - \n"
            "\nTip: use --model <id> (or /model <id> in interactive mode) to switch.\n",
        )

    monkeypatch.setattr(cursor_completion, "run_process", run)
    env = {"PATH": "cursor-path", "CURSOR_API_KEY": "account-one"}
    models = cursor_completion._enumerated_models("cursor-agent", env)  # pyright: ignore[reportPrivateUsage]
    assert models == ("auto", "gemini-3.8-flash-low", "gemini-3.8-flash-high")
    assert cursor_completion._enumerated_models("cursor-agent", dict(env)) == models  # pyright: ignore[reportPrivateUsage]
    assert len(calls) == 1
    assert calls[0]["env"] == env
    assert calls[0]["timeout_s"] == 15
    assert "stdin" not in calls[0]
    assert (
        cursor_completion._enumerated_models(  # pyright: ignore[reportPrivateUsage]
            "cursor-agent", {**env, "CURSOR_API_KEY": "account-two"}
        )
        == models
    )
    assert len(calls) == 2
    assert limits == [4 * 1024 * 1024, 4 * 1024 * 1024]
    assert "account-one" not in repr(cursor_completion._ENUMERATED)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "outcome",
    [
        ProcessResult(1, stderr="private-key must not be logged"),
        ProcessResult(None, "partial - Model", timed_out=True),
        ProcessResult(0, "Available models\nTip: no models available\n"),
        CommandNotFound("private-key"),
        OSError("private-key"),
        OutputLimitExceeded("private-key"),
    ],
)
def test_cursor_enumeration_failure_warns_once_and_uses_configured_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: ProcessResult | Exception,
) -> None:
    monkeypatch.setattr(cursor_completion, "_ENUMERATED", {})
    monkeypatch.setattr(cursor_completion, "resolve_cursor_command", legacy_command)
    monkeypatch.setattr(cursor_completion, "resolve_command", fake_command)
    calls = 0

    def run(*_: Any, **__: Any) -> ProcessResult:
        nonlocal calls
        calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(cursor_completion, "run_process", run)
    adapter = CursorCompletionAdapter(
        Agent(cmd="agent", models={"fast": "configured-fast", "deep": "configured-deep"}),
        parent_env={},
    )
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        assert adapter.default_aliases == ("default", "configured-fast", "configured-deep")
        assert adapter.default_aliases == ("default", "configured-fast", "configured-deep")
        assert CursorCompletionAdapter(Agent(cmd="agent"), parent_env={}).default_aliases == (
            "default",
        )
    assert calls == 1
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "using configured models" in caplog.text
    assert "private-key" not in caplog.text


def test_cursor_enumerated_models_replace_configured_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def models(cmd: str, env: CompleteEnv) -> tuple[str, ...]:
        calls.append((cmd, dict(env)))
        return ("auto", "gemini-3.8-flash-low")

    monkeypatch.setattr(cursor_completion, "resolve_cursor_command", legacy_command)
    monkeypatch.setattr(cursor_completion, "_enumerated_models", models)
    adapter = CursorCompletionAdapter(
        Agent(cmd="agent", models={"old": "not-listed"}),
        parent_env={"CURSOR_API_KEY": "native-key", "RELAY_SECRET": "not-forwarded"},
    )
    assert adapter.default_aliases == ("default", "auto", "gemini-3.8-flash-low")
    assert calls[0][0] == "cursor-agent"
    assert calls[0][1]["CURSOR_API_KEY"] == "native-key"
    assert "RELAY_SECRET" not in calls[0][1]


@pytest.mark.parametrize("failure", [ProcessCancelled(), ProcessDrainError()])
def test_cursor_enumeration_preserves_cancellation_and_unknown_termination(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    monkeypatch.setattr(cursor_completion, "_ENUMERATED", {})
    monkeypatch.setattr(cursor_completion, "resolve_command", fake_command)

    def run(*_: Any, **__: Any) -> ProcessResult:
        raise failure

    monkeypatch.setattr(cursor_completion, "run_process", run)
    with pytest.raises(type(failure)):
        cursor_completion._enumerated_models("cursor-agent", {})  # pyright: ignore[reportPrivateUsage]
    assert not cursor_completion._ENUMERATED  # pyright: ignore[reportPrivateUsage]
    assert not caplog.records


def test_cursor_support_and_aliases_are_backend_specific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_completion, "_enumerated_models", no_models)
    adapter = CursorCompletionAdapter(
        Agent(cmd="agent", models={"default": "auto", "fast": "auto", "deep": "opus"})
    )
    assert isinstance(adapter, CompletionCapable)
    assert adapter.default_aliases == ("default", "auto", "opus")
    support = adapter.inspect()
    assert support.supports_tools
    assert not support.native_max_tokens
    assert not support.supports_reasoning_effort
    assert support.profile.agent == "cursor"


def test_cursor_invocation_isolated_settings_and_exact_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    parent = {
        "PATH": os.environ["PATH"],
        "CURSOR_API_KEY": "parent-key",
        "RELAY_SECRET": "never-forward",
        "HOME": str(tmp_path / "real-home"),
        "APPDATA": str(tmp_path / "real-auth"),
    }
    adapter = CursorCompletionAdapter(
        Agent(
            cmd="custom-cursor", env={"CURSOR_API_KEY": "agent-key", "CURSOR_CONFIG_DIR": "unsafe"}
        ),
        parent_env=parent,
    )

    def resolve(cmd: str, args: tuple[str, ...], **_: Any) -> tuple[str, ...]:
        assert cmd == "custom-cursor"
        return (cmd, *args)

    def run(command: tuple[str, ...], **kwargs: Any) -> ProcessResult:
        calls.append((command, kwargs))
        return response(result='{"content":"yes","tool_calls":[]}')

    monkeypatch.setattr(cursor_completion, "resolve_command", resolve)
    monkeypatch.setattr(cursor_completion, "run_process", run)
    prompt = 'quotes " !x! $() 中文\nsecond line'
    schema = {"type": "object", "properties": {"content": {"type": "string"}}}
    result = adapter.complete(
        CompletionRequest(prompt, "auto", output_schema=schema),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=5,
        max_output_bytes=10000,
    )
    command, kwargs = calls[0]
    assert command == (
        "custom-cursor",
        "-p",
        "--mode",
        "ask",
        "--trust",
        "--output-format",
        "stream-json",
        "--model",
        "auto",
    )
    assert prompt not in command
    assert kwargs["stdin"].startswith(prompt)
    assert json.dumps(schema, separators=(",", ":")) in kwargs["stdin"]
    env = kwargs["env"]
    assert isinstance(env, CompleteEnv)
    assert env["CURSOR_API_KEY"] == "agent-key"
    assert "RELAY_SECRET" not in env
    assert env["CURSOR_CONFIG_DIR"] == str(tmp_path / ".cursor-config")
    assert env["CURSOR_DATA_DIR"] == str(tmp_path / ".cursor-data")
    if os.name == "nt":
        assert env["APPDATA"] == parent["APPDATA"]
    else:
        assert env["HOME"] == parent["HOME"]
    settings = json.loads((tmp_path / ".cursor-config/cli-config.json").read_text())
    assert settings["permissions"]["allow"] == []
    assert set(settings["permissions"]["deny"]) == {
        "Shell(*)",
        "Read(**)",
        "Write(**)",
        "WebFetch(*)",
        "Mcp(*:*)",
    }
    assert json.loads((tmp_path / ".cursor-config/mcp.json").read_text()) == {"mcpServers": {}}
    assert json.loads((tmp_path / ".cursor-config/hooks.json").read_text()) == {
        "version": 1,
        "hooks": {},
    }
    assert "agent-key" not in "".join(
        path.read_text() for path in tmp_path.rglob("*") if path.is_file()
    )
    assert result.envelope == {"content": "yes", "tool_calls": []}


def test_generated_deny_hook_blocks_and_records_attempt(tmp_path: Path) -> None:
    # Exercise the actual generated shell command, including paths with spaces and '$'.
    scratch = tmp_path / "space $value 'quote"
    scratch.mkdir()
    _, _, marker = cursor_completion._prepare_policy(scratch)  # pyright: ignore[reportPrivateUsage]
    hooks = json.loads((scratch / ".cursor/hooks.json").read_text())
    policy = hooks["hooks"]["preToolUse"][0]
    # Tool execution must stay blocked if the interpreter cannot start, hangs, or
    # returns malformed output instead of the normal deny decision.
    assert policy["failClosed"] is True
    assert policy["timeout"] == 10
    command = policy["command"]
    if os.name == "nt":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        assert shell is not None
        argv = [shell, "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]
    proc = subprocess.run(
        argv,
        input='{"tool_name":"Read","tool_input":{"path":"secret"}}',
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 2, proc.stderr
    assert json.loads(proc.stdout)["permission"] == "deny"
    assert marker.is_file()


def test_hook_attempt_cannot_be_hidden_by_success_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cursor_completion, "resolve_command", fake_command)

    def run(*_: Any, **__: Any) -> ProcessResult:
        (tmp_path / ".cursor-config/deny_tools.used").touch()
        return response()

    monkeypatch.setattr(cursor_completion, "run_process", run)
    with pytest.raises(CompletionError) as caught:
        CursorCompletionAdapter(Agent(cmd="cursor")).complete(
            CompletionRequest("hi"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert caught.value.code == "backend_tool_use"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_cursor_effort_rejected_before_launch(effort: str, tmp_path: Path) -> None:
    with pytest.raises(CompletionError) as caught:
        CursorCompletionAdapter(Agent(cmd="nonexistent")).complete(
            CompletionRequest("hi", effort=effort),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert caught.value.status == 400
    assert caught.value.param == "reasoning_effort"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("model", ["--force", "foo!x!", "foo bar", "foo;echo", "foo%PATH%"])
def test_cursor_invalid_model_is_rejected(model: str) -> None:
    with pytest.raises(CompletionError) as caught:
        CursorCompletionAdapter(Agent(cmd="agent")).arguments(CompletionRequest("hello", model))
    assert caught.value.param == "model"


@pytest.fixture
def reasoning_cursor(monkeypatch: pytest.MonkeyPatch) -> CursorCompletionAdapter:
    def models(*_: object) -> tuple[str, ...]:
        return (
            "auto",
            "gemini-3.8-flash-low",
            "gemini-3.8-flash-medium",
            "gemini-3.8-flash-high",
            "gemini-3.7-flash-low",
            "gemini-3.7-flash-medium",
            "gemini-3.7-flash-high",
            "gemini-3.6-flash-minimal",
            "gemini-3.6-flash-low",
            "gemini-3.6-flash-medium",
            "gemini-3.6-flash-high",
            "new-provider-model-low",
            "new-provider-model-high",
            "other-high",
            "gemini-3.1-pro",
        )

    monkeypatch.setattr(cursor_completion, "_enumerated_models", models)
    adapter = CursorCompletionAdapter(Agent(cmd="agent"), parent_env={})
    assert "gemini-3.8-flash" in adapter.default_aliases
    return adapter


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.8-flash-low"])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
def test_cursor_reasoning_parameter_preserves_native_default_and_overrides_legacy_suffix(
    reasoning_cursor: CursorCompletionAdapter, model: str, effort: str | None
) -> None:
    args = reasoning_cursor.arguments(CompletionRequest("hello", model, effort))
    expected = model if effort is None else f"gemini-3.8-flash-{effort}"
    assert args == (
        "-p",
        "--mode",
        "ask",
        "--trust",
        "--output-format",
        "stream-json",
        "--model",
        expected,
    )


@pytest.mark.parametrize("base", ["gemini-3.7-flash", "gemini-3.6-flash"])
@pytest.mark.parametrize("effort", [None, "low", "medium", "high"])
@pytest.mark.parametrize("legacy", [False, True])
def test_cursor_other_reasoning_families_preserve_defaults_and_map_effort(
    reasoning_cursor: CursorCompletionAdapter, base: str, effort: str | None, legacy: bool
) -> None:
    model = f"{base}-low" if legacy else base
    args = reasoning_cursor.arguments(CompletionRequest("hello", model, effort))
    assert args[-1] == (model if effort is None else f"{base}-{effort}")


@pytest.mark.parametrize(
    "model", ["gemini-3.6-flash", "gemini-3.6-flash-minimal", "gemini-3.6-flash-high"]
)
def test_cursor_minimal_is_native_only_for_supported_family(
    reasoning_cursor: CursorCompletionAdapter, model: str
) -> None:
    assert (
        reasoning_cursor.arguments(CompletionRequest("hello", model, "minimal"))[-1]
        == "gemini-3.6-flash-minimal"
    )
    assert reasoning_cursor.arguments(CompletionRequest("hello", model))[-1] == model


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.7-flash-low", "gemini-3.1-pro"])
def test_cursor_minimal_is_rejected_for_other_families(
    reasoning_cursor: CursorCompletionAdapter, model: str
) -> None:
    with pytest.raises(CompletionError) as caught:
        reasoning_cursor.arguments(CompletionRequest("hello", model, "minimal"))
    assert caught.value.param == "reasoning_effort"


@pytest.mark.parametrize(
    "model",
    [
        "default",
        "auto",
        "other-high",
        "gemini-9-flash-low",
        "other-minimal",
        "gemini-3.1-pro",
    ],
)
def test_cursor_effort_is_not_inferred_from_unverified_suffixes(
    reasoning_cursor: CursorCompletionAdapter, model: str
) -> None:
    with pytest.raises(CompletionError) as caught:
        reasoning_cursor.arguments(CompletionRequest("hello", model, "low"))
    assert caught.value.param == "reasoning_effort"
    args = reasoning_cursor.arguments(CompletionRequest("hello", model))
    assert ("--model" not in args) if model == "default" else args[-1] == model


def test_cursor_future_family_uses_inventory_without_manual_mapping(
    reasoning_cursor: CursorCompletionAdapter,
) -> None:
    assert reasoning_cursor.inspect().model_options["new-provider-model"].reasoning_efforts == (
        "low",
        "high",
    )
    for model in ("new-provider-model", "new-provider-model-low"):
        assert (
            reasoning_cursor.arguments(CompletionRequest("hello", model, "high"))[-1]
            == "new-provider-model-high"
        )
        with pytest.raises(CompletionError):
            reasoning_cursor.arguments(CompletionRequest("hello", model, "medium"))


def test_cursor_groups_only_successfully_enumerated_efforts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def models(*_: object) -> tuple[str, ...]:
        return ("gemini-3.8-flash-high", "gemini-3.8-flash-low")

    monkeypatch.setattr(cursor_completion, "_enumerated_models", models)
    adapter = CursorCompletionAdapter(Agent(cmd="agent"), parent_env={})
    assert adapter.default_aliases == (
        "default",
        "gemini-3.8-flash",
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-low",
    )
    support = adapter.inspect()
    assert support.model_options["gemini-3.8-flash"].reasoning_efforts == ("low", "high")
    assert support.model_options["gemini-3.8-flash-low"].listed is False
    with pytest.raises(CompletionError):
        adapter.arguments(CompletionRequest("hello", "gemini-3.8-flash", "medium"))
    with pytest.raises(CompletionError):
        adapter.arguments(CompletionRequest("hello", "gemini-3.8-flash", "low] --force"))

    monkeypatch.setattr(cursor_completion, "_enumerated_models", no_models)
    fallback = CursorCompletionAdapter(
        Agent(cmd="agent", models={"fast": "gemini-3.8-flash-low"}), parent_env={}
    )
    assert fallback.default_aliases == ("default", "gemini-3.8-flash-low")
    assert fallback.inspect().model_options == {}
    assert (
        fallback.arguments(CompletionRequest("hello", "gemini-3.8-flash-low"))[-1]
        == "gemini-3.8-flash-low"
    )
    with pytest.raises(CompletionError):
        fallback.arguments(CompletionRequest("hello", "gemini-3.8-flash-low", "high"))


def test_cursor_plain_usage_and_observed_metadata() -> None:
    proc = response(
        {"type": "system", "subtype": "init", "model": "display name", "tools": ["read"]},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "partial display"}]},
        },
        usage={"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 20, "cacheWriteTokens": 30},
        model="actual-model",
        contextWindow=1000,
        maxOutputTokens=100,
    )
    result = parse_completion(proc, structured=False)
    assert result.content == "hello"
    assert result.usage == CompletionUsage(60, 5, 20, 30)
    assert result.observed == ObservedModel("actual-model", 1000, 100)
    assert parse_completion(response(), structured=False).usage is None
    assert parse_completion(response(model="unbounded"), structured=False).observed is None


@pytest.mark.parametrize(
    "usage",
    [
        {"inputTokens": 5, "outputTokens": 2},
        {"inputTokens": True, "outputTokens": 2, "cacheReadTokens": 0, "cacheWriteTokens": 0},
        {"inputTokens": 5, "outputTokens": -1, "cacheReadTokens": 0, "cacheWriteTokens": 0},
    ],
)
def test_cursor_missing_or_invalid_usage_remains_unknown(usage: dict[str, Any]) -> None:
    assert parse_completion(response(usage=usage), structured=False).usage is None


@pytest.mark.parametrize(
    "values",
    [
        {"subtype": "cancelled"},
        {"subtype": "interrupted"},
        {"subtype": "error"},
        {"subtype": None},
        {"is_error": True},
        {"is_error": "false"},
        {"stop_reason": "cancelled"},
    ],
)
def test_cursor_unsuccessful_terminal_results_fail(values: dict[str, Any]) -> None:
    with pytest.raises(CompletionError):
        parse_completion(response(**values), structured=False)


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        '{"type":"assistant","message":{"content":[]}}',
        '{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n{"type":"assistant"}',
        '{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n{"type":"result","subtype":"success","is_error":false,"result":"ok"}',
    ],
)
def test_cursor_requires_one_final_result(stdout: str) -> None:
    with pytest.raises(CompletionError):
        parse_completion(ProcessResult(0, stdout), structured=False)


@pytest.mark.parametrize(
    "event",
    [
        {"type": "tool_call", "subtype": "started"},
        {"type": "tool_call", "subtype": "completed"},
        {"type": "function_call"},
        {"type": "assistant", "message": {"tool_calls": [{"name": "read"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read"}]}},
    ],
)
def test_cursor_tool_activity_is_rejected(event: dict[str, Any]) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(response(event), structured=False)
    assert caught.value.code == "backend_tool_use"


def test_cursor_structured_output_and_invalid_or_truncated_json() -> None:
    envelope = {"content": None, "tool_calls": [{"name": "read", "arguments": {"path": "x"}}]}
    result = parse_completion(response(result=json.dumps(envelope)), structured=True)
    assert result.envelope == envelope
    for content in ("not json", "[]", "```json\n{}\n```"):
        with pytest.raises(CompletionInvalid):
            parse_completion(response(result=content), structured=True)
    with pytest.raises(CompletionError) as caught:
        parse_completion(response(result="{}", stop_reason="max_tokens"), structured=True)
    assert caught.value.code == "incomplete_tool_output"
    assert (
        parse_completion(response(stop_reason="max_tokens"), structured=False).finish_reason
        == "length"
    )


@pytest.mark.parametrize(
    "proc,status,code",
    [
        (ProcessResult(None, timed_out=True), 504, "timeout"),
        (ProcessResult(1, stderr="context window exceeded"), 400, "context_length_exceeded"),
        (ProcessResult(1, stderr="429 quota key-secret"), 429, "rate_limit_exceeded"),
        (ProcessResult(1, stderr="failure key-secret"), 502, "completion_failed"),
    ],
)
def test_cursor_process_errors_redact_credentials(
    proc: ProcessResult, status: int, code: str
) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(proc, structured=False, secrets={"CURSOR_API_KEY": "key-secret"})
    assert caught.value.status == status
    assert caught.value.code == code
    assert "key-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "failure,code",
    [
        (CommandNotFound("cursor"), "unavailable"),
        (ProcessCancelled(), "request_cancelled"),
        (OutputLimitExceeded(), "output_limit_exceeded"),
        (ProcessDrainError(), "termination_unknown"),
    ],
)
def test_cursor_process_ownership_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception, code: str
) -> None:
    monkeypatch.setattr(cursor_completion, "resolve_command", fake_command)

    def fail(*_: Any, **__: Any) -> ProcessResult:
        raise failure

    monkeypatch.setattr(cursor_completion, "run_process", fail)
    with pytest.raises(CompletionError) as caught:
        CursorCompletionAdapter(Agent(cmd="cursor")).complete(
            CompletionRequest("hi"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert caught.value.code == code
    if isinstance(failure, ProcessDrainError):
        assert isinstance(caught.value, CompletionTerminationError)


def test_cursor_policy_setup_failure_does_not_launch(tmp_path: Path) -> None:
    (tmp_path / ".cursor").write_text("not a directory")
    with pytest.raises(CompletionError, match="restrictions"):
        CursorCompletionAdapter(Agent(cmd="missing")).complete(
            CompletionRequest("hi"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )


def test_cursor_arguments_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_completion, "_enumerated_models", no_models)
    adapter = CursorCompletionAdapter(Agent(cmd="cursor"))

    # Plain stream
    req_plain = CompletionRequest("prompt", "default")
    args_plain = adapter.arguments(req_plain, stream=True)
    assert "--stream-partial-output" in args_plain
    assert args_plain == (
        "-p",
        "--mode",
        "ask",
        "--trust",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
    )

    # Stream with model: verify --stream-partial-output is before --model
    req_model = CompletionRequest("prompt", "custom-model")
    args_model = adapter.arguments(req_model, stream=True)
    assert "--stream-partial-output" in args_model
    assert "--model" in args_model
    assert args_model.index("--stream-partial-output") < args_model.index("--model")
    assert args_model == (
        "-p",
        "--mode",
        "ask",
        "--trust",
        "--output-format",
        "stream-json",
        "--stream-partial-output",
        "--model",
        "custom-model",
    )

    # Stream with schema: --stream-partial-output omitted
    req_schema = CompletionRequest("prompt", "default", output_schema={"type": "object"})
    args_schema = adapter.arguments(req_schema, stream=True)
    assert "--stream-partial-output" not in args_schema
    assert args_schema == ("-p", "--mode", "ask", "--trust", "--output-format", "stream-json")


def test_cursor_stream_sink(tmp_path: Path) -> None:
    chunks: list[str] = []
    tool_marker = tmp_path / "deny_tools.used"
    sink = cursor_completion._stream_sink(chunks.append, tool_marker)  # pyright: ignore[reportPrivateUsage]

    # 1. timestamp_ms partial chunks
    sink(
        json.dumps(
            {
                "type": "assistant",
                "timestamp_ms": 100,
                "message": {"content": "part1"},
            }
        )
    )
    sink(
        json.dumps(
            {
                "type": "assistant",
                "timestamp_ms": 200,
                "message": {"content": "part2"},
            }
        )
    )
    assert chunks == ["part1", "part2"]

    # 2. Cumulative snapshot suffix deduplication (when timestamp_ms not in event)
    chunks.clear()
    sink2 = cursor_completion._stream_sink(chunks.append, tool_marker)  # pyright: ignore[reportPrivateUsage]
    sink2(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": "Hello"},
            }
        )
    )
    sink2(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": "Hello world"},
            }
        )
    )
    assert chunks == ["Hello", " world"]

    # 3. Ignoring model_call_id pre-tool flushes
    chunks.clear()
    sink3 = cursor_completion._stream_sink(chunks.append, tool_marker)  # pyright: ignore[reportPrivateUsage]
    sink3(
        json.dumps(
            {
                "type": "assistant",
                "model_call_id": "call_flush_1",
                "message": {"content": "pre-tool text"},
            }
        )
    )
    assert chunks == []
    # Regular message works after
    sink3(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": "regular"},
            }
        )
    )
    assert chunks == ["regular"]

    # 4. Halting on _tool_activity
    chunks.clear()
    sink4 = cursor_completion._stream_sink(chunks.append, tool_marker)  # pyright: ignore[reportPrivateUsage]
    sink4(
        json.dumps(
            {
                "type": "assistant",
                "timestamp_ms": 100,
                "message": {"content": "valid"},
            }
        )
    )
    assert chunks == ["valid"]
    # Tool activity event
    sink4(json.dumps({"type": "tool_call", "name": "bash"}))
    # Subsequent events ignored
    sink4(
        json.dumps(
            {
                "type": "assistant",
                "timestamp_ms": 200,
                "message": {"content": "ignored"},
            }
        )
    )
    assert chunks == ["valid"]

    # 5. Halting on tool_marker.exists()
    chunks.clear()
    tool_marker.touch()
    sink5 = cursor_completion._stream_sink(chunks.append, tool_marker)  # pyright: ignore[reportPrivateUsage]
    sink5(
        json.dumps(
            {
                "type": "assistant",
                "timestamp_ms": 100,
                "message": {"content": "ignored"},
            }
        )
    )
    assert chunks == []
    tool_marker.unlink()


def test_cursor_complete_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cursor_completion, "_enumerated_models", no_models)
    agent = Agent(cmd="cursor")
    adapter = CursorCompletionAdapter(agent, parent_env={})

    events = [
        {"type": "assistant", "timestamp_ms": 1, "message": {"content": "hello "}},
        {"type": "assistant", "timestamp_ms": 2, "message": {"content": "world"}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "hello world"},
    ]
    raw_lines = [json.dumps(e) for e in events]

    def mock_run_process(cmd: Any, **kwargs: Any) -> ProcessResult:
        entry = _OUTPUT_SINK.get()
        if entry is not None:
            sink, _ = entry
            for line in raw_lines:
                sink(line)
        return ProcessResult(0, "\n".join(raw_lines))

    monkeypatch.setattr(cursor_completion, "run_process", mock_run_process)
    monkeypatch.setattr(cursor_completion, "resolve_cursor_command", legacy_command)
    monkeypatch.setattr(cursor_completion, "resolve_command", fake_command)

    chunks: list[str] = []
    res = adapter.complete(
        CompletionRequest("test", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=5,
        max_output_bytes=10000,
        on_chunk=chunks.append,
    )
    assert res.content == "hello world"
    assert chunks == ["hello ", "world"]

    # Also test stream_completion_chunks context manager
    chunks_ctx: list[str] = []
    with stream_completion_chunks(chunks_ctx.append):
        res2 = adapter.complete(
            CompletionRequest("test", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=10000,
        )
    assert res2.content == "hello world"
    assert chunks_ctx == ["hello ", "world"]
