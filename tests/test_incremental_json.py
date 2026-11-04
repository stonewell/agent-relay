from __future__ import annotations

import http.client
import json
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from relay.adapters import claude_completion
from relay.adapters.completion import (
    CompletionRequest,
    CompletionResult,
    CompletionUsage,
    current_chunk_sink,
)
from relay.adapters.json_stream import IncrementalJsonContentDecoder
from relay.gateway.app import create_app
from test_gateway_app import local_server
from test_gateway_models import FakeBackend, configuration

# ---------------------------------------------------------------------------
# Part 1: IncrementalJsonContentDecoder Unit Tests
# ---------------------------------------------------------------------------


def test_decoder_standard_escapes_single_chunk() -> None:
    payload = json.dumps(
        {
            "content": 'Quote: "back\\slash", slash: /, bell: \b, form: \f, newline: \n, ret: \r, tab: \t',
            "tool_calls": [],
        }
    )
    decoder = IncrementalJsonContentDecoder()
    result = decoder.feed(payload)
    assert (
        result
        == 'Quote: "back\\slash", slash: /, bell: \b, form: \f, newline: \n, ret: \r, tab: \t'
    )
    assert decoder.is_done


def test_decoder_standard_escapes_char_by_char() -> None:
    payload = json.dumps(
        {
            "content": 'Line1\nLine2\tTabbed\\Escaped"Quoted"',
            "tool_calls": [],
        }
    )
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in payload]
    assert "".join(chunks) == 'Line1\nLine2\tTabbed\\Escaped"Quoted"'
    assert decoder.is_done


def test_decoder_escaped_backslash_before_escape_char() -> None:
    # "\\n" in content is literal backslash followed by 'n', not newline
    raw = r'{"content": "literal \\n not newline"}'
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in raw]
    assert "".join(chunks) == r"literal \n not newline"
    assert decoder.is_done


def test_decoder_unicode_escapes_basic_and_1char_chunks() -> None:
    # \u0041 = 'A', \u00e9 = 'é', \u2603 = '☃', \u96ea = '雪'
    raw = r'{"content": "Letter \u0041, accent \u00e9, snowman \u2603, kanji \u96ea"}'
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in raw]
    assert "".join(chunks) == "Letter A, accent é, snowman ☃, kanji 雪"
    assert decoder.is_done


@pytest.mark.parametrize(
    ("split_index", "expected"),
    [
        (0, "A"),
        (1, "A"),
        (2, "A"),
        (3, "A"),
        (4, "A"),
        (5, "A"),
    ],
)
def test_decoder_unicode_escape_split_boundaries(split_index: int, expected: str) -> None:
    # Split r"\u0041" across chunk boundaries at every offset
    escape_str = r"\u0041"
    part1 = f'{{"content": "{escape_str[:split_index]}'
    part2 = f'{escape_str[split_index:]}"}}'
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(part1)
    out2 = decoder.feed(part2)
    assert out1 + out2 == expected
    assert decoder.is_done


def test_decoder_unicode_escape_invalid_hex() -> None:
    # \u12z4 has non-hex 'z' -> emits replacement char and continues
    raw = r'{"content": "bad \u12z4 end"}'
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in raw]
    assert "".join(chunks) == "bad \ufffdz4 end"
    assert decoder.is_done


def test_decoder_surrogate_pair_single_chunk() -> None:
    # \uD83D\uDE00 is 😀 (U+1F600)
    raw = r'{"content": "Smile \uD83D\uDE00!"}'
    decoder = IncrementalJsonContentDecoder()
    assert decoder.feed(raw) == "Smile 😀!"
    assert decoder.is_done


def test_decoder_surrogate_pair_char_by_char() -> None:
    raw = r'{"content": "\uD83D\uDE00 and \uD83C\uDF89"}'
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in raw]
    assert "".join(chunks) == "😀 and 🎉"
    assert decoder.is_done


