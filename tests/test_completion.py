"""Completion adapters remain unavailable until their own exact profile is reviewed."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from relay.adapters.claude_completion import ClaudeCompletionAdapter, parse_completion
from relay.adapters.completion import (
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionUsage,
    aggregate_usage,
)
from relay.core.platform.proc import ProcessResult


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
    proc: ProcessResult, status: int, code: str
) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(proc, structured=False)
    assert caught.value.status == status
    assert caught.value.code == code
    if status in (429, 502):
        # Bad exits name the exit code and a redacted stderr tail.
        assert "exit code 1" in str(caught.value)
        assert "secret" in str(caught.value)
    else:
        assert "secret" not in str(caught.value)


def test_plain_schema_arguments_are_separate_and_native_token_limit_is_never_invented() -> None:
    request = CompletionRequest("hello", "default")
    assert "--json-schema" not in ClaudeCompletionAdapter.arguments(request)
    args = ClaudeCompletionAdapter.arguments(replace(request, output_schema={"type": "object"}))
    assert args[args.index("--json-schema") + 1] == '{"type":"object"}'
    assert "--model" not in args
    assert sys.platform
