from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import relay.adapters.agy_completion as agy_completion
from relay.adapters.agy_completion import (
    COMPLETION_FLAGS,
    DEFAULT_AGY_MODEL,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_OUTPUT_TOKENS,
    PROMPT_LIMIT_BYTES,
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
from relay.core.platform.proc import ProcessResult
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
    assert args[:4] == ("-p", "test prompt", "--output-format", "json")
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
    assert args_stream[:4] == ("-p", "test prompt", "--output-format", "stream-json")
    for flag in COMPLETION_FLAGS:
        assert flag in args_stream

    req_model = CompletionRequest("test prompt", "gemini-2.5-pro", effort="high")
    args_model = AgyCompletionAdapter.arguments(req_model)
    assert "--model" in args_model
    assert args_model[args_model.index("--model") + 1] == "gemini-2.5-pro"
    assert "--effort" in args_model
    assert args_model[args_model.index("--effort") + 1] == "high"

    # stream=True with output_schema uses json format and inlines schema instructions in prompt
    req_schema = CompletionRequest("test prompt", "default", output_schema={"type": "object"})
    args_schema = AgyCompletionAdapter.arguments(req_schema)
    assert args_schema[0] == "-p"
    assert "test prompt" in args_schema[1]
    assert '{"type":"object"}' in args_schema[1]
    assert args_schema[2:4] == ("--output-format", "json")
    assert "--json-schema" not in args_schema

    args_schema_stream = AgyCompletionAdapter.arguments(req_schema, stream=True)
    assert args_schema_stream[0] == "-p"
    assert "test prompt" in args_schema_stream[1]
    assert '{"type":"object"}' in args_schema_stream[1]
    assert args_schema_stream[2:4] == ("--output-format", "json")
    assert "--json-schema" not in args_schema_stream


def test_agy_settings_and_completion_flags() -> None:
    settings_raw: str = agy_completion._AGY_SETTINGS  # pyright: ignore[reportPrivateUsage]
    assert '"agentMode":""' not in settings_raw
    assert "agentMode" not in settings_raw
    assert '"permissions":{"allow":[],"ask":[],"deny":["*"]}' in settings_raw

    settings: dict[str, Any] = json.loads(settings_raw)
    assert "agentMode" not in settings
    assert settings["permissions"] == {"allow": [], "ask": [], "deny": ["*"]}
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

    # Running deny_tools.py --pre-invocation outputs valid JSON with systemMessage and ephemeralMessage
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
    assert "systemMessage" in pre_data
    assert "ephemeralMessage" in pre_data
    assert pre_data["systemMessage"] == pre_data["ephemeralMessage"]
    assert "Stateless single-turn completion mode" in pre_data["systemMessage"]

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
        "Relay clients execute tools. Output the requested JSON envelope containing "
        "the tool_calls directly without calling native tools."
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
    assert "define_subagent" in schema_inst
    assert "invoke_subagent" in schema_inst
    assert "send_message" in schema_inst
    assert "view_file" in schema_inst
    assert "run_command" in schema_inst
    assert "search_web" in schema_inst
    assert json.dumps(schema, separators=(",", ":"), sort_keys=True) in schema_inst

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


def test_agy_completion_prompt_limit(tmp_path: Path) -> None:
    agent = Agent(cmd="agy")
    adapter = AgyCompletionAdapter(agent)
    assert isinstance(adapter, CompletionCapable)

    # Prompt exceeds PROMPT_LIMIT_BYTES
    huge_prompt = "a" * (PROMPT_LIMIT_BYTES + 1)
    req_huge = CompletionRequest(huge_prompt)
    with pytest.raises(CompletionError) as exc_info:
        adapter.complete(
            req_huge,
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=10,
            max_output_bytes=1024,
        )
    assert exc_info.value.status == 400
    assert exc_info.value.code == "context_length_exceeded"

    # Prompt + schema_instructions exceeds PROMPT_LIMIT_BYTES
    near_limit_prompt = "a" * (PROMPT_LIMIT_BYTES - 10)
    req_near_limit = CompletionRequest(
        near_limit_prompt, output_schema={"type": "object", "properties": {"x": {"type": "string"}}}
    )
    with pytest.raises(CompletionError) as exc_info_schema:
        adapter.complete(
            req_near_limit,
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=10,
            max_output_bytes=1024,
        )
    assert exc_info_schema.value.status == 400
    assert exc_info_schema.value.code == "context_length_exceeded"


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
    expected_dict_msg = (
        "Antigravity CLI completion failed: exit code 0; error: Invalid auth token"
    )
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
    proc: ProcessResult, status: int, code: str
) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(proc, structured=False)
    assert caught.value.status == status
    assert caught.value.code == code
    if status in (429, 502):
        # Bad exits name the exit code and a redacted stderr tail.
        assert f"exit code {proc.exit_code}" in str(caught.value)
        assert "secret" in str(caught.value)
    else:
        assert "secret" not in str(caught.value)


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


def test_agy_observed_fallback_and_model_usage() -> None:
    # 1. Fallback when modelUsage is absent
    obs_default = _observed({})
    assert obs_default == ObservedModel(
        DEFAULT_AGY_MODEL, DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS
    )

    # 2. Fallback with custom request_model
    obs_custom = _observed({}, request_model="gemini-2.5-flash")
    assert obs_custom == ObservedModel(
        "gemini-2.5-flash", DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS
    )

    # 3. Fallback with payload model / limits
    obs_payload = _observed(
        {"model": "custom-agy-model", "context_window": 500_000, "max_output_tokens": 16_384}
    )
    assert obs_payload == ObservedModel("custom-agy-model", 500_000, 16_384)

    # 4. When modelUsage is present
    obs_usage = _observed(
        {"modelUsage": {"custom-entry": {"contextWindow": 100_000, "maxOutputTokens": 4_096}}}
    )
    assert obs_usage == ObservedModel("custom-entry", 100_000, 4_096)

    # 5. parse_completion forwards request_model
    proc = agy_response()
    result = parse_completion(proc, structured=False, request_model="gemini-2.5-flash")
    assert result.observed == ObservedModel(
        "gemini-2.5-flash", DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_OUTPUT_TOKENS
    )
