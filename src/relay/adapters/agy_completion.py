"""Antigravity (`agy`) completion backend.

Translates completion requests into `agy -p "<prompt>" --output-format json` invocations.
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

from relay.adapters.antigravity import agy_envelope, envelope_error
from relay.adapters.claude_completion import discard_output
from relay.adapters.completion import (
    CompletionError,
    CompletionInvalid,
    CompletionProfile,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    CompletionTerminationError,
    CompletionUsage,
    ObservedModel,
    model_aliases,
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

COMPLETION_FLAGS: tuple[str, ...] = ("--mode", "plan", "--sandbox")
PROMPT_LIMIT_BYTES = 30_000 if sys.platform == "win32" else 120_000
DEFAULT_AGY_MODEL = "gemini-2.5-pro"
DEFAULT_CONTEXT_WINDOW = 1_048_576
DEFAULT_MAX_OUTPUT_TOKENS = 65_536
ENUMERATION_TIMEOUT_S = 15.0

_ENUMERATED: dict[str, tuple[str, ...]] = {}


def _enumerated_models(cmd: str, env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Slugs printed by `agy models`, cached per command for the process lifetime.

    Each line is `<slug>\\t<display name>`. Returns an empty tuple on any failure (missing
    command, non-zero exit, timeout, unparseable output) so callers can fall back.
    """
    if cmd in _ENUMERATED:
        return _ENUMERATED[cmd]
    slugs: list[str] = []
    try:
        proc = run_process(
            resolve_command(cmd, ("models",), env=env),
            cwd=Path.cwd(),
            env=os.environ if env is None else env,
            timeout_s=ENUMERATION_TIMEOUT_S,
        )
        if proc.exit_code == 0 and not proc.timed_out:
            for line in proc.stdout.splitlines():
                with contextlib.suppress(ValueError):
                    slugs.append(model_aliases((line.split("\t", 1)[0].strip(),))[0])
    except (CommandNotFound, OSError, ProcessCancelled, ProcessDrainError, OutputLimitExceeded):
        slugs = []
    result = tuple(dict.fromkeys(slugs))
    _ENUMERATED[cmd] = result
    return result


def _positive(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _usage(payload: dict[str, Any]) -> CompletionUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    raw = cast("dict[str, Any]", raw)
    input_tokens = raw.get("input_tokens")
    output_tokens = raw.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 0
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens < 0
    ):
        return None
    cache_read = raw.get("cache_read_tokens", raw.get("cache_read_input_tokens"))
    if cache_read is not None and (
        isinstance(cache_read, bool) or not isinstance(cache_read, int) or cache_read < 0
    ):
        cache_read = None
    cache_write = raw.get("cache_creation_input_tokens")
    if cache_write is not None and (
        isinstance(cache_write, bool) or not isinstance(cache_write, int) or cache_write < 0
    ):
        cache_write = None
    return CompletionUsage(input_tokens, output_tokens, cache_read, cache_write)


def _observed(payload: dict[str, Any], request_model: str = "default") -> ObservedModel | None:
    raw = payload.get("modelUsage")
    if isinstance(raw, dict) and len(cast("dict[str, Any]", raw)) == 1:
        ((model, entry),) = cast("dict[str, Any]", raw).items()
        if model.strip() and isinstance(entry, dict):
            entry = cast("dict[str, Any]", entry)
            context = _positive(entry.get("contextWindow"))
            output = _positive(entry.get("maxOutputTokens"))
            if context is not None and output is not None:
                return ObservedModel(model.strip(), context, output)

    # agy CLI outputs standard usage tokens rather than Claude-specific modelUsage.
    # Fall back to payload model/limits or Gemini defaults.
    model_name: str
    if isinstance(payload.get("model"), str) and payload["model"].strip():
        model_name = payload["model"].strip()
    elif isinstance(payload.get("model_id"), str) and payload["model_id"].strip():
        model_name = payload["model_id"].strip()
    elif request_model and request_model != "default":
        model_name = request_model
    else:
        model_name = DEFAULT_AGY_MODEL

    context = (
        _positive(payload.get("context_window") or payload.get("contextWindow"))
        or DEFAULT_CONTEXT_WINDOW
    )
    output = (
        _positive(payload.get("max_output_tokens") or payload.get("maxOutputTokens"))
        or DEFAULT_MAX_OUTPUT_TOKENS
    )

    return ObservedModel(model_name, context, output)