def test_decoder_surrogate_pair_split_between_high_and_low() -> None:
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(r'{"content": "hello \uD83D')
    assert out1 == "hello "
    out2 = decoder.feed(r'\uDE00 world"}')
    assert out2 == "😀 world"
    assert decoder.is_done


def test_decoder_surrogate_pair_split_inside_high_surrogate() -> None:
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(r'{"content": "\uD8')
    assert out1 == ""
    out2 = decoder.feed(r'3D\uDE00"}')
    assert out2 == "😀"
    assert decoder.is_done


def test_decoder_surrogate_pair_split_inside_low_surrogate() -> None:
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(r'{"content": "\uD83D\uDE')
    assert out1 == ""
    out2 = decoder.feed(r'00"}')
    assert out2 == "😀"
    assert decoder.is_done


def test_decoder_isolated_high_surrogate_followed_by_regular_text() -> None:
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(r'{"content": "\uD83D')
    assert out1 == ""
    out2 = decoder.feed('abc"}')
    assert out2 == "\ufffdabc"
    assert decoder.is_done


def test_decoder_isolated_high_surrogate_followed_by_another_high_surrogate() -> None:
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(r'{"content": "\uD83D')
    assert out1 == ""
    # Second surrogate pair is valid party popper 🎉
    out2 = decoder.feed(r'\uD83C\uDF89"}')
    assert out2 == "\ufffd🎉"
    assert decoder.is_done


def test_decoder_isolated_high_surrogate_at_end_of_string() -> None:
    decoder = IncrementalJsonContentDecoder()
    out1 = decoder.feed(r'{"content": "\uD83D')
    assert out1 == ""
    out2 = decoder.feed('"}')
    assert out2 == "\ufffd"
    assert decoder.is_done


def test_decoder_isolated_low_surrogate() -> None:
    raw = r'{"content": "\uDE00 alone"}'
    decoder = IncrementalJsonContentDecoder()
    assert decoder.feed(raw) == "\ufffd alone"
    assert decoder.is_done


def test_decoder_direct_unicode_characters() -> None:
    raw = '{"content": "Hello 世界 🌍"}'
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in raw]
    assert "".join(chunks) == "Hello 世界 🌍"
    assert decoder.is_done


def test_decoder_whitespace_around_tokens() -> None:
    raw = '  \n\t { \t "tool_calls" :  [ ] , \n "content" : \t "spaced out" \n }  '
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in raw]
    assert "".join(chunks) == "spaced out"
    assert decoder.is_done


def test_decoder_empty_chunks_and_calls_after_done() -> None:
    decoder = IncrementalJsonContentDecoder()
    assert decoder.feed("") == ""
    assert decoder.feed('{"content": "done"}') == "done"
    assert decoder.is_done
    # Feeds after done return empty string
    assert decoder.feed("") == ""
    assert decoder.feed("trailing") == ""


def test_decoder_key_ordering_tool_calls_before_content() -> None:
    payload = json.dumps(
        {
            "tool_calls": [{"name": "calculator", "arguments": {"expr": "1+1"}}],
            "content": "The result is 2.",
        }
    )
    decoder = IncrementalJsonContentDecoder()
    result = decoder.feed(payload)
    assert result == "The result is 2."
    assert decoder.is_done


def test_decoder_key_ordering_content_before_tool_calls() -> None:
    payload = json.dumps(
        {
            "content": "Executing calculator...",
            "tool_calls": [{"name": "calculator", "arguments": {"expr": "2*2"}}],
        }
    )
    decoder = IncrementalJsonContentDecoder()
    result = decoder.feed(payload)
    assert result == "Executing calculator..."
    assert decoder.is_done


def test_decoder_extra_keys_surrounding_content() -> None:
    payload = (
        '{"id": "msg_123", "tool_calls": [], "status": 200, "content": "hello world", "more": {}}'
    )
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in payload]
    assert "".join(chunks) == "hello world"
    assert decoder.is_done


