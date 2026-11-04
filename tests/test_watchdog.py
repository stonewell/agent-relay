"""No-output watchdog: size-scaled limits, agent overrides and every adapter's 504 mapping.

The CLIs are fake Python scripts run through the real ``run_process``; no model CLI is used.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from relay.adapters import agy_completion, claude_completion, codex_completion, cursor_completion
from relay.adapters.agy_completion import AgyCompletionAdapter
from relay.adapters.claude_completion import ClaudeCompletionAdapter
from relay.adapters.codex_completion import CodexCompletionAdapter
from relay.adapters.completion import (
    FIRST_OUTPUT_BASE_S,
    FIRST_OUTPUT_PER_100KB_S,
    IDLE_TIMEOUT_S,
    CompletionError,
    CompletionRequest,
    silence_limits,
)
from relay.adapters.cursor_completion import CursorCompletionAdapter
from relay.core.platform.proc import ProcessResult, ProcessStalled
from relay.core.registry import Agent, ConfigError, validate_merged

PY = sys.executable
SECRET_PROMPT = "watchdog prompt text that must never appear in an error"
RECORDED_CLAUDE = Path("tests/fixtures/recorded/claude_plain.json")

# --- limits ------------------------------------------------------------------------------------


def test_defaults() -> None:
    assert (FIRST_OUTPUT_BASE_S, FIRST_OUTPUT_PER_100KB_S, IDLE_TIMEOUT_S) == (120.0, 30.0, 300.0)
    assert silence_limits(0, 1800) == (120.0, 300.0)
    # Coarse-heartbeat backends (codex, cursor) have no idle default; an override still applies.
    assert silence_limits(0, 1800, idle_default=None) == (120.0, None)
    assert silence_limits(0, 1800, idle_s=5, idle_default=None) == (120.0, 5)


def test_first_output_limit_scales_with_prompt_size() -> None:
    assert silence_limits(100 * 1024, 1800)[0] == pytest.approx(150.0)
    assert silence_limits(512 * 1024, 1800)[0] == pytest.approx(120.0 + 30.0 * 5.12)
    # The idle limit does not scale: once output flows, silence is the hang signal.
    assert silence_limits(512 * 1024, 1800)[1] == 300.0


def test_limits_are_capped_below_the_request_deadline() -> None:
    assert silence_limits(0, 100) == (None, None)
    assert silence_limits(0, 200) == (120.0, None)
    assert silence_limits(0, 300) == (120.0, None)
    assert silence_limits(0, 300.5) == (120.0, 300.0)
    # Scaling can push the first-output limit past the deadline.
    assert silence_limits(1024 * 1024, 400) == (None, 300.0)


def test_agent_overrides_replace_the_base_and_the_idle_limit() -> None:
    assert silence_limits(0, 1800, first_output_s=10, idle_s=5) == (10, 5)
    assert silence_limits(200 * 1024, 1800, first_output_s=10)[0] == pytest.approx(70.0)


# --- agent configuration -----------------------------------------------------------------------


def test_agent_accepts_positive_finite_overrides() -> None:
    agent = Agent(cmd="x", first_output_timeout_s=0.5, idle_timeout_s=600)
    assert (agent.first_output_timeout_s, agent.idle_timeout_s) == (0.5, 600.0)
    assert Agent(cmd="x").first_output_timeout_s is None
    assert Agent(cmd="x").idle_timeout_s is None


@pytest.mark.parametrize("field", ["first_output_timeout_s", "idle_timeout_s"])
@pytest.mark.parametrize("value", [0, -1, math.inf, -math.inf, math.nan, True, "soon"])
def test_agent_rejects_non_positive_or_non_finite_overrides(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Agent.model_validate({"cmd": "x", field: value})
    with pytest.raises(ConfigError, match=field):
        validate_merged({"version": 1, "agents": {"claude": {"cmd": "claude", field: value}}})


def test_agent_override_loads_from_merged_config() -> None:
    config = validate_merged(
        {"version": 1, "agents": {"claude": {"cmd": "claude", "idle_timeout_s": 45}}}
    )
    assert config.agents["claude"].idle_timeout_s == 45.0


# --- adapters ----------------------------------------------------------------------------------

SILENT = "import time; time.sleep(30)"


def _fake(module: Any, monkeypatch: pytest.MonkeyPatch, code: str, *extra: str) -> None:
    def resolve(*_a: object, **_k: object) -> tuple[str, ...]:
        return (PY, "-c", code, *extra)

    monkeypatch.setattr(module, "resolve_command", resolve)


def _complete(adapter: Any, request: CompletionRequest, cwd: Path) -> Any:
    return adapter.complete(
        request, cwd=cwd, cancel=threading.Event(), timeout_s=30.0, max_output_bytes=1_000_000
    )


ADAPTERS: list[tuple[Any, Any, str]] = [
    (claude_completion, ClaudeCompletionAdapter, "Claude"),
    (codex_completion, CodexCompletionAdapter, "Codex"),
    (cursor_completion, CursorCompletionAdapter, "Cursor"),
    (agy_completion, AgyCompletionAdapter, "Antigravity"),
]


@pytest.mark.parametrize(("module", "adapter_type", "label"), ADAPTERS)
def test_every_adapter_maps_a_first_output_kill_to_504_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any, adapter_type: Any, label: str
) -> None:
    _fake(module, monkeypatch, SILENT)
    # The override is honoured: the 120 s default would not fire inside this test.
    adapter = adapter_type(Agent(cmd=PY, first_output_timeout_s=0.3))
    started = time.monotonic()
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest(SECRET_PROMPT), tmp_path)
    assert time.monotonic() - started < 15
    assert (caught.value.status, caught.value.code) == (504, "timeout")
    message = str(caught.value)
    assert message.startswith(f"{label} CLI produced no output for ")
    assert "(first-output limit)" in message
    assert f"prompt {len(SECRET_PROMPT.encode())} bytes" in message
    assert SECRET_PROMPT not in message


@pytest.mark.parametrize(
    ("module", "adapter_type", "idle"),
    [
        (claude_completion, ClaudeCompletionAdapter, IDLE_TIMEOUT_S),
        (codex_completion, CodexCompletionAdapter, None),
        (cursor_completion, CursorCompletionAdapter, None),
        (agy_completion, AgyCompletionAdapter, IDLE_TIMEOUT_S),
    ],
)
def test_default_idle_limit_per_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    adapter_type: Any,
    idle: float | None,
) -> None:
    """codex and cursor events are too coarse to bound silence: only the deadline governs."""
    seen: list[tuple[object, object]] = []

    def spy(*_a: object, **kwargs: object) -> ProcessResult:
        seen.append((kwargs.get("first_output_timeout_s"), kwargs.get("idle_timeout_s")))
        raise ProcessStalled("idle", 1.0, 1.0)

    _fake(module, monkeypatch, SILENT)
    monkeypatch.setattr(module, "run_process", spy)
    adapter = adapter_type(Agent(cmd=PY))
    with pytest.raises(CompletionError):
        adapter.complete(
            CompletionRequest("hi"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=1800.0,
            max_output_bytes=1_000_000,
        )
    assert len(seen) == 1
    first, actual_idle = seen[0]
    assert first == pytest.approx(FIRST_OUTPUT_BASE_S, abs=1.0)  # every backend keeps it
    assert actual_idle == idle


# The explicit override still applies to every backend, codex and cursor included.
@pytest.mark.parametrize(("module", "adapter_type", "label"), ADAPTERS)
def test_every_adapter_maps_an_idle_kill_to_504_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any, adapter_type: Any, label: str
) -> None:
    _fake(module, monkeypatch, "import time; print('{}', flush=True); time.sleep(30)")
    adapter = adapter_type(Agent(cmd=PY, idle_timeout_s=0.3))
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest(SECRET_PROMPT), tmp_path)
    assert (caught.value.status, caught.value.code) == (504, "timeout")
    assert str(caught.value).startswith(f"{label} CLI produced no output for ")
    assert "(idle limit)" in str(caught.value)


# --- heartbeat: claude and agy run every request in stream-json --------------------------------

# Prints argv[1:] as lines, `gap` seconds apart, then exits (or hangs when argv ends "hang").
STEADY = (
    "import sys, time\n"
    "gap = float(sys.argv[1])\n"
    "lines = sys.argv[2:]\n"
    "hang = lines[-1:] == ['hang']\n"
    "for line in lines[:-1] if hang else lines:\n"
    "    print(line, flush=True)\n"
    "    time.sleep(gap)\n"
    "if hang:\n"
    "    time.sleep(30)\n"
)


def _claude_delta(text: str) -> str:
    return json.dumps(
        {
            "type": "stream_event",
            "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
        }
    )


CLAUDE_INIT = json.dumps({"type": "system", "subtype": "init"})


def test_claude_non_streaming_requests_use_stream_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, ...]] = []

    def resolve(_cmd: str, args: tuple[str, ...], **_k: object) -> tuple[str, ...]:
        seen.append(args)
        return (PY, "-c", "print('')")

    monkeypatch.setattr(claude_completion, "resolve_command", resolve)
    adapter = ClaudeCompletionAdapter(Agent(cmd=PY))
    for request in (
        CompletionRequest("hi"),
        CompletionRequest("hi", output_schema={"type": "object"}),
    ):
        with pytest.raises(CompletionError):
            _complete(adapter, request, tmp_path)
    plain, schema = seen
    expected = ("-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages")
    assert plain[:5] == expected and schema[:5] == expected
    assert schema[schema.index("--json-schema") + 1] == '{"type":"object"}'


def test_claude_stream_path_returns_the_json_path_result_and_steady_events_are_not_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = RECORDED_CLAUDE.read_text(encoding="utf-8").strip()
    lines = [CLAUDE_INIT, *(_claude_delta("x") for _ in range(20)), recorded]
    # 22 lines 0.05 s apart (1.1 s) outlast the 0.8 s idle limit only if each event resets it.
    _fake(claude_completion, monkeypatch, STEADY, "0.05", *lines)
    adapter = ClaudeCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.8))
    result = _complete(adapter, CompletionRequest("hi"), tmp_path)
    expected = claude_completion.parse_completion(ProcessResult(0, recorded), structured=False)
    assert result == expected
    assert result.content == "OK" and result.usage is not None


def test_claude_stream_path_returns_the_json_path_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = {"content": None, "tool_calls": [{"name": "edit", "arguments": {"path": "a"}}]}
    final = json.dumps(
        {"type": "result", "subtype": "success", "result": "", "structured_output": envelope}
    )
    _fake(claude_completion, monkeypatch, STEADY, "0.05", CLAUDE_INIT, final)
    adapter = ClaudeCompletionAdapter(Agent(cmd=PY))
    result = _complete(
        adapter, CompletionRequest("hi", output_schema={"type": "object"}), tmp_path
    )
    expected = claude_completion.parse_completion(ProcessResult(0, final), structured=True)
    assert result == expected and result.envelope == envelope


def test_claude_silence_after_events_is_an_idle_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(claude_completion, monkeypatch, STEADY, "0.05", CLAUDE_INIT, _claude_delta("x"), "hang")
    adapter = ClaudeCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.5))
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest("hi", output_schema={"type": "object"}), tmp_path)
    assert (caught.value.status, caught.value.code) == (504, "timeout")
    assert "(idle limit)" in str(caught.value)


def _agy_step(kind: str = "thinking") -> str:
    return json.dumps({"event": "step_update", "step_update": {"step_type": kind}})


def test_agy_stream_path_returns_the_json_path_result_and_steady_events_are_not_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "status": "SUCCESS",
        "response": "hello",
        "conversation_id": "c",
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    lines = [*(_agy_step() for _ in range(20)), json.dumps({"event": "result", "result": payload})]
    # 21 events 0.05 s apart (1.05 s) outlast the 0.8 s idle limit only if each resets it.
    _fake(agy_completion, monkeypatch, STEADY, "0.05", *lines)
    adapter = AgyCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.8))
    result = _complete(adapter, CompletionRequest("hi"), tmp_path)
    expected = agy_completion.parse_completion(
        ProcessResult(0, json.dumps(payload)), structured=False
    )
    assert result == expected and result.content == "hello"


def test_agy_stream_path_returns_the_json_path_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope: dict[str, Any] = {"content": "done", "tool_calls": []}
    payload: dict[str, Any] = {"status": "SUCCESS", "structured_output": envelope}
    final = json.dumps({"event": "result", "result": payload})
    _fake(agy_completion, monkeypatch, STEADY, "0.05", _agy_step(), final)
    adapter = AgyCompletionAdapter(Agent(cmd=PY))
    result = _complete(
        adapter, CompletionRequest("hi", output_schema={"type": "object"}), tmp_path
    )
    expected = agy_completion.parse_completion(
        ProcessResult(0, json.dumps(payload)), structured=True
    )
    assert result == expected and result.envelope == envelope


def test_agy_silence_after_events_is_an_idle_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake(agy_completion, monkeypatch, STEADY, "0.05", _agy_step(), "hang")
    adapter = AgyCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.5))
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest("hi"), tmp_path)
    assert (caught.value.status, caught.value.code) == (504, "timeout")
    assert "(idle limit)" in str(caught.value)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        ("context_length_exceeded: prompt is too long", 400, "context_length_exceeded"),
        ("429 Too Many Requests", 429, "rate_limit_exceeded"),
    ],
)
def test_agy_error_result_then_silence_keeps_the_classified_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: str, status: int, code: str
) -> None:
    final = json.dumps({"event": "result", "result": {"status": "ERROR", "error": error}})
    _fake(agy_completion, monkeypatch, STEADY, "0.05", _agy_step(), final, "hang")
    adapter = AgyCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.3))
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest("hi"), tmp_path)
    assert (caught.value.status, caught.value.code) == (status, code)


# --- a denied tool followed by silence is a tool error, not a timeout ------------------------


def test_codex_denied_tool_then_silence_is_tool_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = (
        "import time; open('relay-tool-denied', 'w').close(); "
        "print('{}', flush=True); time.sleep(30)"
    )
    _fake(codex_completion, monkeypatch, code)
    adapter = CodexCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.3))
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest("hi"), tmp_path)
    assert caught.value.code == "backend_tool_activity"


def test_cursor_denied_tool_then_silence_is_tool_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = (
        "import time; open('.cursor-config/deny_tools.used', 'w').close(); "
        "print('{}', flush=True); time.sleep(30)"
    )
    _fake(cursor_completion, monkeypatch, code)
    adapter = CursorCompletionAdapter(Agent(cmd=PY, idle_timeout_s=0.3))
    with pytest.raises(CompletionError) as caught:
        _complete(adapter, CompletionRequest("hi"), tmp_path)
    assert caught.value.code == "backend_tool_use"