class AgyCompletionAdapter:
    id = "agy"
    COMPLETION_FLAGS = COMPLETION_FLAGS

    def __init__(
        self,
        agent: Agent,
        *,
        parent_env: Mapping[str, str] | None = None,
    ) -> None:
        self._agent = agent
        self._parent_env = None if parent_env is None else dict(parent_env)
        self._aliases: tuple[str, ...] | None = None

    @property
    def default_aliases(self) -> tuple[str, ...]:
        if self._aliases is None:
            slugs = _enumerated_models(self._agent.cmd, self._environment())
            if slugs:
                self._aliases = model_aliases(("default", *slugs))
            elif self._agent.models:
                self._aliases = model_aliases(("default", *self._agent.models.values()))
            else:
                self._aliases = ("default",)
        return self._aliases

    def _environment(self) -> CompleteEnv:
        parent = os.environ if self._parent_env is None else self._parent_env
        extra: dict[str, str] = {}
        if "GEMINI_API_KEY" not in self._agent.env and parent.get("GEMINI_API_KEY"):
            extra["GEMINI_API_KEY"] = parent["GEMINI_API_KEY"]
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
        args = ["-p", request.prompt, "--output-format", "json", *COMPLETION_FLAGS]
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
        if len(request.prompt.encode("utf-8")) > PROMPT_LIMIT_BYTES:
            raise CompletionError(
                "Prompt exceeds CLI argument limit",
                status=400,
                code="context_length_exceeded",
                param="messages",
            )
        try:
            with (
                cancel_processes_when(cancel),
                stream_output(discard_output, max_bytes=max_output_bytes),
            ):
                env = self._environment()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                command = resolve_command(
                    self._agent.cmd, self.arguments(request), env=env, cwd=cwd
                )
                proc = run_process(command, cwd=cwd, env=env, timeout_s=remaining)
            return parse_completion(
                proc,
                structured=request.output_schema is not None,
                request_model=request.model,
                secrets=env,
            )
        except CommandNotFound as exc:
            message = (
                f"Antigravity CLI not found: command {self._agent.cmd!r} (resolved via PATH); "
                "check agents.agy.cmd in ~/.config/relay/agents.yaml"
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


def parse_completion(
    proc: ProcessResult,
    *,
    structured: bool,
    request_model: str = "default",
    secrets: Mapping[str, str] | None = None,
) -> CompletionResult:
    if proc.timed_out:
        raise CompletionError("Completion deadline expired", status=504, code="timeout")
    payload = agy_envelope(proc.stdout)
    is_error = payload is not None and (
        payload.get("status") in {"ERROR", "INVALID"} or payload.get("is_error") is True
    )
    if proc.exit_code != 0 or is_error:
        err_msg = envelope_error(payload) if payload else None
        diagnostic = (
            proc.stderr + " " + (err_msg or "") + " " + (json.dumps(payload) if payload else "")
        ).lower()
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
            f"Antigravity CLI completion failed: exit code {proc.exit_code}; "
            f"stderr: {redact(proc.stderr[-2000:], secrets=secrets)}"
        )
        print(message, file=sys.stderr)
        raise CompletionError(message)
    if payload is None:
        message = (
            f"Antigravity CLI returned no completion object: exit code {proc.exit_code}; "
            f"stderr: {redact(proc.stderr[-2000:], secrets=secrets)}"
        )
        print(message, file=sys.stderr)
        raise CompletionInvalid(message, raw=proc.stdout[:4000])
    usage = _usage(payload)
    observed = _observed(payload, request_model=request_model)
    length = payload.get("stop_reason") in ("max_tokens", "length")
    if structured:
        if length:
            raise CompletionError(
                "Tool output reached its output limit", code="incomplete_tool_output"
            )
        candidate: Any = payload.get("structured_output")
        if candidate is None and isinstance(payload.get("response"), str):
            with contextlib.suppress(ValueError, RecursionError):
                candidate = json.loads(payload["response"])
        elif candidate is None and isinstance(payload.get("result"), str):
            with contextlib.suppress(ValueError, RecursionError):
                candidate = json.loads(payload["result"])
        if not isinstance(candidate, dict):
            raise CompletionInvalid(
                "CLI returned no tool envelope",
                raw=str(payload.get("response") or payload.get("result", ""))[:4000],
                usage=usage,
            )
        envelope = cast("dict[str, Any]", candidate)
        return CompletionResult(
            envelope=envelope, usage=usage, raw=json.dumps(envelope), observed=observed
        )
    content = payload.get("response")
    if content is None:
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
