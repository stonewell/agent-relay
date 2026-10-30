"""Cursor CLI adapter (tasks 4.2 and 4.11; design sections 7.2, 7.3, 5.4, 9.3, 9.4).

Offline tests only: invocation snapshots, the stdin prompt channel through a Windows `.cmd`
shim built like Cursor's (cmd.exe with delayed expansion, then PowerShell 5.1), envelope
parsing on the recorded spike 0.11 outputs, the parsing order, usage normalization,
environment allowlisting, registry eligibility, and the Cursor doctor canaries against fake
CLIs. No network, credentials or model call is used.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from relay.adapters.base import (
    RESULT_FILE,
    Invocation,
    ProcessResult,
    ProgressCapable,
    RunContext,
    run_invocation,
)
from relay.adapters.cursor import (
    LEGACY_COMMAND,
    CursorAdapter,
    PermissionUnsupported,
    ResultInvalid,
)
from relay.core import registry as registry_module
from relay.core.env import CompleteEnv
from relay.core.registry import Agent, load_config
from relay.core.specs import ResultSpec, TaskSpec
from relay.core.specs.common import PermissionLevel
from relay.core.specs.task import Routing

DIR = Path(__file__).parent / "fixtures" / "recorded"
WINDOWS = os.name == "nt"


def agent(env: dict[str, str] | None = None, cmd: str = "agent") -> Agent:
    return Agent(cmd=cmd, env=env or {})


def spec(routing: Routing | None = None, goal: str = "Do the thing") -> TaskSpec:
    return TaskSpec(
        id="n1",
        run_id="2026-10-27-test",
        category="implement",
        goal=goal,
        routing=routing or Routing(),
    )


def ctx(
    tmp: Path, *, permission: PermissionLevel = "full", resume_session: str | None = None
) -> RunContext:
    node_dir = tmp / "node"
    node_dir.mkdir(parents=True, exist_ok=True)
    worktree = tmp / "worktree"
    worktree.mkdir(exist_ok=True)
    return RunContext(
        run_id="2026-10-27-test",
        node_id="n1",
        attempt=1,
        node_dir=node_dir,
        worktree=worktree,
        permission=permission,
        resume_session=resume_session,
    )


def parse(
    tmp: Path,
    stdout: str,
    *,
    exit_code: int = 0,
    node_file: str | None = None,
    model: str | None = None,
) -> ResultSpec:
    adapter = CursorAdapter(agent(), parent_env={})
    run_ctx = ctx(tmp)
    if node_file is not None:
        (run_ctx.node_dir / RESULT_FILE).write_text(node_file, encoding="utf-8")
    proc = ProcessResult(exit_code=exit_code, stdout=stdout, stderr="", duration_s=2.5)
    return asyncio.run(adapter.parse_result(proc, spec(Routing(model=model)), run_ctx))


def envelope(result: str, **extra: Any) -> str:
    body: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "session_id": "00000000-0000-0000-0000-000000000099",
        "usage": {"inputTokens": 10, "outputTokens": 2, "cacheReadTokens": 5},
    }
    body.update(extra)
    return json.dumps(body)


def recorded(name: str) -> str:
    return (DIR / name).read_text(encoding="utf-8")


# --- build_invocation: the flag set ---------------------------------------------------------


def test_build_invocation_flag_set_and_stdin_prompt(tmp_path: Path) -> None:
    adapter = CursorAdapter(agent(), parent_env={"PATH": "/bin"})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert inv.cmd == "agent"
    assert inv.cwd == tmp_path / "worktree"
    assert inv.full_env is True
    assert inv.args == ("-p", "--output-format", "json", "--trust", "--force")
    # The prompt never crosses argv (the Windows shim mangles quotes and `!`).
    assert inv.stdin is not None
    assert inv.stdin.startswith("Do the thing")
    assert inv.stdin.endswith("\n")
    assert all("Do the thing" not in arg for arg in inv.args)


def test_prompt_injects_the_compact_result_schema(tmp_path: Path) -> None:
    inv = CursorAdapter(agent(), parent_env={}).build_invocation(spec(), ctx(tmp_path))
    assert inv.stdin is not None
    marker = "JSON Schema:\n"
    schema = json.loads(inv.stdin[inv.stdin.index(marker) + len(marker) :])
    assert "$schema" not in schema
    assert {"id", "status"} <= set(schema["required"])
    assert "needs_input" in inv.stdin


def test_model_flag_and_default_sentinel(tmp_path: Path) -> None:
    adapter = CursorAdapter(agent(), parent_env={})
    model = "claude-opus-4-8[context=1m,effort=high]"
    inv = adapter.build_invocation(spec(Routing(model=model, effort="high")), ctx(tmp_path))
    assert inv.args[inv.args.index("--model") + 1] == model
    assert "--effort" not in inv.args  # no verified effort mapping
    default = adapter.build_invocation(spec(Routing(model="default")), ctx(tmp_path))
    assert "--model" not in default.args
    assert "--model" not in adapter.build_invocation(spec(), ctx(tmp_path)).args


@pytest.mark.parametrize("value", ['gpt "5"', "a!b", "two words", "x%PATH%", "x&y"])
def test_argv_tokens_that_would_not_survive_the_shim_are_rejected(
    tmp_path: Path, value: str
) -> None:
    adapter = CursorAdapter(agent(), parent_env={})
    with pytest.raises(ValueError, match="shim"):
        adapter.build_invocation(spec(Routing(model=value)), ctx(tmp_path))
    with pytest.raises(ValueError, match="shim"):
        adapter.build_invocation(spec(), ctx(tmp_path, resume_session=value))


# --- permission mapping (section 7.2 / 9.3) ------------------------------------------------


def test_full_maps_to_force_and_trust(tmp_path: Path) -> None:
    inv = CursorAdapter(agent(), parent_env={}).build_invocation(spec(), ctx(tmp_path))
    assert "--force" in inv.args and "--trust" in inv.args
    assert "--mode" not in inv.args


def test_readonly_maps_to_plan_mode_without_force_or_trust(tmp_path: Path) -> None:
    # Only reachable once `readonly` is advertised; `--trust` would mark an untrusted repo
    # as trusted, so a read-only run never passes it.
    adapter = CursorAdapter(agent(), parent_env={}, levels=("full", "readonly"))
    inv = adapter.build_invocation(spec(), ctx(tmp_path, permission="readonly"))
    assert inv.args[inv.args.index("--mode") + 1] == "plan"
    assert "--force" not in inv.args and "--yolo" not in inv.args
    assert "--trust" not in inv.args
    assert inv.stdin  # stdin carries the prompt and is closed after it, never a TTY


def test_workspace_fails_closed(tmp_path: Path) -> None:
    adapter = CursorAdapter(agent(), parent_env={})
    with pytest.raises(PermissionUnsupported, match="workspace"):
        adapter.build_invocation(spec(), ctx(tmp_path, permission="workspace"))


# --- command resolution: `agent` with the legacy `cursor-agent` alias ------------------------


def _fake_executable(directory: Path, name: str) -> None:
    if WINDOWS:
        (directory / f"{name}.cmd").write_text("@echo off\r\necho 2026.10.22-test\r\n")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\necho 2026.10.22-test\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _path_env(directory: Path) -> dict[str, str]:
    env = {"PATH": str(directory)}
    if WINDOWS:
        env.update(PATHEXT=".COM;.EXE;.BAT;.CMD", SystemRoot=os.environ.get("SYSTEMROOT", ""))
    return env


def test_legacy_alias_is_used_when_agent_is_absent(tmp_path: Path) -> None:
    _fake_executable(tmp_path, LEGACY_COMMAND)
    adapter = CursorAdapter(agent(), parent_env=_path_env(tmp_path))
    assert adapter.command() == LEGACY_COMMAND
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert inv.cmd == LEGACY_COMMAND


def test_agent_wins_over_the_alias_and_explicit_commands_are_kept(tmp_path: Path) -> None:
    _fake_executable(tmp_path, "agent")
    _fake_executable(tmp_path, LEGACY_COMMAND)
    assert CursorAdapter(agent(), parent_env=_path_env(tmp_path)).command() == "agent"
    explicit = CursorAdapter(agent(cmd="my-cursor"), parent_env=_path_env(tmp_path))
    assert explicit.command() == "my-cursor"


# --- resume ---------------------------------------------------------------------------------


def test_resume_flag_and_session_round_trip(tmp_path: Path) -> None:
    session = "00000000-0000-0000-0000-000000000013"
    result = parse(tmp_path, recorded("cursor_plain.json"))
    assert result.session is not None and result.session.cli_session_id == session
    adapter = CursorAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, resume_session=session))
    assert inv.args[inv.args.index("--resume") + 1] == session
    assert "--resume" not in adapter.build_invocation(spec(), ctx(tmp_path)).args


# --- golden tests on the recorded spike 0.11 outputs ----------------------------------------


def test_golden_plain_run_synthesizes_from_the_result_text(tmp_path: Path) -> None:
    result = parse(tmp_path, recorded("cursor_plain.json"))
    assert (result.status, result.summary, result.agent, result.id) == (
        "done",
        "OK",
        "cursor",
        "n1",
    )
    assert result.model is None  # the envelope names no model and none was routed
    assert result.usage is not None
    dump = result.usage.model_dump()
    assert (dump["tokens_in"], dump["tokens_out"]) == (20201, 1)
    assert (dump["cache_read_tokens"], dump["cache_write_tokens"]) == (0, 0)
    assert dump["cost_usd"] is None  # tokens only; cost comes from pricing
    assert dump["duration_s"] == 2.5


def test_golden_resume_keeps_the_session_id(tmp_path: Path) -> None:
    first = parse(tmp_path, recorded("cursor_plain.json"))
    resumed = parse(tmp_path, recorded("cursor_resume.json"))
    assert resumed.session is not None and first.session is not None
    assert resumed.session.cli_session_id == first.session.cli_session_id
    assert resumed.usage is not None
    assert resumed.usage.model_dump()["cache_read_tokens"] == 15716


def test_golden_needs_input_text_is_normalized_and_validated(tmp_path: Path) -> None:
    result = parse(tmp_path, recorded("cursor_prompt_needs_input.json"), model="auto")
    assert result.status == "needs_input"
    assert result.question_revision == 1
    assert [q.kind for q in result.open_questions] == ["free", "choice", "free"]
    free = [q for q in result.open_questions if q.kind == "free"]
    assert all(q.options is None and q.default is None for q in free)
    choice = result.open_questions[1]
    assert choice.options == ["In-memory cache", "Redis", "Memcached"]
    assert choice.default == "In-memory cache"
    assert result.model == "auto"
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000015"


# --- parsing order (section 7.3) ------------------------------------------------------------


def test_node_file_beats_the_result_text(tmp_path: Path) -> None:
    stdout = envelope(json.dumps({"status": "done", "summary": "from-text"}))
    result = parse(tmp_path, stdout, node_file="id: n1\nstatus: done\nsummary: from-file\n")
    assert result.summary == "from-file"


def test_result_text_json_object(tmp_path: Path) -> None:
    stdout = envelope(json.dumps({"status": "done", "summary": "from-text"}))
    assert parse(tmp_path, stdout).summary == "from-text"


def test_result_text_fenced_block(tmp_path: Path) -> None:
    text = 'Here it is:\n```json\n{"status": "done", "summary": "fenced"}\n```\n'
    assert parse(tmp_path, envelope(text)).summary == "fenced"


def test_error_envelope_synthesizes_failed(tmp_path: Path) -> None:
    stdout = envelope("Authentication required", is_error=True, subtype="error")
    result = parse(tmp_path, stdout)
    assert result.status == "failed"
    assert result.summary == "Authentication required"
    assert parse(tmp_path, envelope("OK"), exit_code=1).status == "failed"


def test_no_envelope_synthesizes_from_stdout(tmp_path: Path) -> None:
    result = parse(tmp_path, "Error: Authentication required.\n", exit_code=1)
    assert (result.status, result.summary) == ("failed", "Error: Authentication required.")
    assert result.session is None and result.usage is None


def test_invalid_result_object_raises_result_invalid(tmp_path: Path) -> None:
    with pytest.raises(ResultInvalid):
        parse(tmp_path, envelope(json.dumps({"status": "not-a-status"})))


def test_agent_declared_operational_fields_are_overwritten(tmp_path: Path) -> None:
    text = json.dumps(
        {
            "id": "other",
            "agent": "claude",
            "status": "done",
            "summary": "ok",
            "model": "fake",
            "session": "evil",
            "usage": {"cost_usd": 999.0},
        }
    )
    result = parse(tmp_path, envelope(text), model="gpt-5")
    assert (result.id, result.agent, result.model) == ("n1", "cursor", "gpt-5")
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000099"
    assert result.usage is not None and result.usage.cost_usd is None


# --- environment allowlisting (design 9.4) --------------------------------------------------


def test_env_forwards_only_the_cursor_key_and_drops_other_secrets(tmp_path: Path) -> None:
    parent = {
        "PATH": "/bin",
        "USERPROFILE": "C:\\Users\\me",
        "APPDATA": "C:\\Users\\me\\AppData\\Roaming",
        "GITHUB_TOKEN": "ghp-noise",
        "ANTHROPIC_API_KEY": "sk-ant-noise",
        "RELAY_IPC_TOKEN": "relay-noise",
    }
    plain = CursorAdapter(agent(), parent_env=parent).build_invocation(spec(), ctx(tmp_path))
    assert isinstance(plain.env, CompleteEnv)
    assert "CURSOR_API_KEY" not in plain.env
    for leaked in ("GITHUB_TOKEN", "ANTHROPIC_API_KEY", "RELAY_IPC_TOKEN"):
        assert leaked not in plain.env
    if WINDOWS:  # the account login lives in the user profile (design 7.2)
        assert plain.env["USERPROFILE"] == parent["USERPROFILE"]
        assert plain.env["APPDATA"] == parent["APPDATA"]
    keyed = CursorAdapter(agent(), parent_env={**parent, "CURSOR_API_KEY": "key_secret"})
    assert keyed.build_invocation(spec(), ctx(tmp_path)).env["CURSOR_API_KEY"] == "key_secret"


def test_env_layers_agent_env_additions(tmp_path: Path) -> None:
    adapter = CursorAdapter(agent(env={"CURSOR_API_ENDPOINT": "https://x"}), parent_env={})
    assert adapter.build_invocation(spec(), ctx(tmp_path)).env["CURSOR_API_ENDPOINT"] == "https://x"


# --- registry eligibility (design 7.4, 16) --------------------------------------------------


def test_builtin_cursor_agent_is_opt_in_with_the_agent_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp_path / "none.yaml")
    cursor = load_config(tmp_path, user_config=None).config.agents["cursor"]
    assert cursor.enabled is False
    assert cursor.cmd == "agent"


def test_cursor_is_a_registered_worker_but_not_coordinator_or_progress_source(
    tmp_path: Path,
) -> None:
    from relay.core.run import _default_adapters  # pyright: ignore[reportPrivateUsage]

    config = load_config(tmp_path, user_config=None).config
    enabled = config.model_copy(update={"agents": {**config.agents, "cursor": agent(cmd="agent")}})
    assert isinstance(_default_adapters(enabled)["cursor"], CursorAdapter)
    adapter = CursorAdapter(agent(), parent_env={})
    assert not hasattr(adapter, "coordinate")
    assert not hasattr(adapter, "coordinator_profile")
    assert not isinstance(adapter, ProgressCapable)


# --- Windows `.cmd` shim: the real Cursor chain, emulated ------------------------------------

_FAKE_CURSOR = """\
import json, sys
from pathlib import Path
prompt = sys.stdin.read()
Path(sys.argv[0]).with_name("seen.json").write_text(
    json.dumps({"argv": sys.argv[1:], "stdin": prompt}), encoding="utf-8"
)
reply = {"status": "done", "summary": prompt.splitlines()[0]}
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": json.dumps(reply), "session_id": "00000000-0000-0000-0000-000000000042",
    "usage": {"inputTokens": 1, "outputTokens": 1, "cacheReadTokens": 0, "cacheWriteTokens": 0},
}))
"""


def _cursor_like_shim(directory: Path) -> Path:
    """`agent.cmd` -> PowerShell 5.1 `-File` -> native child, as Cursor installs it."""
    (directory / "fake_cursor.py").write_text(_FAKE_CURSOR, encoding="utf-8")
    (directory / "agent.ps1").write_text(
        "$scriptPath = Split-Path -parent $MyInvocation.MyCommand.Definition\n"
        f'& "{sys.executable}" "$scriptPath\\fake_cursor.py" $args\n'
        "exit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    shim = directory / "agent.cmd"
    shim.write_text(
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        'set "SCRIPT_DIR=%~dp0"\r\n'
        'if "%SCRIPT_DIR:~-1%"=="\\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"\r\n'
        "%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe -NoProfile "
        '-ExecutionPolicy Bypass -File "%SCRIPT_DIR%\\agent.ps1" %*\r\n',
        encoding="utf-8",
        newline="",
    )
    return shim


@pytest.mark.skipif(not WINDOWS, reason="Windows `.cmd` shim chain")
@pytest.mark.skipif(
    WINDOWS and shutil.which("powershell") is None, reason="Windows PowerShell missing"
)
def test_stdin_prompt_survives_the_cursor_shim_chain_exactly(tmp_path: Path) -> None:
    shim = _cursor_like_shim(tmp_path)
    goal = 'Fix "quoted" 100% !bang! $HOME `tick` <a>|&^ case\nsecond line'
    model = "claude-opus-4-8[context=1m,effort=high]"
    session = "00000000-0000-0000-0000-000000000013"
    adapter = CursorAdapter(agent(cmd=str(shim)), parent_env=dict(os.environ))
    run_ctx = ctx(tmp_path, resume_session=session)
    task = spec(Routing(model=model), goal=goal)
    inv: Invocation = adapter.build_invocation(task, run_ctx)
    proc = run_invocation(inv, timeout_s=60)
    assert proc.exit_code == 0, proc.stderr
    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert seen["stdin"] == inv.stdin  # byte-exact, including quotes, `!`, `%` and newlines
    assert seen["argv"] == list(inv.args)
    result = asyncio.run(adapter.parse_result(proc, task, run_ctx))
    assert result.summary == goal.splitlines()[0]
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000042"
