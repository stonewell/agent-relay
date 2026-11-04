from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

import relay.adapters.agy_completion as agy_completion
from relay.adapters.agy_completion import (
    COMPLETION_FLAGS,
    MALFORMED_FAIL_FAST_GRACE_S,
    MALFORMED_FUNCTION_CALL_MARKERS,
    AgyCompletionAdapter,
    _observed,  # pyright: ignore[reportPrivateUsage]
    parse_completion,
)
from relay.adapters.antigravity import agy_envelope, envelope_error
from relay.adapters.completion import (
    CompletionCapable,
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionUsage,
    ObservedModel,
    stream_completion_chunks,
)
from relay.core.platform.proc import ProcessCancelled, ProcessResult, ResolvedCommand
from relay.core.registry import Agent, Config
from relay.gateway.discovery import default_adapters
from relay.gateway.models import ModelCatalog
from relay.gateway.protocol import ChatRequest, FunctionDefinition, Message, Tool
from relay.gateway.render import render_transcript


def agy_response(**values: Any) -> ProcessResult:
    data = {
        "status": "SUCCESS",
        "response": "hello from agy",
        "conversation_id": "test-conv-123",
        **values,
    }
    return ProcessResult(0, json.dumps(data))


def test_agy_completion_adapter_arguments() -> None:
    req = CompletionRequest("test prompt", "default")
    args = AgyCompletionAdapter.arguments(req)
    assert args[:4] == ("-p", "-", "--output-format", "json")
    assert COMPLETION_FLAGS == (
        "--agent",
        "relay-completion",
        "--disable-slash-commands",
        "--sandbox",
        "--settings",
        agy_completion._AGY_SETTINGS,  # pyright: ignore[reportPrivateUsage]
    )
    assert "--mode" not in COMPLETION_FLAGS
    assert "plan" not in COMPLETION_FLAGS
    assert "--mode" not in args
    assert "plan" not in args
    for flag in COMPLETION_FLAGS:
        assert flag in args
    assert "--model" not in args

    # stream=True with output_schema is None uses stream-json
    args_stream = AgyCompletionAdapter.arguments(req, stream=True)
    assert args_stream[:4] == ("-p", "-", "--output-format", "stream-json")
    for flag in COMPLETION_FLAGS:
        assert flag in args_stream

    req_model = CompletionRequest("test prompt", "gemini-2.5-pro", effort="high")
    args_model = AgyCompletionAdapter.arguments(req_model)
    assert "--model" in args_model
    assert args_model[args_model.index("--model") + 1] == "gemini-2.5-pro"
    assert "--effort" in args_model
    assert args_model[args_model.index("--effort") + 1] == "high"

    # prompt is piped via stdin so schema instructions are not in args
    req_schema = CompletionRequest("test prompt", "default", output_schema={"type": "object"})
    args_schema = AgyCompletionAdapter.arguments(req_schema)
    assert args_schema[:4] == ("-p", "-", "--output-format", "json")
    assert "--json-schema" not in args_schema
    assert "test prompt" not in args_schema

    args_schema_stream = AgyCompletionAdapter.arguments(req_schema, stream=True)
    # Tool turns stream events for liveness; content stays buffered by the caller.
    assert args_schema_stream[:4] == ("-p", "-", "--output-format", "stream-json")
    assert "--json-schema" not in args_schema_stream
    assert "test prompt" not in args_schema_stream


def test_agy_buffered_sink_reports_activity_without_content() -> None:
    labels: list[str] = []
    sink = agy_completion._stream_sink(None, on_activity=labels.append)  # pyright: ignore[reportPrivateUsage]
    sink(json.dumps({"event": "init"}))
    sink(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "x"},
            }
        )
    )
    sink("not json")
    assert labels == ["init", "step_update:agent_response"]


def test_agy_settings_and_completion_flags() -> None:
    settings_raw: str = agy_completion._AGY_SETTINGS  # pyright: ignore[reportPrivateUsage]
    assert '"agentMode":""' not in settings_raw
    assert "agentMode" not in settings_raw
    expected_deny = [
        "command(*)",
        "read_file(*)",
        "write_file(*)",
        "read_url(*)",
        "execute_url(*)",
        "mcp(*)",
    ]
    assert f'"deny":{json.dumps(expected_deny, separators=(",", ":"))}' in settings_raw

    settings: dict[str, Any] = json.loads(settings_raw)
    assert "agentMode" not in settings
    assert settings["permissions"] == {"allow": [], "ask": [], "deny": expected_deny}
    assert settings["toolPermission"] == "strict"
    assert settings["allowNonWorkspaceAccess"] is False
    assert settings["disableSlashCommands"] is True

    expected_flags = (
        "--agent",
        "relay-completion",
        "--disable-slash-commands",
        "--sandbox",
        "--settings",
        settings_raw,
    )
    assert expected_flags == COMPLETION_FLAGS
    for flag in COMPLETION_FLAGS:
        assert "agentMode" not in flag
        assert "plan" not in flag
        assert "--mode" not in flag


def test_agy_prepare_policy(tmp_path: Path) -> None:
    stale_marker = tmp_path / ".agents" / "deny_tools.used"
    stale_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stale_marker.touch()
    assert stale_marker.exists()

    marker = agy_completion._prepare_policy(tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert marker == stale_marker
    assert not marker.exists()

    agents_dir = tmp_path / ".agents"
    custom_agents = agents_dir / "agents"
    rules_dir = agents_dir / "rules"
    for directory in (agents_dir, custom_agents, rules_dir):
        assert directory.is_dir()
        if sys.platform != "win32":
            assert (directory.stat().st_mode & 0o777) == 0o700

    policy_files = [
        custom_agents / "relay-completion.md",
        rules_dir / "relay-completion.md",
        tmp_path / "GEMINI.md",
        agents_dir / "rules.json",
        agents_dir / "agents.json",
        agents_dir / "skills.json",
        agents_dir / "plugins.json",
        agents_dir / "hooks.json",
        agents_dir / "deny_tools.py",
        agents_dir / "mcp_config.json",
    ]
    for file_path in policy_files:
        assert file_path.is_file()
        if sys.platform != "win32":
            assert (file_path.stat().st_mode & 0o777) == 0o600

    # 1. relay-completion.md contains tools: [] and enableSubagentTools: false
    agent_text = (custom_agents / "relay-completion.md").read_text(encoding="utf-8")
    assert "name: relay-completion" in agent_text
    assert "tools: []" in agent_text
    assert "enableSubagentTools: false" in agent_text
    assert "enable_subagent_tools: false" in agent_text
    assert "commandExecutionPolicy: off" in agent_text
    assert "<RULE[user_global]>" in agent_text
    assert "define_subagent" in agent_text
    assert "invoke_subagent" in agent_text
    assert "send_message" in agent_text
    assert "edit_file" in agent_text
    assert "write_file" in agent_text
    assert "apply_diff" in agent_text

    # 2. GEMINI.md and rules/relay-completion.md with trigger: always_on
    rule_text = (rules_dir / "relay-completion.md").read_text(encoding="utf-8")
    gemini_text = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert "trigger: always_on" in rule_text
    assert "trigger: always_on" in gemini_text
    assert "Explicit User Instructions Override" in rule_text
    assert "Explicit User Instructions Override" in gemini_text
    assert "<RULE[user_global]>" in rule_text
    assert "<RULE[user_global]>" in gemini_text
    assert "define_subagent" in rule_text
    assert "invoke_subagent" in rule_text
    assert "edit_file" in rule_text
    assert "write_file" in rule_text
    assert "edit_file" in gemini_text
    assert "write_file" in gemini_text

    # 3. rules.json, agents.json, skills.json, plugins.json with 0o600 and valid JSON {"exclude": [...]}
    rules_data: dict[str, Any] = json.loads((agents_dir / "rules.json").read_text(encoding="utf-8"))
    assert rules_data == {"exclude": ["user_global", "~/.gemini/GEMINI.md"]}

    skills_data: dict[str, Any] = json.loads(
        (agents_dir / "skills.json").read_text(encoding="utf-8")
    )
    assert skills_data == {"exclude": ["*"]}

    plugins_data: dict[str, Any] = json.loads(
        (agents_dir / "plugins.json").read_text(encoding="utf-8")
    )
    assert plugins_data == {"exclude": ["*"]}

    agents_data: dict[str, Any] = json.loads(
        (agents_dir / "agents.json").read_text(encoding="utf-8")
    )
    assert "exclude" in agents_data
    assert isinstance(agents_data["exclude"], list)
    assert "relay-completion" not in agents_data["exclude"]
    for expected_agent in (
        "explore",
        "flash",
        "tests",
        "pro",
        "pro-gemini",
        "pro-flash-high",
        "review",
    ):
        assert expected_agent in agents_data["exclude"]

    # 4. hooks.json with both PreInvocation and PreToolUse hooks
    hooks_data: dict[str, Any] = json.loads((agents_dir / "hooks.json").read_text(encoding="utf-8"))
    assert "relay-deny-tools" in hooks_data
    relay_hooks: dict[str, Any] = hooks_data["relay-deny-tools"]
    assert "PreInvocation" in relay_hooks
    assert "PreToolUse" in relay_hooks

    pre_inv: list[dict[str, Any]] = relay_hooks["PreInvocation"]
    assert len(pre_inv) == 1
    assert pre_inv[0]["type"] == "command"
    assert "--pre-invocation" in pre_inv[0]["command"]
    assert pre_inv[0]["timeout"] == 10

    pre_tool: list[dict[str, Any]] = relay_hooks["PreToolUse"]
    assert len(pre_tool) == 1
    assert pre_tool[0]["matcher"] == "*"
    assert pre_tool[0]["hooks"][0]["type"] == "command"
    assert "--pre-invocation" not in pre_tool[0]["hooks"][0]["command"]
    assert pre_tool[0]["hooks"][0]["timeout"] == 10

    # mcp_config.json
    assert json.loads((agents_dir / "mcp_config.json").read_text(encoding="utf-8")) == {
        "mcpServers": {}
    }


def test_agy_prepare_policy_agent_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake_home"
    home_agents_dir = fake_home / ".gemini" / "config" / "agents"
    home_agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home_agents_dir / "custom-agent.md").write_text("name: custom-agent", encoding="utf-8")
    (home_agents_dir / "relay-completion.md").write_text("name: relay-completion", encoding="utf-8")
    (home_agents_dir / "another-agent.md").write_text("name: another-agent", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: fake_home)

    work_dir = tmp_path / "work"
    work_dir.mkdir(mode=0o700)
    agy_completion._prepare_policy(work_dir)  # pyright: ignore[reportPrivateUsage]

    agents_data: dict[str, Any] = json.loads(
        (work_dir / ".agents" / "agents.json").read_text(encoding="utf-8")
    )
    assert "custom-agent" in agents_data["exclude"]
    assert "another-agent" in agents_data["exclude"]
    assert "relay-completion" not in agents_data["exclude"]


