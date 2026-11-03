"""Completion adapters remain unavailable until their own exact profile is reviewed."""

from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import relay.adapters.claude_completion as claude_completion
from relay.adapters.claude_completion import ClaudeCompletionAdapter, parse_completion
from relay.adapters.completion import (
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionUsage,
    aggregate_usage,
    stream_completion_chunks,
)
from relay.core.platform.proc import (
    _OUTPUT_SINK,  # pyright: ignore[reportPrivateUsage]
    ProcessResult,
    ResolvedCommand,
)
from relay.core.registry import Agent


def response(**values: Any) -> ProcessResult:
    return ProcessResult(0, json.dumps({"type": "result", "result": "hello", **values}))


def test_usage_records_disjoint_buckets_and_never_invents_missing_counters() -> None:
    recorded = Path("tests/fixtures/recorded/claude_plain.json").read_text(encoding="utf-8")
    result = parse_completion(ProcessResult(0, recorded), structured=False)
    assert result.usage is not None
    assert result.usage.prompt_tokens == 2 + 22018 + 15552
    assert result.usage.cached_tokens == 15552
    assert result.usage.cache_write_tokens == 22018
    assert result.usage.as_openai()["prompt_tokens_details"] == {"cached_tokens": 15552}
    assert parse_completion(response(), structured=False).usage is None
    assert (
        parse_completion(
            response(usage={"input_tokens": 1, "output_tokens": 2}), structured=False
        ).usage
        is None
    )
    assert aggregate_usage(result.usage, None) is None
    assert aggregate_usage(
        CompletionUsage(2, 3, 1, 0), CompletionUsage(3, 4, 2, 0)
    ) == CompletionUsage(5, 7, 3, 0)


def test_envelope_native_field_preferred_and_failure_cannot_be_repaired_as_success() -> None:
    envelope = {"content": None, "tool_calls": [{"name": "edit", "arguments": {"value": "x"}}]}
    result = parse_completion(response(structured_output=envelope, result="bogus"), structured=True)
    assert result.envelope == envelope
    assert (
        parse_completion(response(result=json.dumps(envelope)), structured=True).envelope
        == envelope
    )
    with pytest.raises(CompletionInvalid):
        parse_completion(response(result="not JSON"), structured=True)
    with pytest.raises(CompletionError) as caught:
        parse_completion(
            response(structured_output=envelope, stop_reason="max_tokens"), structured=True
        )
    assert not isinstance(caught.value, CompletionInvalid)
    with pytest.raises(CompletionError) as caught:
        parse_completion(response(is_error=True, result="secret provider error"), structured=True)
    assert "secret" not in str(caught.value)
    assert not isinstance(caught.value, CompletionInvalid)


@pytest.mark.parametrize(
    ("proc", "status", "code"),
    [
        (ProcessResult(None, timed_out=True), 504, "timeout"),
        (
            ProcessResult(1, stderr="context_length_exceeded: secret"),
            400,
            "context_length_exceeded",
        ),
        (ProcessResult(1, stderr="other secret"), 502, "completion_failed"),
        (
            ProcessResult(1, stderr="429 Too Many Requests: secret"),
            429,
            "rate_limit_exceeded",
        ),
    ],
)
def test_process_error_categories_do_not_expose_stderr(
    proc: ProcessResult, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="uvicorn.error"),
        pytest.raises(CompletionError) as caught,
    ):
        parse_completion(proc, structured=False)
    assert caught.value.status == status
    assert caught.value.code == code
    # Raw stderr is never the public message; bad exits name only the exit code.
    assert "secret" not in str(caught.value)
    if status in (429, 502):
        assert str(caught.value) == "Claude CLI completion failed: exit code 1"
        assert "secret" in caplog.text


def test_missing_completion_object_logs_stderr_but_returns_fixed_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="uvicorn.error"),
        pytest.raises(CompletionInvalid) as caught,
    ):
        parse_completion(
            ProcessResult(0, "no json", stderr="token-abcdef private"),
            structured=True,
            secrets={"KEY": "token-abcdef"},
        )
    assert str(caught.value) == "Claude CLI returned no completion object: exit code 0"
    assert "private" in caplog.text
    assert "token-abcdef" not in caplog.text


def _fake_shim(*_a: object, **_k: object) -> ResolvedCommand:
    return ResolvedCommand(argv=("cmd.exe",), windows_command_line="x" * 9000)


