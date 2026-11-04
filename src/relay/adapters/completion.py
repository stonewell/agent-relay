"""Restricted completion contracts, independent of HTTP and repository task execution."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class CompletionProfile:
    agent: str
    cli_version: str
    platform: str
    auth_mode: str
    config_digest: str


CompletionChunkSink = Callable[[str], None]
_CHUNK_SINK: ContextVar[CompletionChunkSink | None] = ContextVar("_CHUNK_SINK", default=None)


def current_chunk_sink() -> CompletionChunkSink | None:
    return _CHUNK_SINK.get()


@contextmanager  # pyright: ignore[reportDeprecated]
def stream_completion_chunks(sink: CompletionChunkSink | None) -> Iterator[None]:
    token = _CHUNK_SINK.set(sink)
    try:
        yield
    finally:
        _CHUNK_SINK.reset(token)


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
class CompletionModelOptions:
    """Internal per-model capabilities; hidden aliases remain explicitly routable."""

    reasoning_efforts: tuple[str, ...] = ()
    listed: bool = True


@dataclass(frozen=True)
class CompletionSupport:
    profile: CompletionProfile
    supports_tools: bool = False
    native_max_tokens: bool = False
    supports_reasoning_effort: bool = True
    model_options: Mapping[str, CompletionModelOptions] = field(
        default_factory=lambda: dict[str, CompletionModelOptions]()
    )


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


FIRST_OUTPUT_BASE_S = 120.0
"""Default first-output limit for a small prompt (seconds); ``Agent.first_output_timeout_s``."""
FIRST_OUTPUT_PER_100KB_S = 30.0
"""Seconds added to the first-output limit per 100 KiB of prompt (prefill and stdin feed)."""
IDLE_TIMEOUT_S = 300.0
"""Default limit on silence after the first output (seconds); ``Agent.idle_timeout_s``."""
_100_KIB = 100 * 1024


def silence_limits(
    prompt_bytes: int,
    remaining_s: float,
    *,
    first_output_s: float | None = None,
    idle_s: float | None = None,
    idle_default: float | None = IDLE_TIMEOUT_S,
) -> tuple[float | None, float | None]:
    """The ``(first_output, idle)`` no-output limits for one CLI run.

    The first-output limit is ``base + FIRST_OUTPUT_PER_100KB_S * prompt_bytes / 100 KiB``,
    where `first_output_s` overrides the base; the idle limit is `idle_s` or `idle_default`
    (:data:`IDLE_TIMEOUT_S`; None for backends whose events are too coarse to bound
    silence, so only an explicit `idle_s` applies). Both assume the CLI emits events while
    it works (every backend runs an event-stream output mode). A limit that would not
    expire before `remaining_s` is dropped (None): the request deadline already covers it.
    """
    base = FIRST_OUTPUT_BASE_S if first_output_s is None else first_output_s
    idle = idle_default if idle_s is None else idle_s
    first = base + FIRST_OUTPUT_PER_100KB_S * prompt_bytes / _100_KIB
    return (
        first if first < remaining_s else None,
        idle if idle is not None and idle < remaining_s else None,
    )


def stalled_error(backend: str, limit: str, silent_s: float, prompt_bytes: int) -> CompletionError:
    """Public 504 for a watchdog kill: which limit fired, silence and prompt size, no content."""
    return CompletionError(
        f"{backend} CLI produced no output for {silent_s:.1f}s ({limit} limit); "
        f"prompt {prompt_bytes} bytes",
        status=504,
        code="timeout",
    )


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
