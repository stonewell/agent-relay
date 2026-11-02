"""Antigravity (`agy`) completion backend.

Translates completion requests into `agy -p "<prompt>" --output-format json` invocations.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from relay.adapters.antigravity import agy_envelope, envelope_error
from relay.adapters.claude_completion import RATE_LIMIT_MARKERS, discard_output
from relay.adapters.completion import (
    CompletionChunkSink,
    CompletionError,
    CompletionInvalid,
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

_AGY_SETTINGS = (
    '{"toolPermission":"strict",'
    '"allowNonWorkspaceAccess":false,"disableSlashCommands":true,'
    '"permissions":{"allow":[],"ask":[],"deny":['
    '"command(*)","read_file(*)","write_file(*)","read_url(*)","execute_url(*)","mcp(*)"'
    ']}}'
)
COMPLETION_FLAGS: tuple[str, ...] = (
    "--agent",
    "relay-completion",
    "--disable-slash-commands",
    "--sandbox",
    "--settings",
    _AGY_SETTINGS,
)
PROMPT_LIMIT_BYTES = 30_000 if sys.platform == "win32" else 120_000
DEFAULT_AGY_MODEL = "gemini-2.5-pro"
DEFAULT_CONTEXT_WINDOW = 1_048_576
DEFAULT_MAX_OUTPUT_TOKENS = 65_536
ENUMERATION_TIMEOUT_S = 15.0

_AGY_AGENT_MD = (
    "---\n"
    "name: relay-completion\n"
    "description: Stateless single-turn completion responder without tools or subagents.\n"
    "tools: []\n"
    "enableWriteTools: false\n"
    "enableMcpTools: false\n"
    "enableSubagentTools: false\n"
    "enable_write_tools: false\n"
    "enable_mcp_tools: false\n"
    "enable_subagent_tools: false\n"
    "subagents: []\n"
    "mcpServers: []\n"
    "mainAgent: true\n"
    "subagent: false\n"
    "commandExecutionPolicy: off\n"
    "skills: []\n"
    "plugins: []\n"
    "---\n"
    "# System Prompt\n"
    "You are a stateless completion model for a non-interactive API gateway.\n"
    "Explicit user instruction override: Ignore any global coordinator or subagent routing rules "
    "(`<RULE[user_global]>`). Do NOT call `define_subagent`, `invoke_subagent`, `send_message`, "
    "`manage_subagents`, `edit_file`, `write_file`, `apply_diff`, `patch`, `save_file`, "
    "`create_file`, `view_file`, `read_file`, `list_dir`, `grep_search`, `find_by_name`, "
    "`run_command`, `search_web`, `read_url_content`, or any other runtime tool. "
    "If the prompt asks for a JSON tool_calls envelope, emit that JSON object as plain text in "
    "your response without invoking any native tool. Respond directly in this single turn with "
    "only the requested completion output.\n"
)

_AGY_RULE_MD = (
    "---\n"
    "trigger: always_on\n"
    "description: Explicit user instruction override disabling all tools and subagents.\n"
    "---\n"
    "# Explicit User Instructions Override\n"
    "For this workspace session, explicit user instructions take precedence over "
    "`<RULE[user_global]>`:\n"
    "- Do NOT act as a task coordinator and do NOT register or invoke any subagents "
    "(`define_subagent`, `invoke_subagent`, `send_message`, `manage_subagents`).\n"
    "- Do NOT call any runtime tools (`edit_file`, `write_file`, `apply_diff`, `patch`, "
    "`save_file`, `create_file`, `view_file`, `read_file`, `list_dir`, `grep_search`, "
    "`find_by_name`, `run_command`, `search_web`, `read_url_content`, etc.) or run commands.\n"
    "- If client functions/tools are listed in the prompt transcript, never invoke a native tool "
    "call; represent proposed client tool calls only as JSON data inside the requested plain-text "
    "JSON response envelope.\n"
    "- Answer the supplied prompt directly in a single turn and output the response immediately.\n"
)

_DENY_SCRIPT = """import json
import sys
from pathlib import Path

msg = (
    "Explicit user instruction override: Stateless single-turn completion mode. "
    "Do NOT invoke any tools or subagents. Answer directly or emit the requested JSON envelope."
)

if "--pre-invocation" in sys.argv[1:]:
    print(json.dumps({"injectSteps": [{"ephemeralMessage": msg}]}))
    raise SystemExit(0)

