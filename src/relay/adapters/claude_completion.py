"""Claude tool-free completion backend.

This is separate from ClaudeAdapter's repository worker and coordinator paths. The only
streaming exposed by this backend is buffered HTTP streaming after final JSON validation.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from relay.adapters.claude import COORDINATOR_FLAGS
from relay.adapters.completion import (
    DEFAULT_MODEL_ALIASES,
    CompletionError,
    CompletionInvalid,
    CompletionProfile,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    CompletionTerminationError,
    CompletionUsage,
    ObservedModel,
)
from relay.core.env import CompleteEnv, build_env, redact
from relay.core.platform.proc import (
    CommandNotFound,
    OutputLimitExceeded,
    ProcessCancelled,
    ProcessDrainError,
    ProcessResult,
    cancel_processes_when,
    resolve_command,
    run_process,
    stream_output,
)
from relay.core.registry import Agent


def _capture(_: str) -> None:
    """Enable bounded capture without forwarding CLI display/progress as assistant content."""


discard_output = _capture

# CLI diagnostics that indicate provider rate limiting; matched case-insensitively.
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "429",
    "rate limit",
    "rate-limit",
    "too many requests",
    "quota",
)


class ClaudeCompletionAdapter:
    id = "claude"
    default_aliases = DEFAULT_MODEL_ALIASES

    def __init__(
        self,
        agent: Agent,
        *,
        parent_env: Mapping[str, str] | None = None,
    ) -> None:
        self._agent = agent
        self._parent_env = None if parent_env is None else dict(parent_env)

    def _environment(self) -> CompleteEnv:
        parent = os.environ if self._parent_env is None else self._parent_env
        # User-configured credentials take precedence over inherited credentials.
        extra: dict[str, str] = {}
        if "ANTHROPIC_API_KEY" not in self._agent.env and parent.get("ANTHROPIC_API_KEY"):
            extra["ANTHROPIC_API_KEY"] = parent["ANTHROPIC_API_KEY"]
        return build_env(self._agent, extra, parent_env=parent)

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport:
        return CompletionSupport(
            profile=CompletionProfile(
                agent=self.id,
                cli_version="unknown",
                platform=sys.platform,
                auth_mode="unknown",
                config_digest="none",
            ),
            supports_tools=True,
            native_max_tokens=False,
        )

    @staticmethod
    def arguments(request: CompletionRequest) -> tuple[str, ...]:
        args = ["-p", "--output-format", "json", *COORDINATOR_FLAGS]
        if request.output_schema is not None:
            args += ["--json-schema", json.dumps(request.output_schema, separators=(",", ":"))]
        if request.model != "default":
            args += ["--model", request.model]
        if request.effort is not None:
            args += ["--effort", request.effort]
        return tuple(args)

    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        deadline = time.monotonic() + timeout_s
        try:
            with cancel_processes_when(cancel), stream_output(_capture, max_bytes=max_output_bytes):
                env = self._environment()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                command = resolve_command(
                    self._agent.cmd, self.arguments(request), env=env, cwd=cwd
                )
                proc = run_process(
                    command, cwd=cwd, env=env, stdin=request.prompt, timeout_s=remaining
                )
            return parse_completion(
                proc, structured=request.output_schema is not None, secrets=env
            )
        except CommandNotFound as exc:
            message = (
                f"Claude CLI not found: command {self._agent.cmd!r} (resolved via PATH); "
                "check agents.claude.cmd in ~/.config/relay/agents.yaml"
            )
            print(message, file=sys.stderr)
            raise CompletionError(message, status=503, code="unavailable") from exc
        except ProcessCancelled as exc:
            raise CompletionError(
                "Completion cancelled", status=503, code="request_cancelled"
            ) from exc
        except OutputLimitExceeded as exc:
            raise CompletionError(
                "Completion output limit exceeded", code="output_limit_exceeded"
            ) from exc
        except ProcessDrainError as exc:
            raise CompletionTerminationError() from exc


def _usage(payload: dict[str, Any]) -> CompletionUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    raw = cast("dict[str, Any]", raw)
    values: list[int] = []
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    inputs, outputs, reads, writes = values
    return CompletionUsage(inputs + reads + writes, outputs, reads, writes)


def _positive(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _observed(payload: dict[str, Any]) -> ObservedModel | None:
    """The model that ran and the limits the CLI reported for it, from ``modelUsage``.

    Only an unambiguous single entry with positive integer limits counts; anything else
    is reported as unknown rather than guessed.
    """
    raw = payload.get("modelUsage")
    if not isinstance(raw, dict) or len(cast("dict[str, Any]", raw)) != 1:
        return None
    ((model, entry),) = cast("dict[str, Any]", raw).items()
    if not model.strip() or not isinstance(entry, dict):
        return None
    entry = cast("dict[str, Any]", entry)
    context, output = _positive(entry.get("contextWindow")), _positive(entry.get("maxOutputTokens"))
    if context is None or output is None:
        return None
    return ObservedModel(model, context, output)


def parse_completion(
    proc: ProcessResult, *, structured: bool, secrets: Mapping[str, str] | None = None
) -> CompletionResult:
    if proc.timed_out:
        raise CompletionError("Completion deadline expired", status=504, code="timeout")
    payload: dict[str, Any] | None = None
    candidates = [proc.stdout, *reversed(proc.stdout.splitlines())]
    for text in candidates:
        try:
            parsed: Any = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict) and cast("dict[str, Any]", parsed).get("type") == "result":
            payload = cast("dict[str, Any]", parsed)
            break
    if proc.exit_code != 0 or payload is not None and payload.get("is_error"):
        # The category may use provider diagnostics; the diagnostic text never crosses HTTP.
        diagnostic = (proc.stderr + " " + (json.dumps(payload) if payload else "")).lower()
        if any(
            word in diagnostic
            for word in ("context_length_exceeded", "prompt is too long", "context window")
        ):
            raise CompletionError(
                "Context length exceeded",
                status=400,
                code="context_length_exceeded",
                param="messages",
            )
        message = (
            f"Claude CLI completion failed: exit code {proc.exit_code}; "
            f"stderr: {redact(proc.stderr[-2000:], secrets=secrets)}"
        )
        print(message, file=sys.stderr)
        if any(word in diagnostic for word in RATE_LIMIT_MARKERS):
            raise CompletionError(message, status=429, code="rate_limit_exceeded")
        raise CompletionError(message)
    if payload is None:
        message = (
            f"Claude CLI returned no completion object: exit code {proc.exit_code}; "
            f"stderr: {redact(proc.stderr[-2000:], secrets=secrets)}"
        )
        print(message, file=sys.stderr)
        raise CompletionInvalid(message, raw=proc.stdout[:4000])
    usage = _usage(payload)
    observed = _observed(payload)
    length = payload.get("stop_reason") in ("max_tokens", "length")
    if structured:
        if length:
            raise CompletionError(
                "Tool output reached its output limit", code="incomplete_tool_output"
            )
        candidate: Any = payload.get("structured_output")
        if candidate is None and isinstance(payload.get("result"), str):
            with contextlib.suppress(ValueError, RecursionError):
                candidate = json.loads(payload["result"])
        if not isinstance(candidate, dict):
            raise CompletionInvalid(
                "CLI returned no tool envelope",
                raw=str(payload.get("result", ""))[:4000],
                usage=usage,
            )
        envelope = cast("dict[str, Any]", candidate)
        return CompletionResult(
            envelope=envelope, usage=usage, raw=json.dumps(envelope), observed=observed
        )
    content = payload.get("result")
    if not isinstance(content, str):
        raise CompletionInvalid("CLI returned no assistant text", usage=usage)
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CompletionError("CLI returned invalid Unicode text") from exc
    return CompletionResult(
        content=content,
        finish_reason="length" if length else "stop",
        usage=usage,
        observed=observed,
    )