def test_agy_deny_tools_execution(tmp_path: Path) -> None:
    agy_completion._prepare_policy(tmp_path)  # pyright: ignore[reportPrivateUsage]
    script = tmp_path / ".agents" / "deny_tools.py"
    marker = tmp_path / ".agents" / "deny_tools.used"
    assert not marker.exists()

    # Running deny_tools.py --pre-invocation outputs valid JSON with injectSteps and ephemeralMessage
    # and does NOT create deny_tools.used
    res_pre = subprocess.run(
        [sys.executable, str(script), "--pre-invocation"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_pre.returncode == 0
    assert not marker.exists()
    pre_data: dict[str, Any] = json.loads(res_pre.stdout)
    assert "injectSteps" in pre_data
    inject_steps = cast("list[dict[str, Any]]", pre_data["injectSteps"])
    assert isinstance(inject_steps, list)
    assert len(inject_steps) == 1
    assert "ephemeralMessage" in inject_steps[0]
    assert "Stateless single-turn completion mode" in inject_steps[0]["ephemeralMessage"]

    # Running deny_tools.py without --pre-invocation creates deny_tools.used and outputs decision: deny
    res_deny = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res_deny.returncode == 0
    assert marker.exists()
    deny_data: dict[str, Any] = json.loads(res_deny.stdout)
    assert deny_data.get("decision") == "deny"
    assert deny_data.get("allow_tool") is False
    expected_deny = (
        "Relay clients execute tools. Native function calling is disabled; native function call "
        "blocks or <call> tokens are forbidden. Tool proposals must be emitted exclusively as plain "
        "JSON text in the requested envelope without calling native tools."
    )
    assert deny_data.get("reason") == expected_deny
    assert deny_data.get("deny_reason") == expected_deny


@pytest.mark.parametrize(
    ("rel_path", "is_dir"),
    [
        (Path(".agents"), True),
        (Path(".agents") / "agents", True),
        (Path(".agents") / "rules", True),
        (Path("GEMINI.md"), False),
        (Path(".agents") / "rules.json", False),
        (Path(".agents") / "agents.json", False),
        (Path(".agents") / "skills.json", False),
        (Path(".agents") / "plugins.json", False),
        (Path(".agents") / "hooks.json", False),
        (Path(".agents") / "mcp_config.json", False),
        (Path(".agents") / "deny_tools.py", False),
        (Path(".agents") / "agents" / "relay-completion.md", False),
        (Path(".agents") / "rules" / "relay-completion.md", False),
        (Path(".agents") / "deny_tools.used", False),
    ],
)
def test_agy_prepare_policy_symlink_rejection(tmp_path: Path, rel_path: Path, is_dir: bool) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir(mode=0o700)
    target = tmp_path / "target"
    if is_dir:
        target.mkdir(mode=0o700)
    else:
        target.touch()

    symlink_path = work_dir / rel_path
    symlink_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    symlink_path.symlink_to(target)

    with pytest.raises(OSError, match="symbolic link"):
        agy_completion._prepare_policy(work_dir)  # pyright: ignore[reportPrivateUsage]


def test_agy_prepare_policy_windows_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    agy_completion._prepare_policy(tmp_path)  # pyright: ignore[reportPrivateUsage]
    hooks_data: dict[str, Any] = json.loads(
        (tmp_path / ".agents" / "hooks.json").read_text(encoding="utf-8")
    )
    relay_hooks: dict[str, Any] = hooks_data["relay-deny-tools"]
    inv_cmd: str = relay_hooks["PreInvocation"][0]["command"]
    tool_cmd: str = relay_hooks["PreToolUse"][0]["hooks"][0]["command"]
    assert inv_cmd.startswith("& ")
    assert inv_cmd.endswith("; exit $LASTEXITCODE")
    assert "--pre-invocation" in inv_cmd
    assert tool_cmd.startswith("& ")
    assert tool_cmd.endswith("; exit $LASTEXITCODE")
    assert "--pre-invocation" not in tool_cmd


def test_agy_prompt_framing_and_schema_overrides() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["name"],
    }
    schema_inst = agy_completion._agy_schema_instructions(schema)  # pyright: ignore[reportPrivateUsage]
    assert "Explicit user instruction:" in schema_inst
    assert "exclusively one raw JSON object" in schema_inst
    assert "starting with '{' and ending with '}'" in schema_inst
    assert "<call>" not in schema_inst
    for name in (
        "define_subagent",
        "invoke_subagent",
        "send_message",
        "view_file",
        "run_command",
        "search_web",
    ):
        assert name in schema_inst
    assert json.dumps(schema, separators=(",", ":"), sort_keys=True) in schema_inst
    # A non-envelope schema gets no conflicting envelope shapes.
    assert "tool_calls" not in schema_inst
    assert "Tool call:" not in schema_inst and "Text:" not in schema_inst

    tool_shape = (
        'Tool call: {"content": null, "tool_calls": [{"name": "<function_name>", '
        '"arguments": {<args>}}]}'
    )
    text_shape = 'Text: {"content": "<assistant_message>", "tool_calls": []}'

    def envelope(**limits: int) -> dict[str, Any]:
        calls: dict[str, Any] = {"type": "array", **limits}
        return {"type": "object", "properties": {"content": {}, "tool_calls": calls}}

    auto = agy_completion._agy_schema_instructions(envelope())  # pyright: ignore[reportPrivateUsage]
    assert tool_shape in auto and text_shape in auto
    none = agy_completion._agy_schema_instructions(envelope(maxItems=0))  # pyright: ignore[reportPrivateUsage]
    assert tool_shape not in none and text_shape in none
    required = agy_completion._agy_schema_instructions(envelope(minItems=1))  # pyright: ignore[reportPrivateUsage]
    assert tool_shape in required and text_shape not in required

    # ChatRequest without tools
    req_no_tools = ChatRequest(
        model="gemini-2.5-pro",
        messages=[Message(role="user", content="Tell me a joke.")],
    )
    transcript_no_tools = render_transcript(req_no_tools, model="gemini-2.5-pro")
    assert "<RULE[user_global]>" in transcript_no_tools
    assert "define_subagent" in transcript_no_tools
    assert "invoke_subagent" in transcript_no_tools
    assert "send_message" in transcript_no_tools
    assert "view_file" in transcript_no_tools
    assert "read_file" in transcript_no_tools
    assert "list_dir" in transcript_no_tools
    assert "grep_search" in transcript_no_tools
    assert "find_by_name" in transcript_no_tools
    assert "run_command" in transcript_no_tools
    assert "search_web" in transcript_no_tools
    assert "read_url_content" in transcript_no_tools
    assert "Return only the assistant's text." in transcript_no_tools

    # ChatRequest with tools
    tool = Tool(
        type="function",
        function=FunctionDefinition(
            name="get_weather",
            description="Fetch weather",
            parameters={"type": "object", "properties": {"loc": {"type": "string"}}},
        ),
    )
    req_with_tools = ChatRequest(
        model="gemini-2.5-pro",
        messages=[Message(role="user", content="What is the weather in Seattle?")],
        tools=[tool],
    )
    transcript_with_tools = render_transcript(req_with_tools, model="gemini-2.5-pro")
    assert "<RULE[user_global]>" in transcript_with_tools
    assert "define_subagent" in transcript_with_tools
    assert "invoke_subagent" in transcript_with_tools
    assert "send_message" in transcript_with_tools
    assert "view_file" in transcript_with_tools
    assert "read_file" in transcript_with_tools
    assert "run_command" in transcript_with_tools
    assert "search_web" in transcript_with_tools
    assert "read_url_content" in transcript_with_tools
    assert (
        'Return exactly one JSON envelope {"content":string|null,"tool_calls":'
        in transcript_with_tools
    )
    assert "Propose only supplied functions; the client executes them." in transcript_with_tools


def test_agy_stream_json_ndjson_envelope_and_parse() -> None:
    ndjson = "\n".join(
        [
            json.dumps({"event": "init", "session_id": "sess-1"}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_type": "agent_response", "text_delta": "Hel"},
                }
            ),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"step_type": "agent_response", "text_delta": "lo"},
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": "Hello",
                        "usage": {"input_tokens": 12, "output_tokens": 4},
                    },
                }
            ),
        ]
    )
    env = agy_envelope(ndjson)
    assert env is not None
    assert env["status"] == "SUCCESS"
    assert env["response"] == "Hello"

    proc = ProcessResult(0, ndjson)
    result = parse_completion(proc, structured=False)
    assert result.content == "Hello"
    assert result.usage == CompletionUsage(12, 4, None, None)

    # Structured output from stream-json NDJSON
    ndjson_struct = "\n".join(
        [
            json.dumps({"event": "init", "session_id": "sess-2"}),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "structured_output": {"name": "test_tool", "arguments": {}},
                    },
                }
            ),
        ]
    )
    proc_struct = ProcessResult(0, ndjson_struct)
    res_struct = parse_completion(proc_struct, structured=True)
    assert res_struct.envelope == {"name": "test_tool", "arguments": {}}