def test_decoder_null_content_with_tool_calls() -> None:
    payload1 = json.dumps(
        {
            "content": None,
            "tool_calls": [{"name": "search", "arguments": {"query": "pytest"}}],
        }
    )
    decoder1 = IncrementalJsonContentDecoder()
    assert decoder1.feed(payload1) == ""
    assert decoder1.is_done

    payload2 = json.dumps(
        {
            "tool_calls": [{"name": "search", "arguments": {"query": "pytest"}}],
            "content": None,
        }
    )
    decoder2 = IncrementalJsonContentDecoder()
    chunks = [decoder2.feed(c) for c in payload2]
    assert "".join(chunks) == ""
    assert decoder2.is_done


def test_decoder_non_string_content_is_safely_ignored() -> None:
    payload_array = '{"content": [1, 2, 3], "tool_calls": []}'
    decoder = IncrementalJsonContentDecoder()
    assert decoder.feed(payload_array) == ""
    assert decoder.is_done

    payload_num = '{"content": 42, "tool_calls": []}'
    decoder2 = IncrementalJsonContentDecoder()
    assert decoder2.feed(payload_num) == ""
    assert decoder2.is_done


def test_decoder_empty_string_content() -> None:
    payload = '{"content": "", "tool_calls": []}'
    decoder = IncrementalJsonContentDecoder()
    assert decoder.feed(payload) == ""
    assert decoder.is_done


def test_decoder_nested_objects_and_arrays_with_content_key() -> None:
    payload = json.dumps(
        {
            "tool_calls": [
                {
                    "name": "edit_file",
                    "arguments": {
                        "content": "internal file content with { braces } and [ brackets ]",
                        "options": {"content": "nested content here"},
                    },
                },
                {
                    "name": "log_event",
                    "arguments": {
                        "nested_json_str": '{"content": "quoted json string inside arg"}',
                    },
                },
            ],
            "content": "Real top-level assistant content.",
        }
    )
    decoder = IncrementalJsonContentDecoder()
    chunks = [decoder.feed(c) for c in payload]
    assert "".join(chunks) == "Real top-level assistant content."
    assert decoder.is_done


def test_claude_stream_sink_structured_mode() -> None:
    chunks: list[str] = []
    sink = claude_completion._stream_sink(chunks.append, structured=True)  # pyright: ignore[reportPrivateUsage]

    # stream_event with input_json_delta feeding structured JSON chunks
    sink(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"content": "Thinking step by step. ',
                    },
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
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": 'Ready to call tool.", "tool_calls": []}',
                    },
                },
            }
        )
    )
    # assistant snapshots must be ignored in structured mode
    sink(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Snapshots should not emit"}]},
            }
        )
    )
    assert chunks == ["Thinking step by step. ", "Ready to call tool."]


def test_claude_stream_sink_structured_mode_null_content() -> None:
    chunks: list[str] = []
    sink = claude_completion._stream_sink(chunks.append, structured=True)  # pyright: ignore[reportPrivateUsage]

    sink(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"content": null, "tool_calls": [{"name": "func"}]}',
                    },
                },
            }
        )
    )
    assert chunks == []


# ---------------------------------------------------------------------------
# Part 2: Gateway & Integration Tests
# ---------------------------------------------------------------------------


