"""Restricted completion contracts, independent of HTTP and repository task execution."""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class CompletionProfile:
    agent: str
    cli_version: str
    platform: str
    auth_mode: str
    config_digest: str


DEFAULT_MODEL_ALIASES: tuple[str, ...] = ("default", "sonnet", "opus", "fable")
"""CLI model aliases the gateway offers unless the operator passes ``--model``.

``default`` launches without ``--model``. Limits are never listed here: they are observed
from the CLI's own report when an alias is probed (``relay.gateway.discovery``).
"""

# Starts alphanumeric so an alias can never be read as another CLI flag.
_MODEL_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,127}")


def model_aliases(values: Iterable[str] | None = None) -> tuple[str, ...]:
    """Validated, de-duplicated aliases in the given order; the defaults when none given."""
    aliases: list[str] = []
    for value in DEFAULT_MODEL_ALIASES if values is None else values:
        if not _MODEL_ALIAS.fullmatch(value):
            raise ValueError(f"invalid model alias: {value!r}")
        if value not in aliases:
            aliases.append(value)
    if not aliases:
        raise ValueError("at least one model alias is required")
    return tuple(aliases)


@dataclass(frozen=True)
class CompletionSupport:
    profile: CompletionProfile
    supports_tools: bool = False
    native_max_tokens: bool = False
    supports_reasoning_effort: bool = True


@dataclass(frozen=True)
class CompletionRequest:
    prompt: str
    model: str = "default"
    effort: str | None = None
    output_schema: dict[str, Any] | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class CompletionUsage:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None
    cache_write_tokens: int | None = None

    def as_openai(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }
        if self.cached_tokens is not None:
            value["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}
        if self.cache_write_tokens is not None:
            value["relay"] = {"cache_write_tokens": self.cache_write_tokens}
        return value


def aggregate_usage(
    first: CompletionUsage | None, second: CompletionUsage | None
) -> CompletionUsage | None:
    if first is None or second is None:
        return None

    def add(left: int | None, right: int | None) -> int | None:
        return None if left is None or right is None else left + right

    return CompletionUsage(
        first.prompt_tokens + second.prompt_tokens,
        first.completion_tokens + second.completion_tokens,
        add(first.cached_tokens, second.cached_tokens),
        add(first.cache_write_tokens, second.cache_write_tokens),
    )


@dataclass(frozen=True)
class ObservedModel:
    """The model a CLI reported running, with the limits the CLI itself reported for it."""

    model: str
    context_length: int
    max_output_tokens: int


@dataclass(frozen=True)
class CompletionResult:
    content: str | None = None
    envelope: dict[str, Any] | None = None
    finish_reason: Literal["stop", "length"] = "stop"
    usage: CompletionUsage | None = None
    raw: str = ""
    observed: ObservedModel | None = None


class CompletionError(RuntimeError):
    """Public, sanitized error. Raw provider stderr must never be the message."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 502,
        code: str = "completion_failed",
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.param = param


class CompletionInvalid(CompletionError):
    def __init__(
        self, message: str, *, raw: str = "", usage: CompletionUsage | None = None
    ) -> None:
        super().__init__(message, code="invalid_completion")
        self.raw = raw
        self.usage = usage


class CompletionTerminationError(CompletionError):
    """Child termination/drain cannot be proved; capacity must be quarantined."""

    def __init__(self) -> None:
        super().__init__(
            "Completion process termination is unconfirmed", code="termination_unknown"
        )


@runtime_checkable
class CompletionCapable(Protocol):
    id: str

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport: ...

    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult: ...