def test_agy_stream_sink_and_tool_activity(tmp_path: Path) -> None:
    chunks: list[str] = []
    marker = tmp_path / "deny_tools.used"
    sink = agy_completion._stream_sink(chunks.append, tool_marker=marker)  # pyright: ignore[reportPrivateUsage]

    # agent_response text_delta
    sink(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "Hel"},
            }
        )
    )
    sink(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "lo"},
            }
        )
    )
    assert chunks == ["Hel", "lo"]

    # content_block_delta stream_event
    sink(
        json.dumps(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": " world"},
                },
            }
        )
    )
    assert chunks == ["Hel", "lo", " world"]

    # assistant message snapshot
    sink(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Hello world!"}]},
            }
        )
    )
    assert chunks == ["Hel", "lo", " world", "!"]

    # stops immediately on tool activity: step_type == "tool"
    chunks.clear()
    sink2 = agy_completion._stream_sink(chunks.append, tool_marker=marker)  # pyright: ignore[reportPrivateUsage]
    sink2(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "a"},
            }
        )
    )
    assert chunks == ["a"]
    sink2(
        json.dumps(
            {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "bash"}}
        )
    )
    sink2(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "b"},
            }
        )
    )
    assert chunks == ["a"]

    # stops immediately on subagent_info
    chunks.clear()
    sink3 = agy_completion._stream_sink(chunks.append, tool_marker=marker)  # pyright: ignore[reportPrivateUsage]
    sink3(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "subagent_info": {"name": "sub"}},
            }
        )
    )
    sink3(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "x"},
            }
        )
    )
    assert chunks == []

    # stops immediately when tool_marker exists
    chunks.clear()
    marker.touch()
    sink4 = agy_completion._stream_sink(chunks.append, tool_marker=marker)  # pyright: ignore[reportPrivateUsage]
    sink4(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "x"},
            }
        )
    )
    assert chunks == []
    marker.unlink()

    # stops immediately on result with is_error
    chunks.clear()
    sink5 = agy_completion._stream_sink(chunks.append, tool_marker=marker)  # pyright: ignore[reportPrivateUsage]
    sink5(json.dumps({"event": "result", "result": {"is_error": True}}))
    sink5(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "x"},
            }
        )
    )
    assert chunks == []


def test_agy_complete_streaming_and_tool_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relay.core.platform.proc import _OUTPUT_SINK  # pyright: ignore[reportPrivateUsage]

    agent = Agent(cmd="agy")
    adapter = AgyCompletionAdapter(agent)

    recorded_lines = [
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "H"},
            }
        ),
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "i"},
            }
        ),
        json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Hi"}}),
    ]

    def mock_run_process(cmd: Any, **kwargs: Any) -> ProcessResult:
        entry = _OUTPUT_SINK.get()
        if entry is not None:
            sink, _ = entry
            for line in recorded_lines:
                sink(line)
        return ProcessResult(0, "\n".join(recorded_lines))

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return ("agy",)

    monkeypatch.setattr(agy_completion, "run_process", mock_run_process)
    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)

    chunks: list[str] = []
    res = adapter.complete(
        CompletionRequest("hello", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=5,
        max_output_bytes=10000,
        on_chunk=chunks.append,
    )
    assert res.content == "Hi"
    assert chunks == ["H", "i"]

    # Also test stream_completion_chunks context manager
    chunks_ctx: list[str] = []
    with stream_completion_chunks(chunks_ctx.append):
        res2 = adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=10000,
        )
    assert res2.content == "Hi"
    assert chunks_ctx == ["H", "i"]

    # Test tool_marker exists raises backend_tool_use
    def mock_run_tool(cmd: Any, **kwargs: Any) -> ProcessResult:
        (tmp_path / ".agents" / "deny_tools.used").touch()
        return ProcessResult(0, json.dumps({"status": "SUCCESS", "response": "Hi"}))

    monkeypatch.setattr(agy_completion, "run_process", mock_run_tool)
    with pytest.raises(CompletionError) as exc_info:
        adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=10000,
        )
    assert exc_info.value.code == "backend_tool_use"
    (tmp_path / ".agents" / "deny_tools.used").unlink(missing_ok=True)

    # Test stdout line with tool activity raises backend_tool_use in parse_completion
    proc_tool = ProcessResult(
        0,
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "tool", "tool_name": "execute_command"},
            }
        )
        + "\n"
        + json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done"}}),
    )
    with pytest.raises(CompletionError) as exc_info2:
        parse_completion(proc_tool, structured=False)
    assert exc_info2.value.code == "backend_tool_use"

    # Test subagent_info in stdout raises backend_tool_use in parse_completion
    proc_subagent = ProcessResult(
        0,
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "subagent_info": {"name": "sub"}},
            }
        )
        + "\n"
        + json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done"}}),
    )
    with pytest.raises(CompletionError) as exc_info3:
        parse_completion(proc_subagent, structured=False)
    assert exc_info3.value.code == "backend_tool_use"


