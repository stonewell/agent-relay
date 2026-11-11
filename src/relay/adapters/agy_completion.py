"""Antigravity (`agy`) completion backend.

Translates completion requests into `agy --output-format stream-json`
invocations with prompt delivered via stdin; the events double as the heartbeat
for the shared no-output watchdog.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from relay.adapters.antigravity import agy_envelope, envelope_error
from relay.adapters.claude_completion import (
    command_line_too_long,
    launch_failed,
    rate_limited,
)
from relay.adapters.completion import (
    REASONING_EFFORT_TIERS,
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
    silence_limits,
    stalled_error,
)
from relay.core.env import CompleteEnv, build_env, redact
from relay.core.platform.proc import (
    CommandNotFound,
    OutputLimitExceeded,
    ProcessCancelled,
    ProcessDrainError,
    ProcessResult,
    ProcessStalled,
    cancel_processes_when,
    observe_processes,
    resolve_command,
    run_process,
    stream_output,
)
from relay.core.registry import Agent

_AGY_SETTINGS = (
    '{"toolPermission":"strict",'
    '"allowNonWorkspaceAccess":false,"disableSlashCommands":true,'
    '"permissions":{"allow":[],"ask":[],"deny":['
    '"command(*)","run_command(*)","read_file(*)","write_file(*)","edit_file(*)",'
    '"view_file(*)","list_dir(*)","grep_search(*)","find_by_name(*)","read_url(*)",'
    '"read_url_content(*)","execute_url(*)","search_web(*)","web_search(*)",'
    '"send_message(*)","define_subagent(*)","invoke_subagent(*)","manage_subagents(*)",'
    '"manage_task(*)","schedule(*)","ask_question(*)","generate_image(*)","image_generate(*)",'
    '"mcp(*)","*(*)","*"'
    "]}}"
)
# The named deny entries are deliberate defense in depth: the CLI's honoring of the "*(*)"/"*"
# catch-alls is unverified, so the known host tools stay denied even if wildcards are ignored.
COMPLETION_FLAGS: tuple[str, ...] = (
    "--agent",
    "relay-completion",
    "--disable-slash-commands",
    "--sandbox",
    "--settings",
    _AGY_SETTINGS,
)
ENUMERATION_TIMEOUT_S = 15.0
# Hung turns are stopped by the shared no-output watchdog in run_process (completion.py
# `silence_limits`); its default idle limit matches agy's own 5 min stalled-model retry.
STEP_LOG_INTERVAL_S = 30.0  # cadence of the "agy step" line while a turn is quiet
RESULT_EXIT_GRACE_S = 5.0
MALFORMED_FAIL_FAST_GRACE_S = 5.0
logger = logging.getLogger("uvicorn.error")

# Canonical lowercase markers (also used by gateway rendering); match a lowercased haystack.
MALFORMED_FUNCTION_CALL_MARKERS: tuple[str, ...] = (
    "improperly formatted function call",
    "malformed function call",
    "failed to parse function call",
    "function call is empty",
    "empty function call",
    "retry with a properly formatted function call",
)

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
    "(`<RULE[user_global]>`). Native host CLI function calling is disabled; native function "
    "call blocks or `<call>` tokens are forbidden. Do NOT call host runtime tools or subagents "
    "(`define_subagent`, `invoke_subagent`, `send_message`, `manage_subagents`, `manage_task`, "
    "`schedule`, `ask_question`, `edit_file`, `write_file`, `apply_diff`, `patch`, `save_file`, "
    "`create_file`, `view_file`, `read_file`, `list_dir`, `grep_search`, `find_by_name`, "
    "`run_command`, `search_web`, `read_url_content`, or any other host tool). When client "
    "tools are present in the request header, propose client tool calls as plain JSON text in "
    "the requested envelope (`tool_calls`) rather than repeating past conversation history, "
    "summaries, or task lists, and without invoking any host native tool. Respond directly in "
    "this single turn with only the requested completion output.\n"
)

_AGY_RULE_MD = (
    "---\n"
    "trigger: always_on\n"
    "description: Explicit user instruction override disabling host CLI tools and subagents.\n"
    "---\n"
    "# Explicit User Instructions Override\n"
    "For this workspace session, explicit user instructions take precedence over "
    "`<RULE[user_global]>`:\n"
    "- Native host CLI function calling is disabled; native function call blocks or `<call>` "
    "tokens are forbidden.\n"
    "- Do NOT act as a host task coordinator and do NOT register or invoke any host subagents "
    "(`define_subagent`, `invoke_subagent`, `send_message`, `manage_subagents`, `manage_task`, "
    "`schedule`, `ask_question`).\n"
    "- Do NOT call any host runtime tools (`manage_task`, `schedule`, `ask_question`, `edit_file`, "
    "`write_file`, `apply_diff`, `patch`, `save_file`, `create_file`, `view_file`, `read_file`, "
    "`list_dir`, `grep_search`, `find_by_name`, `run_command`, `search_web`, `read_url_content`, "
    "etc.) or run host commands.\n"
    "- When client tools are listed in the prompt transcript header, propose client tool calls "
    "exclusively as plain JSON text in the requested envelope (`tool_calls`) rather than repeating "
    "past conversation history, summaries, or task lists; never invoke a host native tool.\n"
    "- Answer the supplied prompt directly in a single turn and output the response immediately.\n"
)

_DENY_SCRIPT = """import json
import sys
from pathlib import Path

