"""Cursor completion transport with CLI-mediated tool restrictions.

Each invocation uses the gateway's private scratch directory, isolated Cursor settings,
deny permissions and a pre-tool hook. These are CLI controls, not filesystem isolation.
Reported tool activity is rejected rather than exposed as a client completion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from relay.adapters.claude_completion import RATE_LIMIT_MARKERS, discard_output
from relay.adapters.completion import (
    CompletionChunkSink,
    CompletionError,
    CompletionInvalid,
    CompletionModelOptions,
    CompletionProfile,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    CompletionTerminationError,
    CompletionUsage,
    ObservedModel,
    current_chunk_sink,
    model_aliases,
)
from relay.adapters.cursor import resolve_cursor_command, schema_instructions
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

DENY_PERMISSIONS = (
    "Shell(*)",
    "Read(**)",
    "Write(**)",
    "WebFetch(*)",
    "Mcp(*:*)",
)
ENUMERATION_TIMEOUT_S = 15.0
ENUMERATION_OUTPUT_BYTES = 4 * 1024 * 1024
_ENUMERATED: dict[tuple[str, str], tuple[str, ...]] = {}
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_REASONING_EFFORTS = ("minimal", "low", "medium", "high")
logger = logging.getLogger("uvicorn.error")
_DENY_SCRIPT = """import json
import sys
from pathlib import Path

while sys.stdin.buffer.read(65536):
    pass