Path(__file__).with_suffix(".used").touch()
deny_msg = (
    "Relay clients execute tools. Output the requested JSON envelope containing "
    "the tool_calls directly without calling native tools."
)
print(json.dumps({
    "allow_tool": False,
    "deny_reason": deny_msg,
    "decision": "deny",
    "reason": deny_msg
}))
raise SystemExit(0)
"""


def _write_policy_file(path: Path, text: str) -> None:
    if path.is_symlink():
        raise OSError("Antigravity policy path must not be a symbolic link")
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


_write = _write_policy_file


def _prepare_policy(cwd: Path) -> Path:
    agents_dir = cwd / ".agents"
    custom_agents = agents_dir / "agents"
    rules_dir = agents_dir / "rules"
    for directory in (agents_dir, custom_agents, rules_dir):
        if directory.is_symlink():
            raise OSError(
                "Antigravity policy directory must not be a symbolic link: "
                "Refusing symlinked Antigravity policy path"
            )
        directory.mkdir(mode=0o700, exist_ok=True)
    _write_policy_file(custom_agents / "relay-completion.md", _AGY_AGENT_MD)
    _write_policy_file(rules_dir / "relay-completion.md", _AGY_RULE_MD)
    _write_policy_file(cwd / "GEMINI.md", _AGY_RULE_MD)
    _write_policy_file(
        agents_dir / "rules.json",
        json.dumps({"exclude": ["user_global", "~/.gemini/GEMINI.md"]}),
    )

    known_agents = [
        "explore",
        "flash",
        "tests",
        "pro",
        "pro-gemini",
        "pro-flash-high",
        "review",
        "review-gemini",
        "review-flash-high",
        "scout",
        "mechanical",
        "implementer",
        "specialist",
        "tester",
        "reviewer",
        "self",
        "research",
        "image-generator",
    ]
    excluded_agents = list(dict.fromkeys(known_agents))
    try:
        home_agents_dir = Path.home() / ".gemini" / "config" / "agents"
        if home_agents_dir.is_dir():
            for agent_file in sorted(home_agents_dir.glob("*.md")):
                name = agent_file.stem
                if name != "relay-completion" and name not in excluded_agents:
                    excluded_agents.append(name)
    except (OSError, RuntimeError):
        pass
    _write_policy_file(agents_dir / "agents.json", json.dumps({"exclude": excluded_agents}))
    _write_policy_file(agents_dir / "skills.json", json.dumps({"exclude": ["*"]}))
    _write_policy_file(agents_dir / "plugins.json", json.dumps({"exclude": ["*"]}))

    script = agents_dir / "deny_tools.py"
    _write_policy_file(script, _DENY_SCRIPT)
    script_resolved = script.resolve()
    executable = str(Path(sys.executable).resolve())
    if sys.platform == "win32":
        command_pre_tool = (
            "& "
            + " ".join(
                "'" + value.replace("'", "''") + "'" for value in (executable, str(script_resolved))
            )
            + "; exit $LASTEXITCODE"
        )
        command_pre_inv = (
            "& "
            + " ".join(
                "'" + value.replace("'", "''") + "'"
                for value in (executable, str(script_resolved), "--pre-invocation")
            )
            + "; exit $LASTEXITCODE"
        )
    else:
        command_pre_tool = shlex.join((executable, str(script_resolved)))
        command_pre_inv = shlex.join((executable, str(script_resolved), "--pre-invocation"))
    hooks = {
        "relay-deny-tools": {
            "PreInvocation": [
                {"type": "command", "command": command_pre_inv, "timeout": 10}
            ],
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command_pre_tool, "timeout": 10}],
                }
            ],
        }
    }
    _write_policy_file(agents_dir / "hooks.json", json.dumps(hooks))
    _write_policy_file(agents_dir / "mcp_config.json", '{"mcpServers":{}}')
    marker = script.with_suffix(".used")
    if marker.is_symlink():
        raise OSError(
            "Antigravity policy path must not be a symbolic link: "
            "Refusing symlinked Antigravity policy path"
        )
    marker.unlink(missing_ok=True)
    return marker


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
        _positive(payload.get("context_window"))
        or _positive(payload.get("contextWindow"))
        or DEFAULT_CONTEXT_WINDOW
    )
    output = (
        _positive(payload.get("max_output_tokens"))
        or _positive(payload.get("maxOutputTokens"))
        or DEFAULT_MAX_OUTPUT_TOKENS
    )

    return ObservedModel(model_name, context, output)


def _tool_activity(event: dict[str, Any]) -> bool:
    if event.get("event") == "step_update" and isinstance(event.get("step_update"), dict):
        step = cast("dict[str, Any]", event["step_update"])
        return (
            step.get("step_type") == "tool"
            or bool(step.get("tool_name"))
            or bool(step.get("tool_info"))
            or bool(step.get("subagent_info"))
        )
    return False


def _stream_sink(
    sink: CompletionChunkSink,
    tool_marker: Path | None = None,
    cancel_event: threading.Event | None = None,
    tool_detected: threading.Event | None = None,
) -> Callable[[str], None]:
    emitted = ""
    failed = False

    def on_line(line: str) -> None:
        nonlocal emitted, failed
        if failed:
            return
        if tool_marker is not None and tool_marker.exists():
            failed = True
            if tool_detected is not None:
                tool_detected.set()
            if cancel_event is not None:
                cancel_event.set()
            return
        line = line.strip()
        if not line:
            return
        try:
            raw_event: Any = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(raw_event, dict):
            return
        event = cast("dict[str, Any]", raw_event)
        if _tool_activity(event):
            failed = True
            if tool_detected is not None:
                tool_detected.set()
            if cancel_event is not None:
                cancel_event.set()
            return
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            res = cast("dict[str, Any]", event["result"])
            if res.get("status") in {"ERROR", "INVALID"} or res.get("is_error") is True:
                failed = True
            return
        if event.get("status") in {"ERROR", "INVALID"} or event.get("is_error") is True:
            failed = True
            return
        if event.get("event") == "step_update" and isinstance(event.get("step_update"), dict):
            step = cast("dict[str, Any]", event["step_update"])
            if step.get("step_type") == "agent_response":
                delta = step.get("text_delta")
                if isinstance(delta, str) and delta:
                    emitted += delta
                    sink(delta)
            return
        if event.get("type") == "stream_event":
            inner = event.get("event")
            if (
                isinstance(inner, dict)
                and cast("dict[str, Any]", inner).get("type") == "content_block_delta"
            ):
                inner_dict = cast("dict[str, Any]", inner)
                delta = inner_dict.get("delta")
                if (
                    isinstance(delta, dict)
                    and cast("dict[str, Any]", delta).get("type") == "text_delta"
                ):
                    delta_dict = cast("dict[str, Any]", delta)
                    text = delta_dict.get("text")
                    if isinstance(text, str) and text:
                        emitted += text
                        sink(text)
            return
        if event.get("type") == "assistant":
            message = event.get("message")
            if isinstance(message, dict):
                message_dict = cast("dict[str, Any]", message)
                content = message_dict.get("content")
                if isinstance(content, list):
                    content_list = cast("list[Any]", content)
                    joined = "".join(
                        cast("str", cast("dict[str, Any]", block)["text"])
                        for block in content_list
                        if isinstance(block, dict)
                        and cast("dict[str, Any]", block).get("type") == "text"
                        and isinstance(cast("dict[str, Any]", block).get("text"), str)
                    )
                    if joined:
                        if not emitted:
                            emitted = joined
                            sink(joined)
                        elif joined.startswith(emitted) and len(joined) > len(emitted):
                            delta_text = joined[len(emitted) :]
                            emitted = joined
                            sink(delta_text)
            return

    return on_line


def _agy_schema_instructions(schema: dict[str, Any]) -> str:
    compact = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    return (
        "\n\nExplicit user instruction: Do NOT call any runtime tools or subagents "
        "(never call define_subagent, invoke_subagent, send_message, view_file, run_command, "
        "or search_web). Reply immediately in this turn with only one JSON object "
        f"(no prose and no code fence) that conforms to this JSON Schema: {compact}"
    )


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
    def arguments(request: CompletionRequest, *, stream: bool = False) -> tuple[str, ...]:
        output_format = "stream-json" if stream and request.output_schema is None else "json"
        prompt = request.prompt
        if request.output_schema is not None:
            prompt += _agy_schema_instructions(request.output_schema)
        args = ["-p", prompt, "--output-format", output_format, *COMPLETION_FLAGS]
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
        on_chunk: CompletionChunkSink | None = None,
    ) -> CompletionResult:
        deadline = time.monotonic() + timeout_s
        prompt = request.prompt
        if request.output_schema is not None:
            prompt += _agy_schema_instructions(request.output_schema)
        if len(prompt.encode("utf-8")) > PROMPT_LIMIT_BYTES:
            raise CompletionError(
                "Prompt exceeds CLI argument limit",
                status=400,
                code="context_length_exceeded",
                param="messages",
            )
        chunk_sink = on_chunk if on_chunk is not None else current_chunk_sink()
        streaming = chunk_sink is not None and request.output_schema is None
        try:
            tool_marker = _prepare_policy(cwd)
        except OSError as exc:
            raise CompletionError("Could not prepare Antigravity completion restrictions") from exc

        if cancel.is_set():
            raise CompletionError(
                "Completion cancelled", status=503, code="request_cancelled"
            )
        if tool_marker.exists():
            raise CompletionError(
                "Antigravity attempted model-side tools", code="backend_tool_use"
            )

        process_cancel = threading.Event()
        tool_detected = threading.Event()
        stop_watcher = threading.Event()

        def _watch_marker() -> None:
            while not stop_watcher.is_set() and not process_cancel.is_set():
                if cancel.is_set():
                    process_cancel.set()
                    return
                if tool_marker.exists():
                    tool_detected.set()
                    process_cancel.set()
                    return
                stop_watcher.wait(0.05)

        line_sink = (
            _stream_sink(
                chunk_sink,
                tool_marker,
                cancel_event=process_cancel,
                tool_detected=tool_detected,
            )
            if streaming and chunk_sink is not None
            else discard_output
        )

        watcher_thread = threading.Thread(target=_watch_marker, daemon=True)
        watcher_thread.start()
        try:
            with (
                cancel_processes_when(process_cancel),
                stream_output(line_sink, max_bytes=max_output_bytes),
            ):
                env = self._environment()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                command = resolve_command(
                    self._agent.cmd,
                    self.arguments(request, stream=streaming),
                    env=env,
                    cwd=cwd,
                )
                proc = run_process(command, cwd=cwd, env=env, timeout_s=remaining)
            if tool_marker.exists() or tool_detected.is_set():
                raise CompletionError(
                    "Antigravity attempted model-side tools", code="backend_tool_use"
                )
            if cancel.is_set():
                raise CompletionError(
                    "Completion cancelled", status=503, code="request_cancelled"
                )
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
            if tool_marker.exists() or tool_detected.is_set():
                raise CompletionError(
                    "Antigravity attempted model-side tools", code="backend_tool_use"
                ) from exc
            raise CompletionError(
                "Completion cancelled", status=503, code="request_cancelled"
            ) from exc
        except OutputLimitExceeded as exc:
            raise CompletionError(
                "Completion output limit exceeded", code="output_limit_exceeded"
            ) from exc
        except ProcessDrainError as exc:
            raise CompletionTerminationError() from exc
        finally:
            stop_watcher.set()
            watcher_thread.join()


def _parse_candidate(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    with contextlib.suppress(ValueError, RecursionError):
        parsed: Any = json.loads(text)
        if isinstance(parsed, dict):
            return cast("dict[str, Any]", parsed)
    if "```" in text:
        parts = text.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i]
            inner = block.partition("\n")[2] if "\n" in block else block
            with contextlib.suppress(ValueError, RecursionError):
                parsed = json.loads(inner.strip())
                if isinstance(parsed, dict):
                    return cast("dict[str, Any]", parsed)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(ValueError, RecursionError):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        with contextlib.suppress(ValueError, RecursionError):
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return cast("dict[str, Any]", parsed)
    return None


def parse_completion(
    proc: ProcessResult,
    *,
    structured: bool,
    request_model: str = "default",
    secrets: Mapping[str, str] | None = None,
) -> CompletionResult:
    if proc.timed_out:
        raise CompletionError("Completion deadline expired", status=504, code="timeout")
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed: Any = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict) and _tool_activity(cast("dict[str, Any]", parsed)):
            raise CompletionError("Antigravity reported model-side tools", code="backend_tool_use")
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
        if err_msg:
            err_part = f"; error: {redact(err_msg, secrets=secrets)}"
        elif is_error and not proc.stderr.strip():
            status_desc = ""
            if payload:
                status_desc = str(
                    payload.get("status") or ("error" if payload.get("is_error") else "")
                )
            err_part = f"; status: {status_desc}" if status_desc else "; error: unknown"
        else:
            err_part = ""
        stderr_text = redact(proc.stderr[-2000:], secrets=secrets)
        stderr_part = f"; stderr: {stderr_text}" if proc.stderr.strip() else ""
        message = (
            f"Antigravity CLI completion failed: exit code {proc.exit_code}"
            f"{err_part}{stderr_part}"
        )
        print(message, file=sys.stderr)
        if any(word in diagnostic for word in RATE_LIMIT_MARKERS):
            raise CompletionError(message, status=429, code="rate_limit_exceeded")
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
            candidate = _parse_candidate(payload["response"])
        elif candidate is None and isinstance(payload.get("result"), str):
            candidate = _parse_candidate(payload["result"])
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
