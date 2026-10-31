"""Lossless buffered Chat Completions response formatting."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .render import canonical


def completion_response(
    completion_id: str,
    created: int,
    model: str,
    content: str | None,
    calls: list[dict[str, Any]],
    finish_reason: str,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = [
            {
                "id": f"call_{completion_id.removeprefix('chatcmpl-')}_{index}",
                "type": "function",
                "function": {"name": call["name"], "arguments": canonical(call["arguments"])},
            }
            for index, call in enumerate(calls)
        ]
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else finish_reason,
            }
        ],
        "usage": usage,
    }


def event(value: dict[str, Any]) -> bytes:
    return ("data: " + canonical(value) + "\n\n").encode("utf-8")


def heartbeat_event(completion_id: str, created: int, model: str) -> bytes:
    """Protocol-visible stream heartbeat; OpenAI clients ignore SSE comments.

    An empty-delta ``chat.completion.chunk`` resets the client's stall timer without
    contributing assistant text. No ``finish_reason`` key is emitted so intermediate
    frames are distinguishable from terminal/error frames in raw HTTP tests.
    """
    return event(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}}],
        }
    )


def chunk_event(
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish: str | None = None,
) -> bytes:
    return event(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def usage_event(
    completion_id: str,
    created: int,
    model: str,
    usage: dict[str, Any],
) -> bytes:
    return event(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": usage,
        }
    )


def response_events(response: dict[str, Any], include_usage: bool) -> Iterator[bytes]:
    completion_id = response["id"]
    created = response["created"]
    model = response["model"]

    def chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
        return chunk_event(completion_id, created, model, delta, finish)

    yield chunk({"role": "assistant"})
    choice = response["choices"][0]
    message = choice["message"]
    if message["content"] is not None:
        yield chunk({"content": message["content"]})
    for index, call in enumerate(message.get("tool_calls", [])):
        yield chunk({"tool_calls": [{"index": index, **call}]})
    yield chunk({}, choice["finish_reason"])
    if include_usage and response["usage"] is not None:
        yield usage_event(completion_id, created, model, response["usage"])
    yield b"data: [DONE]\n\n"