@pytest.mark.parametrize("streaming", [False, True])
def test_agy_watcher_cancels_process_when_deny_tools_used_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, streaming: bool
) -> None:
    agent = Agent(cmd=sys.executable)
    adapter = AgyCompletionAdapter(agent)
    marker = tmp_path / ".agents" / "deny_tools.used"

    # Child process touches deny_tools.used and then sleeps
    code = "import pathlib, sys, time\npathlib.Path(sys.argv[1]).touch()\ntime.sleep(10)\n"

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code, str(marker))

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)

    chunks: list[str] = []
    on_chunk = chunks.append if streaming else None

    start = time.monotonic()
    with pytest.raises(CompletionError) as exc_info:
        adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=10.0,
            max_output_bytes=10000,
            on_chunk=on_chunk,
        )
    duration = time.monotonic() - start

    assert duration < 3.0, f"Process took {duration}s, did not cancel promptly"
    assert exc_info.value.code == "backend_tool_use"
    assert exc_info.value.status == 502
    assert isinstance(exc_info.value.__cause__, ProcessCancelled)
    assert marker.exists()


def test_agy_stream_sink_triggers_cancellation_on_tool_activity(tmp_path: Path) -> None:
    chunks: list[str] = []
    cancel_event = threading.Event()
    tool_detected = threading.Event()
    marker = tmp_path / "deny_tools.used"

    sink = agy_completion._stream_sink(  # pyright: ignore[reportPrivateUsage]
        chunks.append,
        tool_marker=marker,
        cancel_event=cancel_event,
        tool_detected=tool_detected,
    )

    # Regular agent text chunk passes through
    sink(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "Hello"},
            }
        )
    )
    assert chunks == ["Hello"]
    assert not cancel_event.is_set()
    assert not tool_detected.is_set()

    # Tool activity triggers cancel_event and tool_detected
    sink(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "tool", "tool_name": "run_command"},
            }
        )
    )
    assert cancel_event.is_set()
    assert tool_detected.is_set()

    # Further text is ignored
    sink(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": " world"},
            }
        )
    )
    assert chunks == ["Hello"]

    # Also test tool_marker exists triggers cancel_event and tool_detected
    chunks.clear()
    cancel_event2 = threading.Event()
    tool_detected2 = threading.Event()
    marker.touch()
    sink2 = agy_completion._stream_sink(  # pyright: ignore[reportPrivateUsage]
        chunks.append,
        tool_marker=marker,
        cancel_event=cancel_event2,
        tool_detected=tool_detected2,
    )
    sink2(
        json.dumps(
            {
                "event": "step_update",
                "step_update": {"step_type": "agent_response", "text_delta": "ignored"},
            }
        )
    )
    assert chunks == []
    assert cancel_event2.is_set()
    assert tool_detected2.is_set()
    marker.unlink()


def test_agy_streaming_tool_activity_stdout_cancels_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(cmd=sys.executable)
    adapter = AgyCompletionAdapter(agent)

    # Child process prints a tool activity event and sleeps
    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'bash'}}), flush=True)\n"
        "time.sleep(10)\n"
    )

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)

    chunks: list[str] = []
    start = time.monotonic()
    with pytest.raises(CompletionError) as exc_info:
        adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=10.0,
            max_output_bytes=10000,
            on_chunk=chunks.append,
        )
    duration = time.monotonic() - start

    assert duration < 3.0, f"Process took {duration}s, did not cancel promptly"
    assert exc_info.value.code == "backend_tool_use"
    assert exc_info.value.status == 502
    assert isinstance(exc_info.value.__cause__, ProcessCancelled)


def test_agy_caller_cancel_raises_request_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(cmd=sys.executable)
    adapter = AgyCompletionAdapter(agent)

    # 1. Caller cancel event is pre-set before calling complete
    cancel_preset = threading.Event()
    cancel_preset.set()
    with pytest.raises(CompletionError) as exc_info_preset:
        adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=cancel_preset,
            timeout_s=10.0,
            max_output_bytes=10000,
        )
    assert exc_info_preset.value.code == "request_cancelled"
    assert exc_info_preset.value.status == 503

    # 2. Caller cancel event is set during process execution (mid-flight disconnect)
    code = "import time; time.sleep(10)\n"

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)

    cancel_mid = threading.Event()
    timer = threading.Timer(0.05, cancel_mid.set)
    timer.start()

    start = time.monotonic()
    try:
        with pytest.raises(CompletionError) as exc_info_mid:
            adapter.complete(
                CompletionRequest("hello", "default"),
                cwd=tmp_path,
                cancel=cancel_mid,
                timeout_s=10.0,
                max_output_bytes=10000,
            )
    finally:
        timer.cancel()
    duration = time.monotonic() - start

    assert duration < 3.0, f"Process took {duration}s, did not cancel promptly"
    assert exc_info_mid.value.code == "request_cancelled"
    assert exc_info_mid.value.status == 503
    assert isinstance(exc_info_mid.value.__cause__, ProcessCancelled)


def test_agy_watcher_thread_cleanly_terminates_without_leaking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_threads: list[TrackingThread] = []
    orig_thread = threading.Thread

    class TrackingThread(orig_thread):
        target_func: Any

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            target = kwargs.get("target") or (args[0] if args else None)
            wrapped = kwargs.get("args") or ()
            # The adapter runs its watcher through contextvars.Context.run(fn).
            self.target_func = wrapped[0] if wrapped and callable(wrapped[0]) else target
            captured_threads.append(self)

    monkeypatch.setattr(agy_completion.threading, "Thread", TrackingThread)

    agent = Agent(cmd=sys.executable)
    adapter = AgyCompletionAdapter(agent)

    # 1. Normal completion scenario
    success_json = json.dumps({"status": "SUCCESS", "response": "ok"})
    code_success = f"import sys; print({success_json!r})\n"

    def fake_resolve_success(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code_success)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve_success)
    adapter.complete(
        CompletionRequest("hello", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=5.0,
        max_output_bytes=10000,
    )

    watcher_normal = [
        t for t in captured_threads if getattr(t.target_func, "__name__", "") == "_watch_marker"
    ]
    assert len(watcher_normal) == 1
    assert not watcher_normal[0].is_alive()

    # 2. Cancelled via tool marker
    captured_threads.clear()
    marker = tmp_path / ".agents" / "deny_tools.used"
    code_marker = "import pathlib, sys, time\npathlib.Path(sys.argv[1]).touch()\ntime.sleep(10)\n"

    def fake_resolve_marker(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code_marker, str(marker))

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve_marker)
    with pytest.raises(CompletionError):
        adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5.0,
            max_output_bytes=10000,
        )

    watcher_marker = [
        t for t in captured_threads if getattr(t.target_func, "__name__", "") == "_watch_marker"
    ]
    assert len(watcher_marker) == 1
    assert not watcher_marker[0].is_alive()

    # 3. Cancelled via client disconnect
    captured_threads.clear()
    code_sleep = "import time; time.sleep(10)\n"

    def fake_resolve_sleep(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code_sleep)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve_sleep)
    cancel_evt = threading.Event()
    timer = orig_thread(target=lambda: (time.sleep(0.05), cancel_evt.set()))
    timer.start()
    try:
        with pytest.raises(CompletionError):
            adapter.complete(
                CompletionRequest("hello", "default"),
                cwd=tmp_path,
                cancel=cancel_evt,
                timeout_s=5.0,
                max_output_bytes=10000,
            )
    finally:
        timer.join()

    watcher_cancel = [
        t for t in captured_threads if getattr(t.target_func, "__name__", "") == "_watch_marker"
    ]
    assert len(watcher_cancel) == 1
    assert not watcher_cancel[0].is_alive()

    # 4. Cancelled via tool event in stdout stream
    captured_threads.clear()
    tool_json = json.dumps(
        {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "bash"}}
    )
    code_tool = f"import sys, time; print({tool_json!r}, flush=True); time.sleep(10)\n"

    def fake_resolve_tool(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code_tool)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve_tool)
    chunks: list[str] = []
    with pytest.raises(CompletionError):
        adapter.complete(
            CompletionRequest("hello", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5.0,
            max_output_bytes=10000,
            on_chunk=chunks.append,
        )
    watcher_stream = [
        t for t in captured_threads if getattr(t.target_func, "__name__", "") == "_watch_marker"
    ]
    assert len(watcher_stream) == 1
    assert not watcher_stream[0].is_alive()


def test_agy_completion_large_prompt_piped_via_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_stdin: list[str | None] = []
    captured_command: list[Any] = []

    def mock_run_process(cmd: Any, **kwargs: Any) -> ProcessResult:
        captured_command.append(cmd)
        captured_stdin.append(kwargs.get("stdin"))
        return ProcessResult(0, json.dumps({"status": "SUCCESS", "response": "large prompt ok"}))

    def fake_resolve(cmd: str, args: tuple[str, ...], **_k: object) -> ResolvedCommand:
        return ResolvedCommand(argv=(cmd, *args))

    monkeypatch.setattr(agy_completion, "run_process", mock_run_process)
    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)

    adapter = AgyCompletionAdapter(Agent(cmd="agy"))
    assert isinstance(adapter, CompletionCapable)

    # 500 KB prompt (> 128k context) succeeds and is piped via stdin
    large_prompt = "a" * (500 * 1024)
    req_large = CompletionRequest(large_prompt)
    res = adapter.complete(
        req_large,
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=10,
        max_output_bytes=1024,
    )
    assert res.content == "large prompt ok"
    assert len(captured_stdin) == 1
    assert captured_stdin[0] == large_prompt
    assert len(captured_command) == 1
    cmd_args = captured_command[0].argv
    assert large_prompt not in cmd_args
    assert cmd_args[:3] == ("agy", "-p", "-")

    # Prompt + schema_instructions also piped via stdin
    captured_stdin.clear()
    req_schema = CompletionRequest(
        large_prompt, output_schema={"type": "object", "properties": {"x": {"type": "string"}}}
    )

    def mock_run_schema(cmd: Any, **kwargs: Any) -> ProcessResult:
        captured_stdin.append(kwargs.get("stdin"))
        payload: dict[str, Any] = {"content": "ok", "tool_calls": []}
        return ProcessResult(0, json.dumps({"status": "SUCCESS", "response": json.dumps(payload)}))

    monkeypatch.setattr(agy_completion, "run_process", mock_run_schema)
    res_schema = adapter.complete(
        req_schema,
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=10,
        max_output_bytes=1024,
    )
    assert res_schema.envelope == {"content": "ok", "tool_calls": []}
    assert len(captured_stdin) == 1
    assert captured_stdin[0] is not None
    assert captured_stdin[0].startswith(large_prompt)
    assert '"type":"object"' in captured_stdin[0]