@pytest.mark.parametrize(
    "failure", [OSError("[WinError 5] Access is denied"), ValueError("batch newline")]
)
def test_claude_launch_failures_become_fixed_completion_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        if isinstance(failure, ValueError):
            raise failure
        return ("claude",)

    def run(*_a: object, **_k: object) -> ProcessResult:
        raise failure

    monkeypatch.setattr(claude_completion, "resolve_command", resolve)
    monkeypatch.setattr(claude_completion, "run_process", run)
    with pytest.raises(CompletionError) as caught:
        ClaudeCompletionAdapter(Agent(cmd="claude")).complete(
            CompletionRequest("hi"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert str(caught.value) == "Claude CLI could not be started"
    assert (caught.value.status, caught.value.code) == (502, "completion_failed")


def test_claude_oversized_schema_command_line_is_rejected_before_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*_a: object, **_k: object) -> ProcessResult:
        raise AssertionError("must not spawn")

    monkeypatch.setattr(claude_completion, "resolve_command", _fake_shim)
    monkeypatch.setattr(claude_completion, "run_process", run)
    with pytest.raises(CompletionError) as caught:
        ClaudeCompletionAdapter(Agent(cmd="claude")).complete(
            CompletionRequest("hi", output_schema={"type": "object"}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert (caught.value.status, caught.value.code, caught.value.param) == (
        400,
        "context_length_exceeded",
        "tools",
    )


def test_command_line_length_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    too_long = claude_completion.command_line_too_long
    assert not too_long(("claude", "x" * 40_000))  # unresolved test doubles are not checked
    assert not too_long(ResolvedCommand(argv=("cmd.exe",), windows_command_line="x" * 8191))
    assert too_long(ResolvedCommand(argv=("cmd.exe",), windows_command_line="x" * 8192))
    monkeypatch.setattr(sys, "platform", "win32")
    assert not too_long(ResolvedCommand(argv=("agy", "x" * 32_000)))
    assert too_long(ResolvedCommand(argv=("agy", 'quoted "' * 4_000)))
    monkeypatch.setattr(sys, "platform", "linux")
    assert not too_long(ResolvedCommand(argv=("agy", "x" * 40_000)))


def test_plain_schema_arguments_are_separate_and_native_token_limit_is_never_invented() -> None:
    request = CompletionRequest("hello", "default")
    assert "--json-schema" not in ClaudeCompletionAdapter.arguments(request)
    args = ClaudeCompletionAdapter.arguments(replace(request, output_schema={"type": "object"}))
    assert args[args.index("--json-schema") + 1] == '{"type":"object"}'
    assert "--model" not in args
    assert sys.platform


def test_claude_arguments_streaming() -> None:
    req_plain = CompletionRequest("hello", "default")
    args_plain = ClaudeCompletionAdapter.arguments(req_plain, stream=True)
    assert args_plain[1:5] == (
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    )

    args_non_stream = ClaudeCompletionAdapter.arguments(req_plain, stream=False)
    assert args_non_stream[1:3] == ("--output-format", "json")
    assert "--verbose" not in args_non_stream
    assert "--include-partial-messages" not in args_non_stream

    req_schema = CompletionRequest("hello", "default", output_schema={"type": "object"})
    args_schema = ClaudeCompletionAdapter.arguments(req_schema, stream=True)
    assert args_schema[1:3] == ("--output-format", "json")
    assert "--verbose" not in args_schema
    assert "--include-partial-messages" not in args_schema


def test_claude_stream_sink() -> None:
    chunks: list[str] = []
    sink = claude_completion._stream_sink(chunks.append)  # pyright: ignore[reportPrivateUsage]

    # 1. stream_event: content_block_delta -> text_delta
    sink(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hel"},
                },
            }
        )
    )
    sink(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "lo"},
                },
            }
        )
    )
    assert chunks == ["Hel", "lo"]

    # 2. assistant snapshot deduplication
    chunks.clear()
    sink2 = claude_completion._stream_sink(chunks.append)  # pyright: ignore[reportPrivateUsage]
    sink2(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello"}]},
            }
        )
    )
    sink2(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello world"}]},
            }
        )
    )
    assert chunks == ["Hello", " world"]

    # 3. stopping on is_error: true
    chunks.clear()
    sink3 = claude_completion._stream_sink(chunks.append)  # pyright: ignore[reportPrivateUsage]
    sink3(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hel"},
                },
            }
        )
    )
    sink3(json.dumps({"type": "result", "is_error": True}))
    sink3(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "lo"},
                },
            }
        )
    )
    assert chunks == ["Hel"]


def test_claude_complete_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(cmd="claude")
    adapter = ClaudeCompletionAdapter(agent)

    recorded_lines = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "chunk1"},
                },
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "chunk2"},
                },
            }
        ),
        json.dumps({"type": "result", "result": "chunk1chunk2"}),
    ]

    def mock_run_process(cmd: Any, **kwargs: Any) -> ProcessResult:
        entry = _OUTPUT_SINK.get()
        if entry is not None:
            sink, _ = entry
            for line in recorded_lines:
                sink(line)
        return ProcessResult(0, "\n".join(recorded_lines))

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return ("claude",)

    monkeypatch.setattr(claude_completion, "run_process", mock_run_process)
    monkeypatch.setattr(claude_completion, "resolve_command", fake_resolve)

    chunks: list[str] = []
    res = adapter.complete(
        CompletionRequest("test", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=5,
        max_output_bytes=10000,
        on_chunk=chunks.append,
    )
    assert res.content == "chunk1chunk2"
    assert chunks == ["chunk1", "chunk2"]

    # Also with stream_completion_chunks context manager
    chunks_ctx: list[str] = []
    with stream_completion_chunks(chunks_ctx.append):
        res2 = adapter.complete(
            CompletionRequest("test", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=10000,
        )
    assert res2.content == "chunk1chunk2"
    assert chunks_ctx == ["chunk1", "chunk2"]