Path(__file__).with_suffix(".used").touch()
print(json.dumps({"permission": "deny", "agent_message": "Relay clients execute tools."}))
raise SystemExit(2)
"""


def _enumerated_models(cmd: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """Account model IDs from `agent models`, cached without retaining credentials."""
    fingerprint = hashlib.sha256(json.dumps(sorted(env.items())).encode()).hexdigest()
    key = (cmd, fingerprint)
    if key in _ENUMERATED:
        return _ENUMERATED[key]
    models: list[str] = []
    failure: str | None = None
    try:
        with stream_output(discard_output, max_bytes=ENUMERATION_OUTPUT_BYTES):
            proc = run_process(
                resolve_command(cmd, ("models",), env=env),
                cwd=Path.cwd(),
                env=env,
                timeout_s=ENUMERATION_TIMEOUT_S,
            )
        if proc.timed_out:
            failure = "deadline expired"
        elif proc.exit_code != 0:
            failure = f"exit code {proc.exit_code}"
        else:
            for line in proc.stdout.splitlines():
                candidate, separator, label = _ANSI_ESCAPE.sub("", line).strip().partition(" - ")
                if not separator or not label.strip():
                    continue
                try:
                    models.append(model_aliases((candidate.strip(),))[0])
                except ValueError:
                    continue
            if not models:
                failure = "no model IDs in output"
    except (OSError, OutputLimitExceeded) as exc:
        # Cancellation and unconfirmed termination must retain their ownership signals.
        failure = type(exc).__name__
    result = tuple(dict.fromkeys(models))
    if failure is not None:
        logger.warning("Cursor model enumeration failed (%s); using configured models", failure)
    _ENUMERATED[key] = result
    return result


def _write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise OSError("Cursor policy path must not be a symbolic link")
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _prepare_policy(cwd: Path) -> tuple[Path, Path, Path]:
    """Write only fixed settings under the runtime-owned scratch; never copy user config."""
    config, data, project = (cwd / name for name in (".cursor-config", ".cursor-data", ".cursor"))
    for directory in (config, data, project):
        if directory.is_symlink():
            raise OSError("Cursor policy directory must not be a symbolic link")
        directory.mkdir(mode=0o700, exist_ok=True)
    script = config / "deny_tools.py"
    _write(script, _DENY_SCRIPT)
    script = script.resolve()
    executable = str(Path(sys.executable).resolve())
    if sys.platform == "win32":
        # Cursor runs Windows hooks in PowerShell. Single-quoted literals prevent
        # interpolation even when a trusted installation/scratch path contains '$'.
        command = (
            "& "
            + " ".join("'" + value.replace("'", "''") + "'" for value in (executable, str(script)))
            + "; exit $LASTEXITCODE"
        )
    else:
        command = shlex.join((executable, str(script)))
    # Cursor otherwise allows hook spawn/timeout/execution failures to proceed.
    hooks = {
        "version": 1,
        "hooks": {"preToolUse": [{"command": command, "timeout": 10, "failClosed": True}]},
    }
    permissions = {"allow": [], "deny": list(DENY_PERMISSIONS)}
    _write(project / "cli.json", json.dumps({"permissions": permissions}))
    _write(project / "hooks.json", json.dumps(hooks))
    _write(project / "mcp.json", '{"mcpServers":{}}')
    _write(
        config / "cli-config.json",
        json.dumps(
            {
                "version": 1,
                "editor": {"vimMode": False},
                "permissions": permissions,
                "approvalMode": "allowlist",
                "display": {"showThinkingBlocks": False},
            }
        ),
    )
    _write(config / "mcp.json", '{"mcpServers":{}}')
    _write(config / "hooks.json", '{"version":1,"hooks":{}}')
    return config, data, script.with_suffix(".used")


class CursorCompletionAdapter:
    id = "cursor"

    def __init__(self, agent: Agent, *, parent_env: Mapping[str, str] | None = None) -> None:
        self._agent = agent
        self._parent_env = None if parent_env is None else dict(parent_env)
        self._aliases: tuple[str, ...] | None = None
        self._model_options: dict[str, CompletionModelOptions] = {}
        self._reasoning_models: dict[str, dict[str, str]] = {}
        self._reasoning_aliases: dict[str, str] = {}

    @property
    def default_aliases(self) -> tuple[str, ...]:
        return self._load_models()

    def _load_models(self) -> tuple[str, ...]:
        if self._aliases is not None:
            return self._aliases
        parent = os.environ if self._parent_env is None else self._parent_env
        extra: dict[str, str] = {}
        if "CURSOR_API_KEY" not in self._agent.env and parent.get("CURSOR_API_KEY"):
            extra["CURSOR_API_KEY"] = parent["CURSOR_API_KEY"]
        env = build_env(self._agent, extra, parent_env=parent)
        cmd = resolve_cursor_command(self._agent.cmd, env)
        models = _enumerated_models(cmd, env)
        families: dict[str, dict[str, str]] = {}
        for model in models:
            base, _, effort = model.rpartition("-")
            if base and effort in _REASONING_EFFORTS:
                families.setdefault(base, {})[effort] = model
        # A lone suffix may be part of a standalone name. Require multiple discovered
        # variants, and retain their exact native IDs rather than guessing parameter keys.
        self._reasoning_models = {
            base: {effort: variants[effort] for effort in _REASONING_EFFORTS if effort in variants}
            for base, variants in families.items()
            if len(variants) > 1
        }
        self._reasoning_aliases = {
            alias: base
            for base, variants in self._reasoning_models.items()
            for alias in (base, *variants.values())
        }
        options: dict[str, CompletionModelOptions] = {}
        aliases: list[str] = ["default"]
        for model in models or tuple(self._agent.models.values()):
            base = self._reasoning_aliases.get(model)
            if base is not None and model != base:
                aliases.append(base)
                efforts = tuple(self._reasoning_models[base])
                options[base] = CompletionModelOptions(efforts)
                options[model] = CompletionModelOptions(efforts, listed=False)
            aliases.append(model)
        self._model_options = options
        self._aliases = model_aliases(aliases)
        return self._aliases

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport:
        # Explicit --model filters still need account inventory to establish variants.
        # This is a cached, read-only metadata command, never a completion probe.
        self._load_models()
        return CompletionSupport(
            profile=CompletionProfile(self.id, "unknown", sys.platform, "unknown", "none"),
            supports_tools=True,
            native_max_tokens=False,
            supports_reasoning_effort=False,
            model_options=self._model_options,
        )

    def _environment(self, config: Path, data: Path) -> CompleteEnv:
        parent = os.environ if self._parent_env is None else self._parent_env
        extra = {"CURSOR_CONFIG_DIR": str(config), "CURSOR_DATA_DIR": str(data)}
        if "CURSOR_API_KEY" not in self._agent.env and parent.get("CURSOR_API_KEY"):
            extra["CURSOR_API_KEY"] = parent["CURSOR_API_KEY"]
        # Login credentials live in the OS auth store, separately from Cursor settings.
        # Keep HOME/APPDATA/XDG_CONFIG_HOME unchanged; no credentials need copying.
        return build_env(self._agent, extra, parent_env=parent)

    def arguments(self, request: CompletionRequest, *, stream: bool = False) -> tuple[str, ...]:
        try:
            model_aliases((request.model,))
        except ValueError as exc:
            raise CompletionError("Invalid Cursor model", status=400, param="model") from exc
        model = request.model
        if request.effort is not None:
            self._load_models()
            base = self._reasoning_aliases.get(model)
            variants = {} if base is None else self._reasoning_models[base]
            if request.effort not in variants:
                raise CompletionError(
                    "Cursor model does not support this reasoning effort",
                    status=400,
                    code="unsupported_parameter",
                    param="reasoning_effort",
                )
            # An explicit effort overrides a legacy suffix. Without one, keep the exact
            # requested alias: bare base uses Cursor's default, legacy -low stays low.
            model = variants[request.effort]
        args = ["-p", "--mode", "ask", "--trust", "--output-format", "stream-json"]
        if stream and request.output_schema is None:
            args.append("--stream-partial-output")
        if model != "default":
            args += ["--model", model]
        return tuple(args)

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
        deadline = time.monotonic() + timeout_s
        chunk_sink = on_chunk if on_chunk is not None else current_chunk_sink()
        streaming = chunk_sink is not None and request.output_schema is None
        args = self.arguments(request, stream=streaming)
        prompt = request.prompt
        if request.output_schema is not None:
            prompt += schema_instructions(request.output_schema)
        try:
            prompt.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CompletionError("Invalid Unicode prompt", status=400, param="messages") from exc
        try:
            config, data, tool_marker = _prepare_policy(cwd)
        except OSError as exc:
            raise CompletionError("Could not prepare Cursor completion restrictions") from exc
        env = self._environment(config, data)
        command_name = resolve_cursor_command(self._agent.cmd, env)
        line_sink = (
            _stream_sink(chunk_sink, tool_marker)
            if streaming and chunk_sink is not None
            else discard_output
        )
        try:
            with (
                cancel_processes_when(cancel),
                stream_output(line_sink, max_bytes=max_output_bytes),
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                command = resolve_command(command_name, args, env=env, cwd=cwd)
                proc = run_process(command, cwd=cwd, env=env, stdin=prompt, timeout_s=remaining)
            if tool_marker.exists():
                raise CompletionError("Cursor attempted model-side tools", code="backend_tool_use")
            return parse_completion(proc, structured=request.output_schema is not None, secrets=env)
        except CommandNotFound as exc:
            raise CompletionError(
                f"Cursor CLI not found: command {command_name!r}; check agents.cursor.cmd",
                status=503,
                code="unavailable",
            ) from exc
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


def _counter(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage(payload: dict[str, Any]) -> CompletionUsage | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    raw = cast("dict[str, Any]", raw)
    counts = [
        _counter(raw.get(key))
        for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
    ]
    inputs, outputs, reads, writes = counts
    # Cursor reports disjoint input/cache buckets. Missing buckets cannot be inferred.
    if inputs is None or outputs is None or reads is None or writes is None:
        return None
    return CompletionUsage(inputs + reads + writes, outputs, reads, writes)


def _observed(payload: dict[str, Any]) -> ObservedModel | None:
    model = payload.get("model")
    context = _counter(payload.get("contextWindow"))
    output = _counter(payload.get("maxOutputTokens"))
    if not isinstance(model, str) or not model.strip() or not context or not output:
        return None
    return ObservedModel(model.strip(), context, output)


def _tool_activity(event: dict[str, Any]) -> bool:
    kind = event.get("type")
    if isinstance(kind, str) and (kind.startswith("tool_") or kind == "function_call"):
        return True
    if event.get("tool_call") or event.get("tool_calls") or event.get("function_call"):
        return True
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    message = cast("dict[str, Any]", message)
    if message.get("tool_calls") or message.get("function_call"):
        return True
    blocks = message.get("content")
    return isinstance(blocks, list) and any(
        isinstance(block, dict)
        and cast("dict[str, Any]", block).get("type")
        in {"tool_use", "tool_call", "tool_result", "function_call"}
        for block in cast("list[Any]", blocks)
    )


def _assistant_text(event: dict[str, Any]) -> str | None:
    message = event.get("message")
    if isinstance(message, dict):
        message_dict = cast("dict[str, Any]", message)
        content = message_dict.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            content_list = cast("list[Any]", content)
            blocks: list[str] = [
                cast("str", cast("dict[str, Any]", block)["text"])
                for block in content_list
                if isinstance(block, dict)
                and cast("dict[str, Any]", block).get("type") == "text"
                and isinstance(cast("dict[str, Any]", block).get("text"), str)
            ]
            if blocks:
                return "".join(blocks)
    if isinstance(event.get("text"), str):
        return cast("str", event["text"])
    return None


def _stream_sink(sink: CompletionChunkSink, tool_marker: Path) -> Callable[[str], None]:
    emitted = ""
    failed = False

    def on_line(line: str) -> None:
        nonlocal emitted, failed
        if failed or not line.strip():
            return
        if tool_marker.exists():
            failed = True
            return
        try:
            raw_event: Any = json.loads(line)
        except (ValueError, RecursionError):
            failed = True
            return
        if not isinstance(raw_event, dict):
            failed = True
            return
        event = cast("dict[str, Any]", raw_event)
        if _tool_activity(event) or event.get("type") == "error":
            failed = True
            return
        if event.get("type") == "assistant":
            if "model_call_id" in event:
                return
            text = _assistant_text(event)
            if not text:
                return
            if "timestamp_ms" in event:
                emitted += text
                sink(text)
            elif not emitted:
                emitted = text
                sink(text)
            elif text.startswith(emitted) and len(text) > len(emitted):
                delta = text[len(emitted) :]
                emitted = text
                sink(delta)

    return on_line


def parse_completion(
    proc: ProcessResult, *, structured: bool, secrets: Mapping[str, str] | None = None
) -> CompletionResult:
    if proc.timed_out:
        raise CompletionError("Completion deadline expired", status=504, code="timeout")
    if proc.exit_code != 0:
        diagnostic = proc.stderr.lower()
        if any(
            word in diagnostic
            for word in ("context_length_exceeded", "context window", "prompt is too long")
        ):
            raise CompletionError(
                "Context length exceeded",
                status=400,
                code="context_length_exceeded",
                param="messages",
            )
        message = (
            f"Cursor CLI completion failed: exit code {proc.exit_code}; "
            f"stderr: {redact(proc.stderr[-2000:], secrets=secrets)}"
        )
        if any(word in diagnostic for word in RATE_LIMIT_MARKERS):
            raise CompletionError(message, status=429, code="rate_limit_exceeded")
        raise CompletionError(message)
    events: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event: Any = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise CompletionInvalid("Cursor returned invalid event JSON") from exc
        if not isinstance(event, dict):
            raise CompletionInvalid("Cursor returned an invalid event")
        event = cast("dict[str, Any]", event)
        if _tool_activity(event):
            raise CompletionError("Cursor reported model-side tools", code="backend_tool_use")
        if event.get("type") == "error":
            raise CompletionError("Cursor reported an execution error")
        events.append(event)
    results = [event for event in events if event.get("type") == "result"]
    if len(results) != 1 or results[0] is not events[-1]:
        raise CompletionError("Cursor returned no unique terminal result")
    payload = results[0]
    if payload.get("subtype") != "success" or payload.get("is_error") is not False:
        raise CompletionError("Cursor did not complete successfully")
    usage = _usage(payload)
    content = payload.get("result")
    if not isinstance(content, str):
        raise CompletionInvalid("Cursor returned no assistant text", usage=usage)
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CompletionError("Cursor returned invalid Unicode text") from exc
    stop_reason = payload.get("stop_reason")
    if stop_reason not in (None, "stop", "end_turn", "stop_sequence", "max_tokens", "length"):
        raise CompletionError("Cursor reported an unsuccessful stop reason")
    length = stop_reason in {"max_tokens", "length"}
    if structured:
        if length:
            raise CompletionError(
                "Tool output reached its output limit", code="incomplete_tool_output"
            )
        try:
            envelope: Any = json.loads(content)
        except (ValueError, RecursionError) as exc:
            raise CompletionInvalid(
                "Cursor returned no tool envelope", raw=content[:4000], usage=usage
            ) from exc
        if not isinstance(envelope, dict):
            raise CompletionInvalid(
                "Cursor returned no tool envelope", raw=content[:4000], usage=usage
            )
        return CompletionResult(
            envelope=cast("dict[str, Any]", envelope),
            usage=usage,
            raw=content,
            observed=_observed(payload),
        )
    return CompletionResult(
        content=content,
        finish_reason="length" if length else "stop",
        usage=usage,
        observed=_observed(payload),
    )