def test_agy_parse_completion_plain_and_usage() -> None:
    proc = agy_response(
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 20,
        }
    )
    result = parse_completion(proc, structured=False)
    assert result.content == "hello from agy"
    assert result.usage == CompletionUsage(100, 50, 20, None)
    assert result.finish_reason == "stop"


def test_agy_parse_completion_structured() -> None:
    tool_call = {"name": "write", "arguments": {"path": "a.txt"}}
    proc = agy_response(structured_output=tool_call)
    result = parse_completion(proc, structured=True)
    assert result.envelope == tool_call

    proc_str = agy_response(response=json.dumps(tool_call))
    res_str = parse_completion(proc_str, structured=True)
    assert res_str.envelope == tool_call

    proc_fenced = agy_response(response=f"```json\n{json.dumps(tool_call)}\n```")
    res_fenced = parse_completion(proc_fenced, structured=True)
    assert res_fenced.envelope == tool_call

    proc_multiline = agy_response(
        response=f"Prose preamble\n{json.dumps({'old': 1})}\n{json.dumps(tool_call)}\n"
    )
    res_multiline = parse_completion(proc_multiline, structured=True)
    assert res_multiline.envelope == tool_call

    proc_prose_fenced = agy_response(
        response="Here is the result:\n```json\n" + json.dumps(tool_call, indent=2) + "\n```"
    )
    res_prose_fenced = parse_completion(proc_prose_fenced, structured=True)
    assert res_prose_fenced.envelope == tool_call