msg = (
    "Explicit user instruction override: Stateless single-turn completion mode. "
    "Native host function calling is disabled; native function call blocks or <call> tokens "
    "are forbidden. Do NOT invoke host runtime tools or subagents (define_subagent, invoke_subagent, "
    "send_message, manage_subagents, manage_task, schedule, ask_question). Propose client tool calls "
    "as plain JSON text in the requested envelope rather than repeating past conversation text."
)

if "--pre-invocation" in sys.argv[1:]:
    print(json.dumps({"injectSteps": [{"ephemeralMessage": msg}]}))
    raise SystemExit(0)

Path(__file__).with_suffix(".used").touch()
deny_msg = (
    "Relay clients execute tools. Native host function calling is disabled; native function call "
    "blocks or <call> tokens are forbidden. Do NOT invoke host runtime tools or subagents "
    "(define_subagent, invoke_subagent, send_message, manage_subagents, manage_task, schedule, "
    "ask_question). Propose client tool calls exclusively as plain JSON text in the requested "
    "envelope rather than repeating past conversation text."
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
    gemini_home_raw = cwd / ".gemini-home"
    gemini_dir_raw = gemini_home_raw / ".gemini"
    gemini_dir = (gemini_home_raw.resolve() / ".gemini").resolve()
    # Resolved paths cannot be symlinks; the raw paths are the ones checked.
    for directory in (agents_dir, custom_agents, rules_dir, gemini_home_raw, gemini_dir_raw):
        if directory.is_symlink():
            raise OSError(
                "Antigravity policy directory must not be a symbolic link: "
                "Refusing symlinked Antigravity policy path"
            )
        directory.mkdir(mode=0o700, exist_ok=True)
    _write_policy_file(custom_agents / "relay-completion.md", _AGY_AGENT_MD)
    _write_policy_file(rules_dir / "relay-completion.md", _AGY_RULE_MD)
    _write_policy_file(cwd / "GEMINI.md", _AGY_RULE_MD)
    _write_policy_file(gemini_dir / "GEMINI.md", _AGY_RULE_MD)
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
            "PreInvocation": [{"type": "command", "command": command_pre_inv, "timeout": 10}],
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
    # Limits are observed, never guessed: without both reported limits the model is unknown.
    context = _positive(payload.get("context_window")) or _positive(payload.get("contextWindow"))
    output = _positive(payload.get("max_output_tokens")) or _positive(
        payload.get("maxOutputTokens")
    )
    if context is None or output is None:
        return None
    if isinstance(payload.get("model"), str) and payload["model"].strip():
        model_name = payload["model"].strip()
    elif isinstance(payload.get("model_id"), str) and payload["model_id"].strip():
        model_name = payload["model_id"].strip()
    elif request_model and request_model != "default":
        model_name = request_model
    else:
        return None
    return ObservedModel(model_name, context, output)


_SUCCESS_STATUSES = frozenset({"success", "ok", "completed", "done"})


_LOGGED_STATUSES = frozenset(
    {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}
)


def _success_status(status: object) -> bool:
    """No status, or a success variant in any case; ERROR, INVALID, CANCELED etc. fail."""
    return status is None or (isinstance(status, str) and status.lower() in _SUCCESS_STATUSES)


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


def _activity_label(event: dict[str, Any]) -> str:
    label = str(event.get("event") or event.get("type") or "unknown")
    step = event.get("step_update")
    if isinstance(step, dict):
        label += f":{cast('dict[str, Any]', step).get('step_type')}"
    return label


def _is_malformed_step(event: dict[str, Any]) -> tuple[bool, str]:
    """A step reporting a malformed function call, and that error's text.

    Only non-empty error fields are inspected; `message`/`text` count as error fields only
    on an `error_message` step, so model text that merely mentions a marker never matches.
    A `result` event matches only when it failed, and only on its error/status fields.
    """
    if event.get("event") == "result":
        res = event.get("result")
        res_dict = cast("dict[str, Any]", res) if isinstance(res, dict) else {}
        is_err = (
            not _success_status(res_dict.get("status"))
            or res_dict.get("is_error") is True
            or not _success_status(event.get("status"))
            or event.get("is_error") is True
        )
        if not is_err:
            return False, ""
        for source, key in (
            (res_dict, "error_message"),
            (res_dict, "error"),
            (res_dict, "status"),
            (event, "error_message"),
            (event, "error"),
            (event, "status"),
        ):
            val = source.get(key)
            if not val:
                continue
            text = val if isinstance(val, str) else json.dumps(val)
            if any(marker in text.lower() for marker in MALFORMED_FUNCTION_CALL_MARKERS):
                return True, text
        return False, ""

    if event.get("event") != "step_update" or not isinstance(event.get("step_update"), dict):
        return False, ""
    step = cast("dict[str, Any]", event["step_update"])
    error_step = step.get("step_type") == "error_message"
    fields = (
        (step, "error_message", True),
        (step, "message", error_step),
        (step, "error", True),
        (step, "text", error_step),
        (event, "error_message", True),
        (event, "error", True),
    )
    for source, key, is_error_field in fields:
        value = source.get(key)
        if not is_error_field or not value:
            continue
        text = value if isinstance(value, str) else json.dumps(value)
        if any(marker in text.lower() for marker in MALFORMED_FUNCTION_CALL_MARKERS):
            return True, text
    return False, ""


def _stream_sink(
    sink: CompletionChunkSink | None,
    tool_marker: Path | None = None,
    cancel_event: threading.Event | None = None,
    tool_detected: threading.Event | None = None,
    on_activity: Callable[[str], None] | None = None,
    on_result: Callable[[dict[str, Any], str], None] | None = None,
    on_malformed: Callable[[str, str], None] | None = None,
    on_error_result: Callable[[str], None] | None = None,
) -> Callable[[str], None]:
    """Consume stream-json lines. `sink` is None for buffered (tool) turns: events are
    then only liveness signals (`on_activity`) and tool-use detection, never content.
    `on_result` receives a success result event, `on_error_result` a failed one's line."""
    emitted = ""
    failed = False
    emit: CompletionChunkSink = sink if sink is not None else (lambda _: None)

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
        if on_activity is not None:
            on_activity(_activity_label(event))
        if on_malformed is not None:
            is_malformed, malformed_msg = _is_malformed_step(event)
            if is_malformed:
                on_malformed(malformed_msg, line)
        if _tool_activity(event):
            failed = True
            if tool_detected is not None:
                tool_detected.set()
            if cancel_event is not None:
                cancel_event.set()
            return
        if event.get("event") == "result" and isinstance(event.get("result"), dict):
            res = cast("dict[str, Any]", event["result"])
            if not _success_status(res.get("status")) or res.get("is_error") is True:
                failed = True
                if on_error_result is not None:
                    on_error_result(line)
                return
            if on_result is not None:
                on_result(res, line)
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
                    emit(delta)
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
                        emit(text)
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
                            emit(joined)
                        elif joined.startswith(emitted) and len(joined) > len(emitted):
                            delta_text = joined[len(emitted) :]
                            emitted = joined
                            emit(delta_text)
            return

    return on_line


def _agy_schema_instructions(schema: dict[str, Any]) -> str:
    compact = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    # Example shapes only for the relay envelope, and only those its schema permits
    # (tool_choice "none" sets maxItems 0; "required" or a named choice sets minItems 1).
    shapes = ""
    props = schema.get("properties")
    calls = cast("dict[str, Any]", props).get("tool_calls") if isinstance(props, dict) else None
    has_tools = False
    if isinstance(calls, dict) and "content" in cast("dict[str, Any]", props):
        calls_dict = cast("dict[str, Any]", calls)
        if calls_dict.get("maxItems") != 0:
            has_tools = True
            shapes += (
                'Tool call: {"content": null, "tool_calls": [{"name": "<function_name>", '
                '"arguments": {<args>}}]}\n'
                'Or OpenAI tool call: {"content": null, "tool_calls": [{"type": "function", '
                '"function": {"name": "<function_name>", "arguments": {<args>}}}]}\n'
            )
        if not calls_dict.get("minItems"):
            shapes += 'Text: {"content": "<assistant_message>", "tool_calls": []}\n'
    tool_guidance = (
        " Propose client tool calls exclusively as plain JSON text in the requested envelope."
        " When action is required, propose client tool calls in the envelope rather than "
        "repeating past conversation text, summaries, or task lists."
        if has_tools
        else ""
    )
    return (
        "\n\nExplicit user instruction: Native host CLI tools and function calling are disabled; "
        "native function call blocks or tokens are forbidden. "
        "Do NOT call define_subagent, invoke_subagent, send_message, manage_subagents, "
        "manage_task, schedule, ask_question, view_file, read_file, "
        "list_dir, grep_search, find_by_name, run_command, search_web, read_url_content or "
        "any other host runtime tool or subagent; reply directly."
        + tool_guidance
        + " Your response must be exclusively one raw JSON object starting with '{' and ending "
        "with '}', with no markdown fence, no XML tags or function call blocks, and no prose."
        + (f" Use one of these shapes:\n{shapes}" if shapes else "\n")
        + f"It must conform to this JSON Schema: {compact}"
    )


def _resolve_model_and_effort(model: str, effort: str | None) -> tuple[str, str | None]:
    """Resolve (model, effort) into (cli_model, cli_effort) for the Antigravity CLI.

    AGY CLI rejects passing `--effort` when `--model` already carries a baked-in
    effort tier suffix (e.g. `--model gemini-3.8-flash-high --effort medium` fails).
    """
    if model == "default":
        return model, effort

    base, _, suffix = model.rpartition("-")
    if base and suffix in REASONING_EFFORT_TIERS:
        if effort is not None and effort in REASONING_EFFORT_TIERS:
            # Explicit effort overrides the suffix tier; omit --effort flag to avoid CLI conflict
            return f"{base}-{effort}", None
        if effort is not None:
            # A non-tier effort cannot be expressed as a suffix and cannot be combined with
            # one: the baked-in tier wins and the requested effort is dropped.
            logger.warning(
                "agy: ignoring reasoning effort %r for tier-suffixed model %r", effort, model
            )
        # No effort (or a dropped non-tier effort); omit --effort flag
        return model, None

    # Models without tier suffix (e.g. gemini-2.5-pro) use standalone --effort
    return model, effort


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

    def _environment(self, cwd: Path | None = None) -> CompleteEnv:
        parent = os.environ if self._parent_env is None else self._parent_env
        extra: dict[str, str] = {}
        if "GEMINI_API_KEY" not in self._agent.env and parent.get("GEMINI_API_KEY"):
            extra["GEMINI_API_KEY"] = parent["GEMINI_API_KEY"]
        if cwd is not None:
            gemini_home = (cwd / ".gemini-home").resolve()
            extra["GEMINI_CLI_HOME"] = str(gemini_home)
            extra["GEMINI_DIR"] = str((gemini_home / ".gemini").resolve())
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
        output_format = "stream-json" if stream else "json"
        args = ["--output-format", output_format, *COMPLETION_FLAGS]
        model, effort = _resolve_model_and_effort(request.model, request.effort)
        if model != "default":
            args += ["--model", model]
        if effort is not None:
            args += ["--effort", effort]
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
        prompt_bytes = len(prompt.encode("utf-8"))
        chunk_sink = on_chunk if on_chunk is not None else current_chunk_sink()
        streaming = chunk_sink is not None and request.output_schema is None
        try:
            tool_marker = _prepare_policy(cwd)
        except OSError as exc:
            raise CompletionError("Could not prepare Antigravity completion restrictions") from exc

        if cancel.is_set():
            raise CompletionError("Completion cancelled", status=503, code="request_cancelled")
        if tool_marker.exists():
            raise CompletionError("Antigravity attempted model-side tools", code="backend_tool_use")

        process_cancel = threading.Event()
        tool_detected = threading.Event()
        stop_watcher = threading.Event()

        # First result event (arrival time, line) and first malformed-call report (time,
        # error, line): only these lines are kept, as the full stdout is already captured
        # by run_process (and lost only when it raises ProcessCancelled).
        result: list[tuple[float, str]] = []
        malformed: list[tuple[float, str, str]] = []
        # (seconds since start, event index, origin kind, result already arrived) at first marker.
        malformed_info: list[tuple[float, int, str, bool]] = []
        # First failed result event line: classifies the error if agy then hangs silently.
        error_result: list[str] = []
        # Why the relay stopped the tree, set once by the watcher: "cancelled", "result",
        # "malformed" or "tool". A silent (hung) CLI is stopped by run_process itself, which
        # raises ProcessStalled.
        kill_reason: list[str | None] = [None]
        child_pid: list[int | None] = [None]
        step_log_at = [0.0]  # monotonic time before which the quiet-turn line is not repeated

        def _observe_process(phase: str, pid: int | None) -> None:
            if phase == "started":
                child_pid[0] = pid

        def note_malformed(err: str, line: str) -> None:
            if not malformed:
                at = time.monotonic()
                malformed.append((at, err, line))
                try:
                    parsed_line: Any = json.loads(line)
                except (ValueError, RecursionError):
                    parsed_line = None
                kind = "step_update"
                if (
                    isinstance(parsed_line, dict)
                    and cast("dict[str, Any]", parsed_line).get("event") == "result"
                ):
                    kind = "result"
                malformed_info.append((at - started, activity["events"], kind, bool(result)))
                logger.debug(
                    "agy malformed marker first seen: at_s=%.1f event_index=%d origin=%s "
                    "result_already_arrived=%s",
                    at - started,
                    activity["events"],
                    kind,
                    str(bool(result)).lower(),
                )

        def kill(reason: str) -> None:
            if kill_reason[0] is None:
                kill_reason[0] = reason
            process_cancel.set()

        def _watch_marker() -> None:
            while not stop_watcher.is_set() and not process_cancel.is_set():
                if cancel.is_set():
                    kill("cancelled")
                    return
                if result:
                    grace_elapsed = time.monotonic() - result[0][0]
                    if grace_elapsed >= RESULT_EXIT_GRACE_S:
                        logger.warning(
                            "agy result received but process still running after %.1fs grace; "
                            "killing pid=%s events=%d",
                            RESULT_EXIT_GRACE_S,
                            child_pid[0] if child_pid[0] is not None else "-",
                            activity["events"],
                        )
                        kill("result")
                        return
                elif malformed:
                    grace_elapsed = time.monotonic() - malformed[0][0]
                    if grace_elapsed >= MALFORMED_FAIL_FAST_GRACE_S:
                        logger.warning(
                            "agy malformed function call reported; failing fast after %.1fs "
                            "events=%d",
                            MALFORMED_FAIL_FAST_GRACE_S,
                            activity["events"],
                        )
                        kill("malformed")
                        return
                now = time.monotonic()
                quiet = now - activity["at"]
                if quiet >= STEP_LOG_INTERVAL_S and now >= step_log_at[0]:
                    # Names the last step so a long silent turn can be told from a hung one.
                    logger.info(
                        "agy step events=%d last=%s since_event_s=%.0f",
                        activity["events"],
                        activity["label"],
                        quiet,
                    )
                    step_log_at[0] = now + STEP_LOG_INTERVAL_S
                if tool_marker.exists():
                    tool_detected.set()
                    kill("tool")
                    return
                stop_watcher.wait(0.05)

        # Every turn runs stream-json so step events show the CLI is working (the heartbeat
        # the no-output watchdog relies on); only a streaming turn forwards content, others
        # keep it buffered for parsing and envelope validation (sink None).
        started = time.monotonic()
        activity: dict[str, Any] = {"events": 0, "at": started, "label": "none"}

        def note_activity(label: str) -> None:
            activity["events"] += 1
            activity["at"] = time.monotonic()
            activity["label"] = label

        def note_result(_res: dict[str, Any], line: str) -> None:
            if not result:
                result.append((time.monotonic(), line))

        def note_error_result(line: str) -> None:
            if not error_result:
                error_result.append(line)

        line_sink = _stream_sink(
            chunk_sink if streaming else None,
            tool_marker,
            cancel_event=process_cancel,
            tool_detected=tool_detected,
            on_activity=note_activity,
            on_result=note_result,
            on_malformed=note_malformed if request.output_schema is not None else None,
            on_error_result=note_error_result,
        )

        # Before the watcher starts: nothing after its start may skip the join below.
        env = self._environment(cwd)

        def recovered() -> CompletionResult:
            # The relay stopped the tree after its result arrived: the exit status reflects
            # the kill, not the turn, so parse the result event as a clean exit.
            return parse_completion(
                ProcessResult(0, stdout=result[0][1] + "\n", duration_s=time.monotonic() - started),
                structured=request.output_schema is not None,
                request_model=request.model,
                secrets=env,
            )

        def stopped() -> CompletionError:
            # The relay stopped the tree before any result: the first kill reason decides.
            if kill_reason[0] == "malformed":
                line = malformed[0][2] if malformed else ""
                return CompletionInvalid(
                    "Antigravity CLI completion failed: malformed function call",
                    raw=line[:4000],
                    reason="malformed_call",
                )
            return CompletionError("Completion cancelled", status=503, code="request_cancelled")

        watcher_thread = threading.Thread(
            target=contextvars.copy_context().run, args=(_watch_marker,), daemon=True
        )
        watcher_thread.start()
        try:
            with (
                cancel_processes_when(process_cancel),
                observe_processes(_observe_process),
                stream_output(line_sink, max_bytes=max_output_bytes),
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CompletionError("Completion deadline expired", status=504, code="timeout")
                try:
                    command = resolve_command(
                        self._agent.cmd,
                        self.arguments(request, stream=True),
                        env=env,
                        cwd=cwd,
                    )
                except ValueError as exc:
                    # A Windows .cmd shim cannot forward a prompt containing newlines or NUL.
                    logger.warning("Antigravity prompt rejected by .cmd shim: %s", exc)
                    raise CompletionError(
                        "Antigravity CLI .cmd shim cannot receive a multi-line prompt; "
                        "configure agents.agy.cmd as the native executable",
                        status=503,
                        code="unavailable",
                    ) from exc
                if command_line_too_long(command):
                    # The prompt is on stdin; only CLI flags/configuration can cause this limit.
                    raise CompletionError(
                        "Command line exceeds the Windows limit",
                        status=400,
                        code="context_length_exceeded",
                        param="messages",
                    )
                first_output_s, idle_s = silence_limits(
                    prompt_bytes,
                    remaining,
                    first_output_s=self._agent.first_output_timeout_s,
                    idle_s=self._agent.idle_timeout_s,
                )
                proc = run_process(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=prompt,
                    timeout_s=remaining,
                    first_output_timeout_s=first_output_s,
                    idle_timeout_s=idle_s,
                )
            if tool_marker.exists() or tool_detected.is_set():
                raise CompletionError(
                    "Antigravity attempted model-side tools", code="backend_tool_use"
                )
            # A kill can also surface as an ordinary (non-zero) exit rather than
            # ProcessCancelled when the leader exits while the kill is in flight.
            if kill_reason[0] is not None:
                if result:
                    return recovered()
                raise stopped()
            if cancel.is_set() and not result:
                raise CompletionError("Completion cancelled", status=503, code="request_cancelled")
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
            # A result that already arrived wins over any later kill, including a caller
            # cancel landing during the exit grace.
            if result:
                return recovered()
            raise stopped() from exc
        except ProcessStalled as exc:
            # The shared watchdog stopped a silent CLI. A tool attempt, an arrived result or
            # an earlier relay kill (e.g. malformed call) still decides the outcome first.
            if tool_marker.exists() or tool_detected.is_set():
                raise CompletionError(
                    "Antigravity attempted model-side tools", code="backend_tool_use"
                ) from exc
            if result:
                return recovered()
            if kill_reason[0] is not None:
                raise stopped() from exc
            if error_result:
                # agy reported a failed turn, then hung: classify that error (context length,
                # rate limit, ...) as a normal exit would. No exit code: the watchdog killed it.
                return parse_completion(
                    ProcessResult(
                        None,
                        stdout=error_result[0] + "\n",
                        duration_s=time.monotonic() - started,
                    ),
                    structured=request.output_schema is not None,
                    request_model=request.model,
                    secrets=env,
                )
            raise stalled_error("Antigravity", exc.limit, exc.silent_s, prompt_bytes) from exc
        except OutputLimitExceeded as exc:
            raise CompletionError(
                "Completion output limit exceeded", code="output_limit_exceeded"
            ) from exc
        except ProcessDrainError as exc:
            raise CompletionTerminationError() from exc
        except OSError as exc:
            raise launch_failed("Antigravity", exc) from exc
        finally:
            stop_watcher.set()
            watcher_thread.join()
            # Separates "working" (steps seen recently) from "hung" (none) after the fact.
            now = time.monotonic()
            logger.log(
                logging.WARNING if sys.exc_info()[0] is not None else logging.INFO,
                "agy turn ended after %.1fs: events=%d last=%s %.1fs before end kill=%s",
                now - started,
                activity["events"],
                activity["label"],
                now - activity["at"],
                kill_reason[0] or "-",
            )
            if malformed_info:
                m_at, m_index, _m_kind, m_result = malformed_info[0]
                logger.debug(
                    "agy malformed summary: malformed_at_s=%.1f malformed_event_index=%d "
                    "total_events=%d total_duration_s=%.1f result_before_marker=%s "
                    "fail_fast_fired=%s",
                    m_at,
                    m_index,
                    activity["events"],
                    now - started,
                    str(m_result).lower(),
                    str(kill_reason[0] == "malformed").lower(),
                )


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
    stdout_malformed = False
    malformed_line = ""
    events = 0
    malformed_at = -1
    last_result_at = -1
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed: Any = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(parsed, dict):
            parsed_dict = cast("dict[str, Any]", parsed)
            if _tool_activity(parsed_dict):
                raise CompletionError("Antigravity reported model-side tools", code="backend_tool_use")
            events += 1
            if parsed_dict.get("event") == "result":
                last_result_at = events
            if not stdout_malformed:
                is_m, _ = _is_malformed_step(parsed_dict)
                if is_m:
                    stdout_malformed = True
                    malformed_line = line
                    malformed_at = events
    payload = agy_envelope(proc.stdout)
    # Only a success status (or none) is success; CANCELED/INTERRUPTED/WAITING/RUNNING also fail.
    payload_failed = payload is not None and (
        not _success_status(payload.get("status")) or payload.get("is_error") is True
    )
    if stdout_malformed:
        # agy may recover from a malformed call itself and end with a successful result.
        recovered = (
            proc.exit_code == 0
            and payload is not None
            and not payload_failed
            and last_result_at > malformed_at
        )
        status = payload.get("status") if payload is not None else None
        logger.debug(
            "agy malformed step: last_result_status=%s is_error=%s payload=%s exit_code=%s "
            "events=%d decision=%s",
            (str(status).upper() if str(status).upper() in _LOGGED_STATUSES else "other")
            if status is not None
            else "none",
            payload.get("is_error") is True if payload is not None else "none",
            str(payload is not None).lower(),
            proc.exit_code,
            events,
            "ignored_successful_result" if recovered else "failed",
        )
        if recovered:
            stdout_malformed = False
    is_error = stdout_malformed or payload_failed
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
        malformed = stdout_malformed or any(
            marker in diagnostic for marker in MALFORMED_FUNCTION_CALL_MARKERS
        )
        if malformed:
            err_part = "; malformed function call"
        elif err_msg:
            err_part = f"; error: {redact(err_msg, secrets=secrets)}"
        elif is_error:
            status_desc = ""
            if payload:
                status_desc = str(
                    payload.get("status") or ("error" if payload.get("is_error") else "")
                )
            err_part = f"; status: {status_desc}" if status_desc else "; error: unknown"
        else:
            err_part = ""
        message = f"Antigravity CLI completion failed: exit code {proc.exit_code}{err_part}"
        # Raw stderr is a private diagnostic: log it redacted, never return it to the client.
        logger.warning("%s; stderr: %s", message, redact(proc.stderr[-2000:], secrets=secrets))
        # Only stderr and the error text: payload JSON may hold numbers such as token counts.
        rate_text = (proc.stderr + " " + (err_msg or "")).lower()
        if rate_limited(rate_text):
            raise CompletionError(message, status=429, code="rate_limit_exceeded")
        if structured and (
            (payload is not None and payload.get("status") == "INVALID") or malformed
        ):
            candidate_raw = (
                str(payload.get("response") or payload.get("result") or "")
                if payload is not None
                else ""
            )
            raw_line = candidate_raw or malformed_line
            raw_content = raw_line[:4000] if raw_line else proc.stdout
            raise CompletionInvalid(
                message,
                raw=raw_content[:4000],
                usage=_usage(payload) if payload else None,
                reason="malformed_call" if malformed else "schema_violation",
            )
        raise CompletionError(message)
    if payload is None:
        message = f"Antigravity CLI returned no completion object: exit code {proc.exit_code}"
        logger.warning("%s; stderr: %s", message, redact(proc.stderr[-2000:], secrets=secrets))
        raise CompletionInvalid(message, raw=proc.stdout[:4000], reason="no_envelope")
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
                reason="no_envelope",
            )
        envelope = cast("dict[str, Any]", candidate)
        return CompletionResult(
            envelope=envelope, usage=usage, raw=json.dumps(envelope), observed=observed
        )
    content = payload.get("response")
    if content is None:
        content = payload.get("result")
    if not isinstance(content, str):
        raise CompletionInvalid("CLI returned no assistant text", usage=usage, reason="no_text")
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