def test_gateway_streaming_tool_bearing_live_deltas_real_http() -> None:
    first_chunk_received = threading.Event()

    class LiveToolStreamingBackend(FakeBackend):
        def complete(
            self,
            request: CompletionRequest,
            *,
            cwd: Any,
            cancel: threading.Event,
            timeout_s: float,
            max_output_bytes: int,
        ) -> CompletionResult:
            self.calls.append(request)
            sink = current_chunk_sink()
            assert sink is not None
            sink("Analyzing request... ")
            # Wait until client has received the first chunk live before proceeding
            assert first_chunk_received.wait(3)
            sink("Querying database.")
            return CompletionResult(
                envelope={
                    "content": "Analyzing request... Querying database.",
                    "tool_calls": [
                        {"name": "sql_query", "arguments": {"query": "SELECT * FROM users"}}
                    ],
                },
                content="Analyzing request... Querying database.",
            )

    backend = LiveToolStreamingBackend()
    with local_server(backend) as (port, _runtime):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "model": "fake",
                    "stream": True,
                    "messages": [{"role": "user", "content": "Show all users"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "sql_query",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"query": {"type": "string"}},
                                    "required": ["query"],
                                },
                            },
                        }
                    ],
                }
            ),
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert "text/event-stream" in response.getheader("Content-Type", "")

        # 1. Role chunk
        line1 = response.readline()
        response.readline()
        chunk1 = json.loads(line1.removeprefix(b"data: "))
        assert chunk1["choices"][0]["delta"] == {"role": "assistant"}

        # 2. First live content chunk arrives BEFORE backend finishes
        line2 = response.readline()
        response.readline()
        chunk2 = json.loads(line2.removeprefix(b"data: "))
        assert chunk2["choices"][0]["delta"] == {"content": "Analyzing request... "}

        # Unblock backend to emit second chunk and return
        first_chunk_received.set()

        # Read remaining stream
        remainder = response.read().decode("utf-8")
        connection.close()

        # Parse remainder SSE frames
        frames = [
            json.loads(line.removeprefix("data: "))
            for line in remainder.split("\n")
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

        # Remainder content frame
        content_frames = [f for f in frames if "content" in f["choices"][0]["delta"]]
        assert len(content_frames) == 1
        assert content_frames[0]["choices"][0]["delta"]["content"] == "Querying database."

        # Tool call frame
        tool_frames = [f for f in frames if "tool_calls" in f["choices"][0]["delta"]]
        assert len(tool_frames) == 1
        tool_call = tool_frames[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_call["index"] == 0
        assert tool_call["type"] == "function"
        assert tool_call["function"]["name"] == "sql_query"
        assert tool_call["function"]["arguments"] == '{"query":"SELECT * FROM users"}'
        assert tool_call["id"].startswith("call_")

        # Finish reason frame
        finish_frames = [f for f in frames if f["choices"][0]["finish_reason"] is not None]
        assert len(finish_frames) == 1
        assert finish_frames[0]["choices"][0]["finish_reason"] == "tool_calls"

        assert "data: [DONE]\n\n" in remainder


def test_gateway_streaming_tool_bearing_null_content_clean_buffering() -> None:
    backend = FakeBackend()
    backend.results = [
        CompletionResult(
            envelope={
                "content": None,
                "tool_calls": [
                    {
                        "name": "lookup",
                        "arguments": {"key": "settings"},
                    }
                ],
            },
            content=None,
        )
    ]
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake",
                "stream": True,
                "messages": [{"role": "user", "content": "lookup settings"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                            },
                        },
                    }
                ],
            },
        )
        assert resp.status_code == 200
        raw_lines = [
            line
            for line in resp.text.split("\n")
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        events = [json.loads(line.removeprefix("data: ")) for line in raw_lines]

        # Verify no spurious content deltas appear
        content_deltas = [
            ev["choices"][0]["delta"].get("content")
            for ev in events
            if "content" in ev["choices"][0]["delta"]
        ]
        assert content_deltas == []

        # Event 1: role: assistant
        assert events[0]["choices"][0]["delta"] == {"role": "assistant"}

        # Event 2: tool_calls with index: 0
        tool_call_delta = events[1]["choices"][0]["delta"]["tool_calls"][0]
        assert tool_call_delta["index"] == 0
        assert tool_call_delta["type"] == "function"
        assert tool_call_delta["function"]["name"] == "lookup"
        assert tool_call_delta["function"]["arguments"] == '{"key":"settings"}'
        assert tool_call_delta["id"].startswith("call_")

        # Event 3: finish_reason: tool_calls
        assert events[2]["choices"][0]["finish_reason"] == "tool_calls"
        assert events[2]["choices"][0]["delta"] == {}


def test_gateway_streaming_multiple_tool_calls_indices() -> None:
    backend = FakeBackend()
    backend.results = [
        CompletionResult(
            envelope={
                "content": "Running batch tools",
                "tool_calls": [
                    {"name": "fetch", "arguments": {"id": 1}},
                    {"name": "fetch", "arguments": {"id": 2}},
                    {"name": "fetch", "arguments": {"id": 3}},
                ],
            },
            content="Running batch tools",
        )
    ]
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake",
                "stream": True,
                "messages": [{"role": "user", "content": "batch"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "fetch",
                            "parameters": {
                                "type": "object",
                                "properties": {"id": {"type": "integer"}},
                            },
                        },
                    }
                ],
            },
        )
        assert resp.status_code == 200
        raw_lines = [
            line
            for line in resp.text.split("\n")
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        events = [json.loads(line.removeprefix("data: ")) for line in raw_lines]

        tool_deltas = [
            ev["choices"][0]["delta"]["tool_calls"][0]
            for ev in events
            if "tool_calls" in ev["choices"][0]["delta"]
        ]
        assert len(tool_deltas) == 3
        for idx, call in enumerate(tool_deltas):
            assert call["index"] == idx
            assert call["function"]["arguments"] == f'{{"id":{idx + 1}}}'

        finish_event = [ev for ev in events if ev["choices"][0]["finish_reason"] is not None][0]
        assert finish_event["choices"][0]["finish_reason"] == "tool_calls"


def test_gateway_streaming_tool_bearing_with_usage_included() -> None:
    backend = FakeBackend()
    backend.results = [
        CompletionResult(
            envelope={
                "content": "Done",
                "tool_calls": [{"name": "act", "arguments": {}}],
            },
            content="Done",
            usage=CompletionUsage(prompt_tokens=42, completion_tokens=15),
        )
    ]
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "act"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "act", "parameters": {"type": "object"}},
                    }
                ],
            },
        )
        assert resp.status_code == 200
        raw_lines = [
            line
            for line in resp.text.split("\n")
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        events = [json.loads(line.removeprefix("data: ")) for line in raw_lines]

        # The last chunk before [DONE] should be the usage event
        usage_chunk = events[-1]
        assert usage_chunk["choices"] == []
        assert usage_chunk["usage"] == {
            "prompt_tokens": 42,
            "completion_tokens": 15,
            "total_tokens": 57,
        }


def test_gateway_streaming_repair_retry_suppresses_chunks() -> None:
    class RepairBackend(FakeBackend):
        def complete(
            self,
            request: CompletionRequest,
            *,
            cwd: Any,
            cancel: threading.Event,
            timeout_s: float,
            max_output_bytes: int,
        ) -> CompletionResult:
            sink = current_chunk_sink()
            attempt = len(self.calls)
            self.calls.append(request)
            if attempt == 0:
                # Attempt 0: stream_chunks should be True
                assert sink is not None
                return CompletionResult(content="bad raw envelope", raw="bad raw envelope")
            # Attempt 1 (repair): stream_chunks should be False
            assert sink is None
            return CompletionResult(
                envelope={
                    "content": "Repaired content",
                    "tool_calls": [{"name": "search", "arguments": {}}],
                },
                content="Repaired content",
                raw='{"content": "Repaired content", "tool_calls": [{"name": "search", "arguments": {}}]}',
            )

    backend = RepairBackend()
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "fake",
                "stream": True,
                "messages": [{"role": "user", "content": "search"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "search", "parameters": {"type": "object"}},
                    }
                ],
            },
        )
        assert resp.status_code == 200
        raw_lines = [
            line
            for line in resp.text.split("\n")
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        events = [json.loads(line.removeprefix("data: ")) for line in raw_lines]

        assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert events[1]["choices"][0]["delta"] == {"content": "Repaired content"}
        assert events[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "search"
        assert events[3]["choices"][0]["finish_reason"] == "tool_calls"