def test_agy_parse_completion_error_sanitization() -> None:
    # Error envelope
    proc_err = ProcessResult(
        1, json.dumps({"status": "ERROR", "error": "secret credentials invalid"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_err, structured=False, secrets={"KEY": "secret"})
    assert "secret" not in str(exc_info.value)
    assert "<redacted>" in str(exc_info.value)
    assert "credentials invalid" in str(exc_info.value)
    assert exc_info.value.code == "completion_failed"
    assert "exit code 1" in str(exc_info.value)

    # Non-json output
    proc_invalid = ProcessResult(0, "not json at all")
    with pytest.raises(CompletionInvalid):
        parse_completion(proc_invalid, structured=False)


def test_agy_parse_completion_exit_code_zero_error() -> None:
    # 1. Exit code 0 with status: ERROR and string error
    proc_err_str = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": "User authentication failed"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_err_str, structured=False)
    msg = str(exc_info.value)
    expected_msg = (
        "Antigravity CLI completion failed: exit code 0; error: User authentication failed"
    )
    assert msg == expected_msg
    assert "; stderr:" not in msg
    assert exc_info.value.status == 502

    # 2. Exit code 0 with status: ERROR and no error field or stderr -> status: ERROR
    proc_err_no_error = ProcessResult(0, json.dumps({"status": "ERROR"}))
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_err_no_error, structured=False)
    msg_no_error = str(exc_info.value)
    assert msg_no_error == "Antigravity CLI completion failed: exit code 0; status: ERROR"
    assert "; stderr:" not in msg_no_error

    # 3. Dict error structures
    # 3a. dict with 'message'
    proc_dict_msg = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {"message": "Invalid auth token"}})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_dict_msg, structured=False)
    expected_dict_msg = "Antigravity CLI completion failed: exit code 0; error: Invalid auth token"
    assert str(exc_info.value) == expected_dict_msg
    assert "; stderr:" not in str(exc_info.value)

    # 3b. empty dict in error falls through to payload message
    proc_dict_empty = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {}, "message": "API key expired"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_dict_empty, structured=False)
    assert "exit code 0; error: API key expired" in str(exc_info.value)

    # 3c. dict with 'detail'
    proc_dict_detail = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {"detail": "Backend rejected payload"}})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_dict_detail, structured=False)
    assert "exit code 0; error: Backend rejected payload" in str(exc_info.value)

    # 3d. dict with nested 'error'
    proc_dict_err = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {"error": "Nested error string"}})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_dict_err, structured=False)
    assert "exit code 0; error: Nested error string" in str(exc_info.value)

    # 3e. dict with deeply nested 'error' dict
    proc_deep_err = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {"error": {"message": "Deep nested error"}}})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_deep_err, structured=False)
    assert "exit code 0; error: Deep nested error" in str(exc_info.value)

    # 3f. arbitrary dict serialized to JSON
    proc_dict_json = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {"code": 500, "flag": False}})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_dict_json, structured=False)
    assert 'error: {"code": 500, "flag": false}' in str(exc_info.value)

    # 3g. payload level 'message' / 'detail' / 'reason' when error is None
    proc_payload_msg = ProcessResult(
        0, json.dumps({"status": "ERROR", "message": "Top-level failure message"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_payload_msg, structured=False)
    assert "exit code 0; error: Top-level failure message" in str(exc_info.value)

    proc_payload_detail = ProcessResult(
        0, json.dumps({"status": "ERROR", "detail": "Top-level failure detail"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_payload_detail, structured=False)
    assert "exit code 0; error: Top-level failure detail" in str(exc_info.value)

    proc_payload_reason = ProcessResult(
        0, json.dumps({"status": "ERROR", "reason": "Top-level failure reason"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_payload_reason, structured=False)
    assert "exit code 0; error: Top-level failure reason" in str(exc_info.value)

    # 4. Rate limit markers in error raise HTTP 429
    proc_rate_limit = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": "rate limit exceeded: please slow down"})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_rate_limit, structured=False)
    assert exc_info.value.status == 429
    assert exc_info.value.code == "rate_limit_exceeded"
    assert "rate limit exceeded" in str(exc_info.value)

    proc_quota = ProcessResult(
        0, json.dumps({"status": "ERROR", "error": {"message": "Resource quota reached"}})
    )
    with pytest.raises(CompletionError) as exc_info:
        parse_completion(proc_quota, structured=False)
    assert exc_info.value.status == 429
    assert exc_info.value.code == "rate_limit_exceeded"
    assert "quota reached" in str(exc_info.value)


def test_agy_envelope_error_variants() -> None:
    assert envelope_error({}) is None
    assert envelope_error({"error": ""}) is None
    assert envelope_error({"error": "   "}) is None
    assert envelope_error({"error": "simple message"}) == "simple message"
    assert envelope_error({"error": {"message": "msg"}}) == "msg"
    assert envelope_error({"error": {"detail": "det"}}) == "det"
    assert envelope_error({"error": {"error": "err"}}) == "err"
    assert envelope_error({"error": {"error": {"message": "nested"}}}) == "nested"
    assert envelope_error({"error": {}}) is None
    assert envelope_error({"error": {}, "message": "API key expired"}) == "API key expired"
    assert envelope_error({"error": {"unknown": 123}}) == '{"unknown": 123}'
    assert envelope_error({"error": 500}) == "500"
    assert envelope_error({"message": "top msg"}) == "top msg"
    assert envelope_error({"detail": "top detail"}) == "top detail"
    assert envelope_error({"reason": "top reason"}) == "top reason"


@pytest.mark.parametrize(
    ("proc", "status", "code"),
    [
        (ProcessResult(None, timed_out=True), 504, "timeout"),
        (
            ProcessResult(1, stderr="context_length_exceeded: secret"),
            400,
            "context_length_exceeded",
        ),
        (ProcessResult(1, stderr="other secret"), 502, "completion_failed"),
        (
            ProcessResult(3, stderr="error: Individual quota reached: secret"),
            429,
            "rate_limit_exceeded",
        ),
    ],
)
def test_agy_process_error_categories_do_not_expose_stderr(
    proc: ProcessResult, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        caplog.at_level(logging.WARNING, logger="uvicorn.error"),
        pytest.raises(CompletionError) as caught,
    ):
        parse_completion(proc, structured=False)
    assert caught.value.status == status
    assert caught.value.code == code
    # Raw stderr is logged for operators, never returned as the public message.
    assert "secret" not in str(caught.value)
    if status in (429, 502):
        assert str(caught.value) == (
            f"Antigravity CLI completion failed: exit code {proc.exit_code}"
        )
        assert "secret" in caplog.text


@pytest.mark.parametrize("status", ["CANCELED", "INTERRUPTED", "WAITING", "RUNNING", "UNKNOWN"])
def test_agy_non_success_status_fails(status: str) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(agy_response(status=status), structured=False)
    assert not isinstance(caught.value, CompletionInvalid)
    assert str(caught.value) == f"Antigravity CLI completion failed: exit code 0; status: {status}"


def test_agy_success_or_missing_status_succeeds() -> None:
    assert parse_completion(agy_response(), structured=False).content == "hello from agy"
    no_status = ProcessResult(0, json.dumps({"conversation_id": "c", "response": "ok"}))
    assert parse_completion(no_status, structured=False).content == "ok"


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (OSError("[WinError 206] The filename or extension is too long"), 502, "completion_failed"),
        (ValueError("batch arguments cannot contain newlines"), 503, "unavailable"),
    ],
)
def test_agy_launch_failures_become_completion_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    status: int,
    code: str,
) -> None:
    def resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        if isinstance(failure, ValueError):
            raise failure
        return ("agy",)

    def run(*_a: object, **_k: object) -> ProcessResult:
        raise failure

    monkeypatch.setattr(agy_completion, "resolve_command", resolve)
    monkeypatch.setattr(agy_completion, "run_process", run)
    with pytest.raises(CompletionError) as caught:
        AgyCompletionAdapter(Agent(cmd="agy")).complete(
            CompletionRequest("line one\n\nline two"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert (caught.value.status, caught.value.code) == (status, code)
    assert "WinError" not in str(caught.value)


def test_agy_final_command_line_length_is_context_length_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(_cmd: str, args: tuple[str, ...], **_k: object) -> ResolvedCommand:
        return ResolvedCommand(argv=("agy.exe", *args, "x" * 33_000))

    def run(*_a: object, **_k: object) -> ProcessResult:
        raise AssertionError("must not spawn")

    monkeypatch.setattr(agy_completion, "resolve_command", resolve)
    monkeypatch.setattr(agy_completion, "run_process", run)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(CompletionError) as caught:
        AgyCompletionAdapter(Agent(cmd="agy")).complete(
            CompletionRequest("test"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=5,
            max_output_bytes=1000,
        )
    assert (caught.value.status, caught.value.code) == (400, "context_length_exceeded")


SLUGS = ("gemini-4-argon-medium", "claude-sonnet-5-5-high")
FAMILIES = ("gemini-4-argon", "claude-sonnet-5-5")


def fake_slugs(*_: object, **__: object) -> tuple[str, ...]:
    return SLUGS


def fake_no_slugs(*_: object, **__: object) -> tuple[str, ...]:
    return ()


def test_default_adapters_and_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy_completion, "_enumerated_models", fake_slugs)
    cfg = Config.model_validate(
        {
            "version": 1,
            "agents": {
                "agy": {
                    "enabled": True,
                    "cmd": "agy",
                    "models": {"default": "default"},
                },
                "claude": {
                    "enabled": False,
                    "cmd": "claude",
                },
            },
        }
    )
    adapters = default_adapters(cfg)
    assert "agy" in adapters
    assert "claude" not in adapters
    assert isinstance(adapters["agy"], AgyCompletionAdapter)

    # ModelCatalog per-agent aliases come from `agy models`
    catalog = ModelCatalog(cfg, adapters)
    assert catalog.agent_aliases["agy"] == ("default", *SLUGS)
    assert set(catalog.models) == {
        "agy",
        *(f"agy/{slug}" for slug in ("default", *SLUGS, *FAMILIES)),
    }

    # With explicit models override
    catalog_explicit = ModelCatalog(cfg, adapters, models=["m1", "m2"])
    assert catalog_explicit.agent_aliases["agy"] == ("m1", "m2")
    assert set(catalog_explicit.models) == {"agy", "agy/m1", "agy/m2"}

    # Enumeration failure falls back to the configured models
    monkeypatch.setattr(agy_completion, "_enumerated_models", fake_no_slugs)
    fallback = default_adapters(cfg)["agy"]
    assert isinstance(fallback, AgyCompletionAdapter)
    assert fallback.default_aliases == ("default",)


def test_agy_catalog_lists_enumerated_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy_completion, "_enumerated_models", fake_slugs)
    cfg = Config.model_validate({"version": 1, "agents": {"agy": {"enabled": True, "cmd": "agy"}}})
    catalog = ModelCatalog(cfg, default_adapters(cfg))
    assert set(catalog.models) == {
        "agy",
        "agy/default",
        *(f"agy/{slug}" for slug in (*SLUGS, *FAMILIES)),
    }
    assert [row["id"] for row in catalog.listing()["data"]] == [
        f"agy/{family}" for family in FAMILIES
    ]
    for slug in SLUGS:
        relay = catalog.models[f"agy/{slug}"].public(None)["relay"]
        assert relay["context_length"] is None
        assert relay["max_output_tokens"] is None


def test_agy_observed_limits_are_never_invented() -> None:
    # 1. No reported limits -> unknown, never default Gemini limits
    assert _observed({}) is None
    assert _observed({}, request_model="gemini-2.5-flash") is None
    assert _observed({"model": "m", "context_window": 500_000}) is None
    assert _observed({"model": "m", "max_output_tokens": 16_384}) is None

    # 2. Limits reported without any model name -> unknown model
    assert _observed({"context_window": 500_000, "max_output_tokens": 16_384}) is None

    # 3. Payload model / limits
    obs_payload = _observed(
        {"model": "custom-agy-model", "context_window": 500_000, "max_output_tokens": 16_384}
    )
    assert obs_payload == ObservedModel("custom-agy-model", 500_000, 16_384)
    obs_request = _observed(
        {"contextWindow": 500_000, "maxOutputTokens": 16_384}, request_model="gemini-2.5-flash"
    )
    assert obs_request == ObservedModel("gemini-2.5-flash", 500_000, 16_384)

    # 4. When modelUsage is present
    obs_usage = _observed(
        {"modelUsage": {"custom-entry": {"contextWindow": 100_000, "maxOutputTokens": 4_096}}}
    )
    assert obs_usage == ObservedModel("custom-entry", 100_000, 4_096)

    # 5. parse_completion forwards request_model and reports unknown limits as None
    result = parse_completion(agy_response(), structured=False, request_model="gemini-2.5-flash")
    assert result.observed is None
    reported = agy_response(context_window=200_000, max_output_tokens=8_192)
    result = parse_completion(reported, structured=False, request_model="gemini-2.5-flash")
    assert result.observed == ObservedModel("gemini-2.5-flash", 200_000, 8_192)


def test_agy_malformed_function_call_markers() -> None:
    assert MALFORMED_FUNCTION_CALL_MARKERS == (
        "improperly formatted function call",
        "malformed function call",
        "failed to parse function call",
        "function call is empty",
        "empty function call",
    )
    assert MALFORMED_FAIL_FAST_GRACE_S == 5.0


def test_agy_parse_completion_malformed_function_call_invalid() -> None:
    # 1. Exit code != 0 with malformed function call in stderr and structured=True -> CompletionInvalid
    proc_malformed = ProcessResult(
        1,
        "",
        stderr="google.api_core.exceptions.InvalidArgument: 400 Malformed function call: Function call is empty",
    )
    with pytest.raises(CompletionInvalid) as exc_info:
        parse_completion(proc_malformed, structured=True)
    assert exc_info.value.code == "invalid_completion"
    # Fixed text for the repair prompt; the raw provider stderr is not the message.
    assert str(exc_info.value) == (
        "Antigravity CLI completion failed: exit code 1; malformed function call"
    )
    assert "InvalidArgument" not in str(exc_info.value)

    # 2. structured=False does not convert malformed function call to CompletionInvalid -> CompletionError
    with pytest.raises(CompletionError) as exc_err:
        parse_completion(proc_malformed, structured=False)
    assert not isinstance(exc_err.value, CompletionInvalid)
    assert exc_err.value.code == "completion_failed"

    # 3. Payload with status: INVALID and structured=True -> CompletionInvalid with raw and usage
    usage_data = {"input_tokens": 100, "output_tokens": 20}
    proc_invalid_payload = ProcessResult(
        0,
        json.dumps(
            {
                "status": "INVALID",
                "error": "Failed to parse function call",
                "usage": usage_data,
                "response": '{"broken": json',
            }
        ),
    )
    with pytest.raises(CompletionInvalid) as exc_info2:
        parse_completion(proc_invalid_payload, structured=True)
    assert exc_info2.value.code == "invalid_completion"
    assert exc_info2.value.raw == '{"broken": json'
    assert exc_info2.value.usage == CompletionUsage(prompt_tokens=100, completion_tokens=20)

    # 4. Each marker in MALFORMED_FUNCTION_CALL_MARKERS triggers CompletionInvalid when structured=True
    for marker in MALFORMED_FUNCTION_CALL_MARKERS:
        proc_marker = ProcessResult(1, "", stderr=f"Backend error: {marker}")
        with pytest.raises(CompletionInvalid):
            parse_completion(proc_marker, structured=True)

    # 5. Rate limit still takes precedence over malformed function call
    proc_rate_limit = ProcessResult(
        1,
        "",
        stderr="Rate limit exceeded: please slow down; malformed function call",
    )
    with pytest.raises(CompletionError) as exc_rl:
        parse_completion(proc_rate_limit, structured=True)
    assert not isinstance(exc_rl.value, CompletionInvalid)
    assert exc_rl.value.status == 429
    assert exc_rl.value.code == "rate_limit_exceeded"

    # 6. Context length exceeded still takes precedence over malformed function call
    proc_ctx = ProcessResult(
        1,
        "",
        stderr="context_length_exceeded: prompt is too long; malformed function call",
    )
    with pytest.raises(CompletionError) as exc_ctx:
        parse_completion(proc_ctx, structured=True)
    assert not isinstance(exc_ctx.value, CompletionInvalid)
    assert exc_ctx.value.status == 400
    assert exc_ctx.value.code == "context_length_exceeded"


def test_agy_watchdog_kills_silent_tool_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shared run_process watchdog replaced agy's own IDLE_TIMEOUT_S check; a child
    # that never writes is stopped by the first-output limit from the agent config.
    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", "import time; time.sleep(30)")

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    adapter = AgyCompletionAdapter(Agent(cmd=sys.executable, first_output_timeout_s=0.3))
    start = time.monotonic()
    with pytest.raises(CompletionError) as exc_info:
        adapter.complete(
            CompletionRequest("hi", "default", output_schema={"type": "object"}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
        )
    assert exc_info.value.code == "timeout"
    assert exc_info.value.status == 504
    assert "no output" in str(exc_info.value)
    assert "first-output limit" in str(exc_info.value)
    assert time.monotonic() - start < 15


def test_agy_non_streaming_turn_uses_stream_json_and_is_watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Formerly a non-live (json) turn had no idle detection. Every turn now runs stream-json
    # so the watchdog sees events; silence after an event is an idle kill.
    seen: list[tuple[str, ...]] = []
    code = (
        "import json, time\n"
        "print(json.dumps({'event':'step_update','step_update':{'step_type':'thinking'}}),"
        " flush=True)\n"
        "time.sleep(30)\n"
    )

    def fake_resolve(_cmd: str, args: tuple[str, ...], **_k: object) -> tuple[str, ...]:
        seen.append(args)
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    adapter = AgyCompletionAdapter(Agent(cmd=sys.executable, idle_timeout_s=0.3))
    start = time.monotonic()
    with pytest.raises(CompletionError) as exc_info:
        adapter.complete(
            CompletionRequest("hi", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
        )
    assert seen[0][2:4] == ("--output-format", "stream-json")
    assert (exc_info.value.status, exc_info.value.code) == (504, "timeout")
    assert "idle limit" in str(exc_info.value)
    assert f"prompt {len(b'hi')} bytes" in str(exc_info.value)
    assert time.monotonic() - start < 15


def test_agy_non_streaming_stream_json_result_parses_like_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = (
        "import json\n"
        "print(json.dumps({'event':'step_update','step_update':{'step_type':'agent_response',"
        "'text_delta':'ok'}}))\n"
        "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'ok',"
        "'usage':{'input_tokens':3,'output_tokens':1}}}))\n"
    )

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    result = AgyCompletionAdapter(Agent(cmd=sys.executable)).complete(
        CompletionRequest("hi", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=30.0,
        max_output_bytes=10000,
    )
    assert result.content == "ok"
    assert result.usage == CompletionUsage(3, 1, None, None)


def test_agy_result_wins_over_idle_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI prints its result and hangs; the idle limit fires before the exit grace.
    monkeypatch.setattr(agy_completion, "RESULT_EXIT_GRACE_S", 30.0)
    code = (
        "import json, time\n"
        "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'OK'}}),"
        " flush=True)\n"
        "time.sleep(30)\n"
    )

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    start = time.monotonic()
    result = AgyCompletionAdapter(Agent(cmd=sys.executable, idle_timeout_s=0.3)).complete(
        CompletionRequest("hi", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=30.0,
        max_output_bytes=10000,
    )
    assert result.content == "OK"
    assert time.monotonic() - start < 15


def test_agy_logs_last_step_while_live_turn_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agy_completion, "STEP_LOG_INTERVAL_S", 0.1)
    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event':'step_update','step_update':{'step_type':'thinking'}}),"
        " flush=True)\n"
        "time.sleep(0.6)\n"
        "print(json.dumps({'event':'result','result':{'status':'SUCCESS','structured_output':{}}}))\n"
    )

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    adapter = AgyCompletionAdapter(Agent(cmd=sys.executable))
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = adapter.complete(
            CompletionRequest("hi", "default", output_schema={"type": "object"}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
        )
    del result
    assert "agy step events=1 last=step_update:thinking" in caplog.text


def test_agy_result_exit_grace_returns_result_and_kills_hung_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agy_completion, "RESULT_EXIT_GRACE_S", 0.2)
    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':'OK'}}), flush=True)\n"
        "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'OK','usage':{'input_tokens':10,'output_tokens':2}}}), flush=True)\n"
        "time.sleep(30)\n"
    )

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    adapter = AgyCompletionAdapter(Agent(cmd=sys.executable))
    chunks: list[str] = []
    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        result = adapter.complete(
            CompletionRequest("hi", "default"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
            on_chunk=chunks.append,
        )
    elapsed = time.monotonic() - start
    assert result.content == "OK"
    assert result.usage == CompletionUsage(10, 2, None, None)
    assert chunks == ["OK"]
    assert elapsed < 5.0
    assert (
        "agy result received but process still running after 0.2s grace; killing pid="
        in caplog.text
    )
    assert "events=2" in caplog.text


def test_agy_malformed_function_call_fail_fast_kills_hung_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agy_completion, "MALFORMED_FAIL_FAST_GRACE_S", 0.2)
    code = (
        "import json, sys, time\n"
        "print(json.dumps({'event':'step_update','step_update':{'step_type':'error_message','message':'Your previous response contained an improperly formatted function call'}}), flush=True)\n"
        "time.sleep(30)\n"
    )

    def fake_resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (sys.executable, "-c", code)

    monkeypatch.setattr(agy_completion, "resolve_command", fake_resolve)
    adapter = AgyCompletionAdapter(Agent(cmd=sys.executable))
    start = time.monotonic()
    with (
        caplog.at_level(logging.WARNING, logger="uvicorn.error"),
        pytest.raises(CompletionInvalid) as exc_info,
    ):
        adapter.complete(
            CompletionRequest("hi", "default", output_schema={"type": "object"}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 5.0
    assert "malformed function call" in exc_info.value.args[0]
    assert "improperly formatted function call" in exc_info.value.raw
    assert "agy malformed function call reported; failing fast after 0.2s events=1" in caplog.text


@pytest.mark.parametrize("status", ["SUCCESS", "success", "OK", "Completed", "done", None])
def test_agy_stream_sink_accepts_success_status_variants(status: str | None) -> None:
    results: list[dict[str, Any]] = []
    sink = agy_completion._stream_sink(None, on_result=lambda res, _line: results.append(res))  # pyright: ignore[reportPrivateUsage]
    payload: dict[str, Any] = {"response": "x"} if status is None else {"status": status}
    sink(json.dumps({"event": "result", "result": payload}))
    assert len(results) == 1


@pytest.mark.parametrize("status", ["ERROR", "error", "INVALID", "CANCELED", "RUNNING"])
def test_agy_stream_sink_rejects_failure_statuses(status: str) -> None:
    results: list[dict[str, Any]] = []
    sink = agy_completion._stream_sink(None, on_result=lambda res, _line: results.append(res))  # pyright: ignore[reportPrivateUsage]
    sink(json.dumps({"event": "result", "result": {"status": status}}))
    assert results == []


def test_agy_parse_completion_accepts_lowercase_success_status() -> None:
    assert parse_completion(agy_response(status="success"), structured=False).content == (
        "hello from agy"
    )


def test_agy_malformed_step_checks_only_non_empty_error_fields() -> None:
    check = agy_completion._is_malformed_step  # pyright: ignore[reportPrivateUsage]

    def step(**fields: Any) -> dict[str, Any]:
        return {"event": "step_update", "step_update": fields}

    # Null or empty error fields are not errors.
    assert check(step(step_type="agent_response", error=None, error_message="")) == (False, "")
    # Model text that mentions a marker is not an error, even beside an unrelated error.
    assert check(step(step_type="agent_response", text="a malformed function call")) == (
        False,
        "",
    )
    assert check(
        step(step_type="agent_response", text_delta="malformed function call", error="busy")
    ) == (False, "")
    # The reported message is the error field that matched.
    assert check(
        step(
            step_type="agent_response", error_message="busy", error="Failed to parse function call"
        )
    ) == (True, "Failed to parse function call")
    assert check(step(step_type="error_message", message="Empty function call")) == (
        True,
        "Empty function call",
    )
    nested = {
        "event": "step_update",
        "step_update": {"step_type": "planner"},
        "error": {"message": "malformed function call"},
    }
    matched, message = check(nested)
    assert matched and "malformed function call" in message


def test_agy_environment_failure_leaves_no_watcher_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[threading.Thread] = []
    orig_thread = threading.Thread

    class TrackingThread(orig_thread):
        def start(self) -> None:
            started.append(self)
            super().start()

    def broken_environment() -> Any:
        raise RuntimeError("environment unavailable")

    monkeypatch.setattr(agy_completion.threading, "Thread", TrackingThread)
    adapter = AgyCompletionAdapter(Agent(cmd="agy"))
    monkeypatch.setattr(adapter, "_environment", broken_environment)
    with pytest.raises(RuntimeError):
        adapter.complete(
            CompletionRequest("hi", "default", output_schema={"type": "object"}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
        )
    assert not any(thread.is_alive() for thread in started)


_RESULT_LINES = [
    json.dumps(
        {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "OK"}}
    ),
    json.dumps(
        {
            "event": "result",
            "result": {
                "status": "SUCCESS",
                "response": "OK",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        }
    ),
]


def fake_killed_run(
    lines: list[str],
    *,
    cancel: threading.Event | None = None,
    returns: ProcessResult | None = None,
) -> Any:
    """A run_process that streams `lines`, optionally sets the caller `cancel`, waits for the
    adapter to kill it, then raises ProcessCancelled or returns `returns` (an ordinary exit)."""
    from relay.core.platform.proc import (
        _CANCELLATION,  # pyright: ignore[reportPrivateUsage]
        _OUTPUT_SINK,  # pyright: ignore[reportPrivateUsage]
    )

    def run(_cmd: Any, **_kwargs: Any) -> ProcessResult:
        entry = _OUTPUT_SINK.get()
        killed = _CANCELLATION.get()
        assert entry is not None and killed is not None
        for line in lines:
            entry[0](line + "\n")
        if cancel is not None:
            cancel.set()
        assert killed.wait(5.0)
        if returns is None:
            raise ProcessCancelled("attempt cancelled")
        return returns

    return run


def fake_agy(*_a: object, **_k: object) -> tuple[str, ...]:
    return ("agy",)


@pytest.mark.parametrize("ordinary_exit", [False, True])
def test_agy_result_survives_cancel_landing_during_exit_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ordinary_exit: bool
) -> None:
    monkeypatch.setattr(agy_completion, "RESULT_EXIT_GRACE_S", 30.0)
    monkeypatch.setattr(agy_completion, "resolve_command", fake_agy)
    cancel = threading.Event()
    returns = ProcessResult(1, "\n".join(_RESULT_LINES), "killed") if ordinary_exit else None
    monkeypatch.setattr(
        agy_completion,
        "run_process",
        fake_killed_run(_RESULT_LINES, cancel=cancel, returns=returns),
    )
    result = AgyCompletionAdapter(Agent(cmd="agy")).complete(
        CompletionRequest("hi", "default"),
        cwd=tmp_path,
        cancel=cancel,
        timeout_s=30.0,
        max_output_bytes=10000,
        on_chunk=lambda _chunk: None,
    )
    assert result.content == "OK"
    assert result.usage == CompletionUsage(10, 2, None, None)


def test_agy_grace_kill_reported_as_ordinary_nonzero_exit_recovers_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agy_completion, "RESULT_EXIT_GRACE_S", 0.05)
    monkeypatch.setattr(agy_completion, "resolve_command", fake_agy)
    killed_exit = ProcessResult(1, "\n".join(_RESULT_LINES), "killed")
    monkeypatch.setattr(
        agy_completion, "run_process", fake_killed_run(_RESULT_LINES, returns=killed_exit)
    )
    result = AgyCompletionAdapter(Agent(cmd="agy")).complete(
        CompletionRequest("hi", "default"),
        cwd=tmp_path,
        cancel=threading.Event(),
        timeout_s=30.0,
        max_output_bytes=10000,
        on_chunk=lambda _chunk: None,
    )
    assert result.content == "OK"
    assert result.usage == CompletionUsage(10, 2, None, None)


def test_agy_malformed_kill_reported_as_ordinary_exit_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agy_completion, "MALFORMED_FAIL_FAST_GRACE_S", 0.05)
    monkeypatch.setattr(agy_completion, "resolve_command", fake_agy)
    line = json.dumps(
        {
            "event": "step_update",
            "step_update": {"step_type": "error_message", "message": "Malformed function call"},
        }
    )
    monkeypatch.setattr(
        agy_completion, "run_process", fake_killed_run([line], returns=ProcessResult(1, line))
    )
    with pytest.raises(CompletionInvalid) as exc_info:
        AgyCompletionAdapter(Agent(cmd="agy")).complete(
            CompletionRequest("hi", "default", output_schema={"type": "object"}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=30.0,
            max_output_bytes=10000,
        )
    assert "malformed function call" in str(exc_info.value)
    assert exc_info.value.raw == line
