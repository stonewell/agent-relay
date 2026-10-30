"""Strict Chat Completions subset and client-owned history validation."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        param: str | None = None,
        code: str | None = None,
        error_type: str = "invalid_request_error",
    ) -> None:
        super().__init__(message)
        self.status, self.message, self.param = status, message, param
        self.code, self.error_type = code, error_type

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class TextPart(Strict):
    type: Literal["text"]
    text: str


class FunctionCall(Strict):
    name: str = Field(pattern=NAME.pattern)
    arguments: str

    @model_validator(mode="after")
    def arguments_object(self) -> FunctionCall:
        try:
            value = json.loads(self.arguments, parse_constant=_bad_constant)
            check_json_values(value)
        except (ValueError, RecursionError) as exc:
            raise ValueError("function arguments must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("function arguments must be a JSON object")
        return self


class HistoryCall(Strict):
    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"]
    function: FunctionCall


class Message(Strict):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[TextPart] | None = None
    tool_calls: list[HistoryCall] | None = None
    tool_call_id: str | None = None
    name: str | None = Field(default=None, pattern=NAME.pattern)

    def text(self) -> str | None:
        if isinstance(self.content, list):
            return "".join(part.text for part in self.content)
        return self.content

    @model_validator(mode="after")
    def role_fields(self) -> Message:
        if self.role != "assistant" and self.tool_calls is not None:
            raise ValueError("only assistant messages may contain tool_calls")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("only tool messages may contain tool_call_id")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages require tool_call_id")
        if self.content is None and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("content is required unless assistant has tool_calls")
        return self


class FunctionDefinition(Strict):
    name: str = Field(pattern=NAME.pattern)
    description: str | None = None
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    strict: bool | None = None


class Tool(Strict):
    type: Literal["function"]
    function: FunctionDefinition


class NamedFunction(Strict):
    name: str = Field(pattern=NAME.pattern)


class NamedChoice(Strict):
    type: Literal["function"]
    function: NamedFunction


class StreamOptions(Strict):
    include_usage: bool = False


class ChatRequest(Strict):
    model: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1, max_length=10000)
    tools: list[Tool] | None = Field(default=None, max_length=128)
    tool_choice: Literal["auto", "none", "required"] | NamedChoice = "auto"
    parallel_tool_calls: bool = True
    stream: bool = False
    stream_options: StreamOptions | None = None
    n: int = Field(default=1, ge=1, le=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    response_format: dict[str, Any] | None = None

    @model_validator(mode="after")
    def consistency(self) -> ChatRequest:
        names = [tool.function.name for tool in self.tools or []]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        if self.tool_choice == "required" and not names:
            raise ValueError("required tool_choice requires tools")
        if (
            isinstance(self.tool_choice, NamedChoice)
            and self.tool_choice.function.name not in names
        ):
            raise ValueError("named tool_choice must name an offered tool")
        if (
            self.max_tokens is not None
            and self.max_completion_tokens is not None
            and self.max_tokens != self.max_completion_tokens
        ):
            raise ValueError("max_tokens conflicts with max_completion_tokens")
        if self.response_format is not None and self.response_format != {"type": "text"}:
            raise ValueError("only text response_format is supported")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream")
        pending: set[str] = set()
        seen: set[str] = set()
        for message in self.messages:
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("unknown or duplicate tool result id")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise ValueError("all tool calls require results before another turn")
                for call in message.tool_calls or []:
                    if call.id in seen:
                        raise ValueError("assistant tool call ids must be unique")
                    pending.add(call.id)
                    seen.add(call.id)
        if pending:
            raise ValueError("dangling assistant tool calls")
        return self

    @property
    def output_tokens(self) -> int | None:
        return self.max_completion_tokens or self.max_tokens


def _bad_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def check_json_values(value: Any) -> None:
    """Bound JSON structure and reject non-finite numbers and invalid Unicode scalars."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise ValueError("JSON exceeds nesting or 100000-node complexity limit")
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError("JSON numbers must be finite")
        if isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise ValueError("JSON strings must contain valid Unicode scalar values") from exc
        if isinstance(current, dict):
            for key, child in cast("dict[str, Any]", current).items():
                pending.append((key, depth + 1))
                pending.append((child, depth + 1))
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in cast("list[Any]", current))


def parse_request(value: Any) -> ChatRequest:
    try:
        check_json_values(value)
    except ValueError as exc:
        raise GatewayError(400, str(exc)) from exc
    if isinstance(value, dict):
        # Unsupported explicit null fields are harmless; non-null fields are errors.
        fields = cast("dict[str, Any]", value)
        value = {k: v for k, v in fields.items() if v is not None or k in ChatRequest.model_fields}
    try:
        return ChatRequest.model_validate(value)
    except ValidationError as exc:
        issue = exc.errors(include_input=False)[0]
        param = ".".join(str(part) for part in issue["loc"]) or None
        if param is None:
            for field in (
                "response_format",
                "max_tokens",
                "stream_options",
                "tool_choice",
                "tool names",
            ):
                if field in issue["msg"]:
                    param = "tools" if field == "tool names" else field
                    break
        raise GatewayError(400, issue["msg"], param) from exc
