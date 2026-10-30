"""Restricted Codex completions over stdin and the public JSONL event stream.

CLI feature switches and a deny hook constrain model-side tools. They are CLI-mediated
guardrails, not an operating-system isolation guarantee. Observed tool activity makes the
completion fail even if the CLI subsequently prints a valid assistant response.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import shlex
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from relay.adapters.claude_completion import RATE_LIMIT_MARKERS, discard_output
from relay.adapters.completion import (
    CompletionError,
    CompletionInvalid,
    CompletionProfile,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    CompletionTerminationError,
    CompletionUsage,
    model_aliases,
)
from relay.adapters.parsing import strict_schema
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

_AUTH_KEYS = ("OPENAI_API_KEY", "CODEX_API_KEY")
_DENIED = "relay-tool-denied"
_HOOK_TRUST_ADVISORY = (
    "`--dangerously-bypass-hook-trust` is enabled. "
    "Enabled hooks may run without review for this invocation."
)
_DISABLED_FEATURES = (
    "shell_tool",
    "shell_snapshot",
    "apps",
    "plugins",
    "multi_agent",
    "browser_use",
    "computer_use",
    "image_generation",
    "view_image",
    "skill_search",
    "memories",
    "code_mode",
)
ENUMERATION_TIMEOUT_S = 15.0
ENUMERATION_MAX_BYTES = 4 * 1024 * 1024
_ENUMERATED: dict[tuple[str, str], tuple[str, ...]] = {}
_ENUMERATION_LOCK = threading.Lock()
logger = logging.getLogger("uvicorn.error")


def _model_slugs(text: str) -> tuple[str, ...]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("expected a models array")
    rows = cast("dict[str, object]", payload).get("models")
    if not isinstance(rows, list):
        raise ValueError("expected a models array")
    slugs: list[str] = []
    for row in cast("list[object]", rows):
        if not isinstance(row, dict):
            continue
        model = cast("dict[str, object]", row)
        slug = model.get("slug")
        if model.get("visibility") == "hide" or not isinstance(slug, str):
            continue
        try:
            model_aliases((slug,))
        except ValueError:
            continue
        slugs.append(slug)
    if not slugs:
        raise ValueError("no selectable model IDs returned")
    return tuple(dict.fromkeys(slugs))


def _enumerated_models(cmd: str, env: Mapping[str, str]) -> tuple[str, ...]:
    """Read CLI model metadata once per command/environment, without a completion call."""
    fingerprint = hashlib.sha256(json.dumps(sorted(env.items())).encode()).hexdigest()
    key = (cmd, fingerprint)
    with _ENUMERATION_LOCK:
        if key in _ENUMERATED:
            return _ENUMERATED[key]
        try:
            with stream_output(discard_output, max_bytes=ENUMERATION_MAX_BYTES):
                proc = run_process(
                    resolve_command(cmd, ("debug", "models"), env=env),
                    cwd=Path.cwd(),
                    env=env,
                    timeout_s=ENUMERATION_TIMEOUT_S,
                )
            if proc.timed_out:
                raise ValueError("metadata command timed out")
            if proc.exit_code != 0:
                raise ValueError(f"metadata command exited with code {proc.exit_code}")
            slugs = _model_slugs(proc.stdout)
        except (CommandNotFound, OSError, OutputLimitExceeded, ValueError) as exc:
            logger.warning(
                "relay serve: Codex model enumeration failed (%s); using configured models",
                type(exc).__name__,
            )
            slugs = ()
        _ENUMERATED[key] = slugs
        return slugs


def _transport_schema(schema: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(schema)
    # Codex strict output schemas cannot represent an arbitrary arguments object. Encode
    # that one transport field as JSON text, then restore it before gateway validation.
    properties = value.get("properties", {})
    if "tool_calls" in properties:
        call_properties = properties["tool_calls"]["items"]["properties"]
        call_properties["arguments"] = {"type": "string"}
        name = call_properties.get("name", {})
        if "const" in name:
            call_properties["name"] = {"type": "string", "enum": [name["const"]]}
    return strict_schema(value)


def _hook_command(executable: Path, script: Path) -> tuple[str, str]:
    command = shlex.join((str(executable), str(script)))
    windows = "& " + " ".join(
        "'" + str(path).replace("'", "''") + "'" for path in (executable, script)
    )
    return command, windows


def _deny_hook(cwd: Path) -> str:
    script = cwd / "relay-deny-tools.py"
    marker = cwd / _DENIED
    script.write_text(
        "import pathlib, sys\n"
        "try:\n"
        f"    pathlib.Path({str(marker)!r}).touch()\n"
        "finally:\n"
        "    sys.stderr.write('Relay completion tools belong to the client.\\n')\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    command, windows = _hook_command(Path(sys.executable).resolve(), script.resolve())
    return (
        'hooks.PreToolUse=[{hooks=[{type="command",command='
        + json.dumps(command)
        + ",command_windows="
        + json.dumps(windows)
        + ",timeout=5}]}]"
    )


class CodexCompletionAdapter:
    id = "codex"

    def __init__(self, agent: Agent, *, parent_env: Mapping[str, str] | None = None) -> None:
        self._agent = agent
        self._parent_env = None if parent_env is None else dict(parent_env)
        self._aliases: tuple[str, ...] | None = None

    @property
    def default_aliases(self) -> tuple[str, ...]:
        if self._aliases is None:
            slugs = _enumerated_models(self._agent.cmd, self._environment())
            self._aliases = model_aliases(("default", *(slugs or self._agent.models.values())))
        return self._aliases

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport:
        return CompletionSupport(
            CompletionProfile(self.id, "unknown", sys.platform, "unknown", "none"),
            supports_tools=True,
            native_max_tokens=False,
        )

    def _environment(self) -> CompleteEnv:
        parent = os.environ if self._parent_env is None else self._parent_env
        extra = {
            key: parent[key] for key in _AUTH_KEYS if key not in self._agent.env and parent.get(key)
        }
        return build_env(self._agent, extra, parent_env=parent)

    @staticmethod
    def arguments(request: CompletionRequest, cwd: Path) -> tuple[str, ...]:
        model_aliases((request.model,))
        if request.effort not in (None, "low", "medium", "high"):
            raise CompletionError(
                "Unsupported reasoning effort",
                status=400,
                code="unsupported_parameter",
                param="reasoning_effort",
            )
        args = [
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--dangerously-bypass-hook-trust",
            "-C",
            str(cwd),
        ]
        settings = [
            'approval_policy="never"',
            'web_search="disabled"',
            "mcp_servers={}",
            "project_doc_max_bytes=0",
            'shell_environment_policy.inherit="none"',
            "show_raw_agent_reasoning=false",
            "features.hooks=true",
            *(f"features.{feature}=false" for feature in _DISABLED_FEATURES),
            _deny_hook(cwd),
        ]
        for setting in settings:
            args += ["-c", setting]
        if request.model != "default":
            args += ["--model", request.model]
        if request.effort is not None:
            args += ["-c", f'model_reasoning_effort="{request.effort}"']
        if request.output_schema is not None:
            schema = cwd / "relay-output-schema.json"
            schema.write_text(
                json.dumps(_transport_schema(request.output_schema)), encoding="utf-8"
            )
            args += ["--output-schema", str(schema)]
        return (*args, "-")

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
            with (
                cancel_processes_when(cancel),
                stream_output(discard_output, max_bytes=max_output_bytes),
            ):
                env = self._environment()
                args = self.arguments(request, cwd)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                command = resolve_command(self._agent.cmd, args, env=env, cwd=cwd)
                prompt = request.prompt
                if request.output_schema is not None:
                    prompt += (
                        "\nCodex transport encoding: each tool_calls[].arguments must be a "
                        "JSON-encoded string containing the complete arguments object. "
                        "Relay decodes that string before the client receives the proposal.\n"
                    )
                proc = run_process(command, cwd=cwd, env=env, stdin=prompt, timeout_s=remaining)
            if (cwd / _DENIED).exists():
                raise CompletionError(
                    "Codex attempted a model-side tool", code="backend_tool_activity"
                )
            return parse_completion(proc, structured=request.output_schema is not None, secrets=env)
        except CommandNotFound as exc:
            raise CompletionError(
                f"Codex CLI not found: command {self._agent.cmd!r}", status=503, code="unavailable"
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
        except OSError as exc:
            raise CompletionError(
                "Cannot prepare Codex completion policy", code="policy_setup_failed"
            ) from exc


def _usage(event: dict[str, Any]) -> CompletionUsage | None:
    raw = event.get("usage")
    if not isinstance(raw, dict):
        return None
    raw = cast("dict[str, Any]", raw)
    inputs, outputs, cached = (
        raw.get("input_tokens"),
        raw.get("output_tokens"),
        raw.get("cached_input_tokens"),
    )
    if type(inputs) is not int or type(outputs) is not int or inputs < 0 or outputs < 0:
        return None
    if cached is not None and (type(cached) is not int or not 0 <= cached <= inputs):
        return None
    return CompletionUsage(inputs, outputs, cached)


def parse_completion(
    proc: ProcessResult,
    *,
    structured: bool,
    secrets: Mapping[str, str] | None = None,
) -> CompletionResult:
    if proc.timed_out:
        raise CompletionError("Completion deadline expired", status=504, code="timeout")
    events: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise CompletionError("Codex returned malformed JSONL") from exc
        if not isinstance(event, dict):
            raise CompletionError("Codex returned malformed JSONL")
        events.append(cast("dict[str, Any]", event))
    diagnostic = (proc.stderr + " " + json.dumps(events)).lower()
    failures = any(
        event.get("type") in {"turn.failed", "turn.cancelled", "error"} for event in events
    )
    if proc.exit_code != 0 or failures:
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
            f"Codex CLI completion failed: exit code {proc.exit_code}; "
            f"stderr: {redact(proc.stderr[-2000:], secrets=secrets)}"
        )
        if any(word in diagnostic for word in RATE_LIMIT_MARKERS):
            raise CompletionError(message, status=429, code="rate_limit_exceeded")
        raise CompletionError(message)
    if any(
        "hook" in line.lower()
        and any(
            word in line.lower() for word in ("error", "fail", "skip", "not trusted", "timed out")
        )
        for line in proc.stderr.splitlines()
    ):
        raise CompletionError(
            "Codex completion hook did not run successfully", code="policy_setup_failed"
        )
    content: str | None = None
    terminal: dict[str, Any] | None = None
    started = False
    for event in events:
        kind = event.get("type")
        if kind in {"item.started", "item.updated", "item.completed"}:
            if terminal is not None:
                raise CompletionError("Codex emitted an item after completing its turn")
            item = event.get("item")
            if not isinstance(item, dict):
                raise CompletionError("Codex returned an invalid item")
            item = cast("dict[str, Any]", item)
            if item.get("type") == "error":
                # The CLI also uses error items for startup advisories. This exact notice
                # acknowledges our reviewed hook flag; other errors remain fatal.
                if (
                    kind == "item.completed"
                    and not started
                    and item.get("message") == _HOOK_TRUST_ADVISORY
                ):
                    continue
                raise CompletionError("Codex returned a CLI error item")
            if item.get("type") not in {"agent_message", "reasoning"}:
                raise CompletionError(
                    "Codex attempted a model-side tool", code="backend_tool_activity"
                )
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                if not isinstance(text, str):
                    raise CompletionError("Codex returned invalid assistant text")
                content = text
        elif kind == "turn.completed":
            if terminal is not None or event.get("status") not in (None, "completed", "success"):
                raise CompletionError("Codex returned an invalid terminal state")
            if event.get("stop_reason") not in (None, "stop", "end_turn", "max_tokens", "length"):
                raise CompletionError("Codex returned an unsuccessful stop reason")
            terminal = event
        elif kind == "turn.started":
            if started or terminal is not None:
                raise CompletionError("Codex returned an unexpected additional turn")
            started = True
        elif kind == "turn.interrupted":
            raise CompletionError("Codex returned an interrupted turn")
    if terminal is None or content is None:
        raise CompletionError("Codex returned no completed assistant turn")
    usage = _usage(terminal)
    length = terminal.get("stop_reason") in {"max_tokens", "length"}
    if structured:
        if length:
            raise CompletionError(
                "Tool output reached its output limit", code="incomplete_tool_output"
            )
        try:
            envelope = json.loads(content)
        except (ValueError, RecursionError) as exc:
            raise CompletionInvalid(
                "Codex returned no tool envelope", raw=content[:4000], usage=usage
            ) from exc
        if not isinstance(envelope, dict):
            raise CompletionInvalid(
                "Codex returned no tool envelope", raw=content[:4000], usage=usage
            )
        envelope = cast("dict[str, Any]", envelope)
        calls = envelope.get("tool_calls")
        if isinstance(calls, list):
            for call in cast("list[Any]", calls):
                if not isinstance(call, dict):
                    continue
                call = cast("dict[str, Any]", call)
                if isinstance(call.get("arguments"), str):
                    try:
                        call["arguments"] = json.loads(call["arguments"])
                    except (ValueError, RecursionError) as exc:
                        raise CompletionInvalid(
                            "Codex returned invalid tool arguments", raw=content[:4000], usage=usage
                        ) from exc
        return CompletionResult(envelope=envelope, usage=usage, raw=json.dumps(envelope))
    try:
        content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CompletionError("Codex returned invalid Unicode text") from exc
    return CompletionResult(
        content=content, finish_reason="length" if length else "stop", usage=usage
    )
