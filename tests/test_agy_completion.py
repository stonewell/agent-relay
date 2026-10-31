from __future__ import annotations

import json
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
from relay.adapters.antigravity import agy_envelope
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


def test_agy_prepare_policy(tmp_path: Path) -> None:
    marker = agy_completion._prepare_policy(tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert marker == tmp_path / ".agents" / "deny_tools.used"
    assert not marker.exists()

    agents_dir = tmp_path / ".agents"
    custom_agents = agents_dir / "agents"
    assert agents_dir.is_dir()
    assert custom_agents.is_dir()
    assert (agents_dir.stat().st_mode & 0o777) == 0o700
    assert (custom_agents.stat().st_mode & 0o777) == 0o700

    policy_files = [
        custom_agents / "relay-completion.md",
        tmp_path / "GEMINI.md",
        agents_dir / "hooks.json",
        agents_dir / "deny_tools.py",
        agents_dir / "mcp_config.json",
    ]
    for file_path in policy_files:
        assert file_path.is_file()
        assert (file_path.stat().st_mode & 0o777) == 0o600

    assert "relay-completion" in (custom_agents / "relay-completion.md").read_text(encoding="utf-8")
    assert "Explicit User Instructions Override" in (tmp_path / "GEMINI.md").read_text(
        encoding="utf-8"
    )
    assert "PreToolUse" in (agents_dir / "hooks.json").read_text(encoding="utf-8")
    assert "deny" in (agents_dir / "deny_tools.py").read_text(encoding="utf-8")
    assert (agents_dir / "mcp_config.json").read_text(encoding="utf-8") == '{"mcpServers":{}}'

    # Symlinks rejected with OSError
    symlink_dir = tmp_path / "symlink_dir"
    symlink_dir.mkdir(mode=0o700)
    fake_target = tmp_path / "fake_target"
    fake_target.mkdir(mode=0o700)
    (symlink_dir / ".agents").symlink_to(fake_target)
    with pytest.raises(OSError, match="symbolic link"):
        agy_completion._prepare_policy(symlink_dir)  # pyright: ignore[reportPrivateUsage]

    symlink_dir2 = tmp_path / "symlink_dir2"
    symlink_dir2.mkdir(mode=0o700)
    (symlink_dir2 / ".agents").mkdir(mode=0o700)
    (symlink_dir2 / ".agents" / "agents").symlink_to(fake_target)
    with pytest.raises(OSError, match="symbolic link"):
        agy_completion._prepare_policy(symlink_dir2)  # pyright: ignore[reportPrivateUsage]

    symlink_dir3 = tmp_path / "symlink_dir3"
    symlink_dir3.mkdir(mode=0o700)
    target_file = tmp_path / "target_file.txt"
    target_file.touch()
    (symlink_dir3 / "GEMINI.md").symlink_to(target_file)
    with pytest.raises(OSError, match="symbolic link"):
        agy_completion._prepare_policy(symlink_dir3)  # pyright: ignore[reportPrivateUsage]


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
        parse_completion(proc_err, structured=False)
    assert "secret" not in str(exc_info.value)
    # The CLI's own diagnostic is reported instead of an auth classification.
    assert exc_info.value.code == "completion_failed"
    assert "exit code 1" in str(exc_info.value)

    # Non-json output
    proc_invalid = ProcessResult(0, "not json at all")
    with pytest.raises(CompletionInvalid):
        parse_completion(proc_invalid, structured=False)


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
