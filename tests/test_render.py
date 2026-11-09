"""Tests for transcript rendering and sliding-window truncation."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from relay.adapters.completion import (
    CompletionChunkSink,
    CompletionProfile,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    ObservedModel,
)
from relay.core.registry import Config
from relay.gateway.models import ModelCatalog
from relay.gateway.protocol import (
    ChatRequest,
    FunctionCall,
    Message,
    parse_request,
)
from relay.gateway.protocol import (
    HistoryCall as ToolCall,
)
from relay.gateway.render import (
    _group_turns,  # pyright: ignore[reportPrivateUsage]
    _target_turn_marker,  # pyright: ignore[reportPrivateUsage]
    render_repair,
    render_transcript,
)


def _msg(role: str, content: str | None = None, **kwargs: Any) -> Message:
    return Message.model_validate({"role": role, "content": content, **kwargs})


def _tool_call(
    call_id: str,
    name: str = "lookup",
    args: str | dict[str, Any] = "{}",
    **kwargs: Any,
) -> ToolCall:
    args_str = json.dumps(args) if isinstance(args, dict) else args
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments=args_str),
        **kwargs,
    )


tool_call = _tool_call


def make_request(messages: list[dict[str, Any]], **kwargs: Any) -> ChatRequest:
    payload: dict[str, Any] = {"model": "test-model", "messages": messages, **kwargs}
    return parse_request(payload)


def test_group_turns_basic() -> None:
    req = make_request(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
    )
    turns = _group_turns(req.messages)
    assert len(turns) == 4
    for i, turn in enumerate(turns):
        assert len(turn) == 1
        assert turn[0] == req.messages[i]


def test_group_turns_single_tool_call() -> None:
    req = make_request(
        [
            {"role": "user", "content": "call a tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call_1")],
            },
            {"role": "tool", "content": "result 1", "tool_call_id": "call_1"},
            {"role": "assistant", "content": "done"},
        ]
    )
    turns = _group_turns(req.messages)
    assert len(turns) == 3
    # Turn 0: user
    assert len(turns[0]) == 1
    assert turns[0][0].role == "user"
    # Turn 1: assistant tool_calls + tool result
    assert len(turns[1]) == 2
    assert turns[1][0].role == "assistant"
    assert turns[1][1].role == "tool"
    assert turns[1][1].tool_call_id == "call_1"
    # Turn 2: assistant done
    assert len(turns[2]) == 1
    assert turns[2][0].content == "done"


def test_group_turns_parallel_tool_calls() -> None:
    req = make_request(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call_a"), _tool_call("call_b")],
            },
            {"role": "tool", "content": "result b", "tool_call_id": "call_b"},
            {"role": "tool", "content": "result a", "tool_call_id": "call_a"},
            {"role": "user", "content": "next"},
        ]
    )
    turns = _group_turns(req.messages)
    assert len(turns) == 2
    # Turn 0: assistant + both tool results
    assert len(turns[0]) == 3
    assert turns[0][0].role == "assistant"
    assert turns[0][1].tool_call_id == "call_b"
    assert turns[0][2].tool_call_id == "call_a"
    # Turn 1: user
    assert len(turns[1]) == 1
    assert turns[1][0].role == "user"


def test_group_turns_orphaned_and_unfinished() -> None:
    # Direct Message instances for unusual history
    msgs = [
        _msg(role="tool", content="orphan", tool_call_id="call_x"),
        _msg(
            role="assistant",
            content=None,
            tool_calls=[
                _tool_call("call_y", "lookup", "{}"),
                _tool_call("call_z", "lookup", "{}"),
            ],
        ),
        # Only call_y returned, then user interrupts
        _msg(role="tool", content="res y", tool_call_id="call_y"),
        _msg(role="user", content="interrupted"),
    ]
    turns = _group_turns(msgs)
    assert len(turns) == 3
    assert len(turns[0]) == 1 and turns[0][0].role == "tool"
    assert len(turns[1]) == 2 and turns[1][0].role == "assistant"
    assert len(turns[2]) == 1 and turns[2][0].role == "user"


def test_untruncated_transcript_under_budget() -> None:
    req = make_request(
        [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
            {"role": "assistant", "content": "Hi there!"},
        ]
    )
    transcript = render_transcript(req, "test-model")
    assert "[Earlier conversation history truncated" not in transcript
    assert "You are a helpful assistant." in transcript
    assert "Hello!" in transcript
    assert "Hi there!" in transcript
    assert _target_turn_marker(req) in transcript


def test_truncation_with_max_chars_preserving_latest_turns_and_marker() -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": "Instructions"}]
    for i in range(10):
        messages.append({"role": "user", "content": f"User question {i:02d} " + "X" * 50})
        messages.append({"role": "assistant", "content": f"Answer {i:02d} " + "Y" * 50})

    req = make_request(messages)

    # Full transcript without truncation
    full = render_transcript(req, "test-model", max_chars=1_000_000)
    assert "[Earlier conversation history truncated" not in full

    # Truncate with budget allowing roughly only the last 2 turns
    max_chars = 1500
    truncated = render_transcript(req, "test-model", max_chars=max_chars)

    assert len(truncated) <= max_chars
    assert "[Earlier conversation history truncated:" in truncated
    assert "messages omitted to fit context limits]" in truncated
    # Pinned system instructions preserved
    assert "Instructions" in truncated
    # Latest turn preserved
    assert "User question 09" in truncated
    assert "Answer 09" in truncated
    # Old turns omitted
    assert "User question 00" not in truncated
    assert "Answer 00" not in truncated
    # Target turn marker preserved
    assert _target_turn_marker(req) in truncated


def test_preservation_of_initial_system_developer_messages() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "PINNED_SYSTEM_1"},
        {"role": "developer", "content": "PINNED_DEVELOPER_2"},
    ]
    for i in range(8):
        messages.append({"role": "user", "content": f"U{i} " + "a" * 80})
        messages.append({"role": "assistant", "content": f"A{i} " + "b" * 80})

    req = make_request(messages)
    truncated = render_transcript(req, "test-model", max_chars=1400)

    # Both initial system messages must be preserved
    assert "PINNED_SYSTEM_1" in truncated
    assert "PINNED_DEVELOPER_2" in truncated

    # Notice must appear AFTER system/developer instructions but BEFORE retained history
    sys1_idx = truncated.index("PINNED_SYSTEM_1")
    dev2_idx = truncated.index("PINNED_DEVELOPER_2")
    notice_idx = truncated.index("[Earlier conversation history truncated:")
    latest_a_idx = truncated.index("A7")

    assert sys1_idx < dev2_idx < notice_idx < latest_a_idx

    # With more budget, earlier history turns (like U7) are also retained
    truncated_more = render_transcript(req, "test-model", max_chars=1800)
    assert "U7" in truncated_more
    assert "A7" in truncated_more
    assert truncated_more.index("U7") < truncated_more.index("A7")


def test_mid_conversation_system_messages_not_pinned() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "U0 " + "m" * 100},
        {"role": "system", "content": "MID_CONVERSATION_SYSTEM " + "m" * 100},
        {"role": "user", "content": "U1 latest question"},
    ]
    req = make_request(messages)
    # Small budget so only the latest turn fits
    truncated = render_transcript(req, "test-model", max_chars=900)

    assert "U1 latest question" in truncated
    assert "MID_CONVERSATION_SYSTEM" not in truncated
    assert "[Earlier conversation history truncated: 2 messages omitted" in truncated


def test_atomic_tool_turn_preservation() -> None:
    # A single tool turn (assistant tool call + tool result)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Old message " + "z" * 100},
        {"role": "assistant", "content": "Old answer " + "z" * 100},
        {"role": "user", "content": "Call tool please"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("call_weather")],
        },
        {"role": "tool", "content": "Sunny and 72F", "tool_call_id": "call_weather"},
        {"role": "assistant", "content": "It is sunny and 72F outside."},
        {"role": "user", "content": "Latest user query"},
    ]
    req = make_request(messages)

    # Set budget large enough to keep the tool turn and latest user query, but omitting old messages
    req_from_tool = make_request(messages[2:])
    min_budget = len(render_transcript(req_from_tool, "test-model", max_chars=1_000_000)) + 150
    truncated = render_transcript(req, "test-model", max_chars=min_budget)

    assert "Call tool please" in truncated
    assert "call_weather" in truncated
    assert "Sunny and 72F" in truncated
    assert "It is sunny and 72F outside." in truncated
    assert "Latest user query" in truncated
    assert "Old message" not in truncated

    # Now make budget tighter so tool turn cannot fit:
    # The tool turn has [assistant, tool], which must be omitted together!
    # Only "Latest user query" should be kept.
    tight_budget = 1000
    truncated_tight = render_transcript(req, "test-model", max_chars=tight_budget)

    assert "Latest user query" in truncated_tight
    # Neither part of the tool turn should be present
    assert "call_weather" not in truncated_tight
    assert "Sunny and 72F" not in truncated_tight


def test_parallel_tool_call_preservation() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Old query " + "x" * 150},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("call_1"), _tool_call("call_2"), _tool_call("call_3")],
        },
        {"role": "tool", "content": "res1", "tool_call_id": "call_1"},
        {"role": "tool", "content": "res2", "tool_call_id": "call_2"},
        {"role": "tool", "content": "res3", "tool_call_id": "call_3"},
        {"role": "user", "content": "Latest request"},
    ]
    req = make_request(messages)

    # When budget only fits the latest turn:
    # The parallel tool call turn consists of 4 messages (1 assistant + 3 tools).
    # Omitted count should be 1 (old query) + 4 (tool turn) = 5 messages!
    truncated = render_transcript(req, "test-model", max_chars=900)

    assert "Latest request" in truncated
    assert "[Earlier conversation history truncated: 5 messages omitted to fit context limits]" in truncated
    assert "call_1" not in truncated
    assert "res1" not in truncated
    assert "call_2" not in truncated
    assert "call_3" not in truncated


def test_guarantee_latest_turn_always_retained() -> None:
    # Budget smaller than even fixed overhead + latest turn
    req = make_request([{"role": "user", "content": "Important latest question"}])
    transcript = render_transcript(req, "test-model", max_chars=10)
    assert "Important latest question" in transcript
    assert _target_turn_marker(req) in transcript

    # Latest turn is a tool turn:
    req_tool = make_request(
        [
            {"role": "user", "content": "Earlier question " + "w" * 200},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call_latest")],
            },
            {"role": "tool", "content": "Latest tool output", "tool_call_id": "call_latest"},
        ]
    )
    transcript_tool = render_transcript(req_tool, "test-model", max_chars=10)
    assert "call_latest" in transcript_tool
    assert "Latest tool output" in transcript_tool
    assert "Earlier question" not in transcript_tool
    assert "[Earlier conversation history truncated: 1 messages omitted" in transcript_tool


def test_token_budget_computation_with_context_limit_and_output_tokens() -> None:
    req = make_request(
        [{"role": "user", "content": "Query " + "q" * 50}],
        max_tokens=2048,
    )
    # output_tokens on request is 2048
    assert req.output_tokens == 2048

    # limit_tokens = 5000, output_reserve = 2048 -> prompt_tokens = 2952 -> char_budget = 11808
    # A short transcript easily fits in 11808 chars
    t1 = render_transcript(req, "test-model", context_limit=5000)
    assert "[Earlier conversation history truncated" not in t1

    # Now make a long conversation that exceeds char_budget
    long_msgs: list[dict[str, Any]] = [
        {"role": "user", "content": f"msg {i} " + "k" * 200} for i in range(30)
    ]
    req_long = make_request(long_msgs, max_tokens=1000)
    # context_limit = 2000, output_reserve = 1000 -> prompt_tokens = 1000 -> char_budget = 4000 chars
    t2 = render_transcript(req_long, "test-model", context_limit=2000)
    assert len(t2) <= 4000
    assert "[Earlier conversation history truncated:" in t2


def test_token_budget_computation_fallback_defaults() -> None:
    # Neither max_chars nor context_limit provided: defaults to DEFAULT_CONTEXT_LIMIT_TOKENS
    # prompt_tokens = 128000 - 4096 = 123904 -> budget = 495616 chars
    req = make_request([{"role": "user", "content": "hello"}])
    t = render_transcript(req, "test-model")
    assert "[Earlier conversation history truncated" not in t

    # Floor at 1000 tokens when limit_tokens < output_reserve
    # limit = 2000, output = 4000 -> limit - output = -2000 -> floor = 1000 -> budget = 4000
    req_small = make_request(
        [{"role": "user", "content": f"msg {i} " + "v" * 150} for i in range(20)],
        max_tokens=4000,
    )
    t_floor = render_transcript(req_small, "test-model", context_limit=2000)
    assert len(t_floor) <= 4000
    assert "[Earlier conversation history truncated:" in t_floor


def test_model_catalog_observed() -> None:
    support = CompletionSupport(
        CompletionProfile("fake", "fake-cli", "arg", "fmt", "out"),
        supports_tools=True,
    )

    class DummyAdapter:
        id = "fake"

        def inspect(self, *, timeout_s: float = 10) -> CompletionSupport:
            return support

        def complete(
            self,
            request: CompletionRequest,
            *,
            cwd: Path,
            cancel: threading.Event,
            timeout_s: float,
            max_output_bytes: int,
            on_chunk: CompletionChunkSink | None = None,
        ) -> CompletionResult:
            return CompletionResult(content="ok", raw="ok")

    config = Config.model_validate(
        {
            "version": 1,
            "agents": {
                "fake": {
                    "cmd": "fake-cmd",
                    "models": {"m": "fake/m"},
                }
            },
        }
    )
    catalog = ModelCatalog(config, {"fake": DummyAdapter()}, models=["m"])
    model = catalog.models["fake/m"]

    # Before observation
    assert catalog.observed(model) is None

    # After observation added to _observed
    obs = ObservedModel("m", 32768, 4096)
    catalog._observed[("fake", "m")] = obs  # pyright: ignore[reportPrivateUsage]
    assert catalog.observed(model) == obs


def test_render_repair_sanitizes_malformed_function_call() -> None:
    raw_error = (
        "Antigravity CLI completion failed: exit code 0; error: Your previous response "
        "contained an improperly formatted function call\n"
        "Please retry with a properly formatted function call\n"
        "Retries remaining: 3"
    )
    prompt = render_repair("transcript\n", "invalid payload", raw_error)
    assert "Please retry with a properly formatted function call" not in prompt
    assert '"invalid_response":""' in prompt or '"invalid_response": ""' in prompt
    lines = prompt.splitlines()
    preamble = lines[1]
    record = json.loads(lines[-1])["repair"]
    assert record["invalid_response"] == ""
    assert "native host CLI function calling is disabled".lower() in preamble.lower()
    assert "native host function calling is disabled" in record["validation_error"]


def test_render_repair_preserves_standard_schema_errors() -> None:
    invalid = '{"result": 123}'
    error = 'ValidationError: missing required field "name"'
    prompt = render_repair("transcript\n", invalid, error)
    lines = prompt.splitlines()
    preamble = lines[1]
    record = json.loads(lines[-1])["repair"]
    assert record["invalid_response"] == invalid
    assert record["validation_error"] == error
    assert "The previous response failed output validation." in preamble
    assert "malformed function call" not in prompt
    assert "Native host CLI function calling is disabled" not in prompt

