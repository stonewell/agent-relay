"""Antigravity CLI (`agy`) adapter (task 4.1, design sections 7.2, 7.3, 5.4, 9.3, 9.4).

Deterministic tests only: invocation snapshots, probe detection, envelope parsing, the
four-step parsing order, usage normalization, and environment allowlisting. No network and
no live CLI. Every `agy` behavioural claim is **[verify]** (the CLI is not installed on the
reference machine), so the live smoke test is intentionally skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from relay.adapters.antigravity import (
    AntigravityAdapter,
    PermissionUnsupported,
    ResultInvalid,
)
from relay.adapters.base import RESULT_FILE, Invocation, ProcessResult, RunContext
from relay.core.env import CompleteEnv
from relay.core.registry import Agent
from relay.core.specs import ResultSpec, TaskSpec, schema_for
from relay.core.specs.common import PermissionLevel
from relay.core.specs.task import Routing


def agent(env: dict[str, str] | None = None) -> Agent:
    return Agent(cmd="agy", env=env or {})


def spec(routing: Routing | None = None) -> TaskSpec:
    return TaskSpec(
        id="n1",
        run_id="2026-10-22-test",
        category="implement",
        goal="Do the thing",
        routing=routing or Routing(),
    )


def ctx(
    tmp: Path,
    *,
    permission: PermissionLevel = "full",
    resume_session: str | None = None,
) -> RunContext:
    node_dir = tmp / "node"
    node_dir.mkdir(parents=True, exist_ok=True)
    worktree = tmp / "worktree"
    worktree.mkdir(exist_ok=True)
    return RunContext(
        run_id="2026-10-22-test",
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
    duration_s: float = 1.0,
    model: str | None = "gemini-4-argon-high",
) -> ResultSpec:
    adapter = AntigravityAdapter(agent(), parent_env={})
    run_ctx = ctx(tmp)
    if node_file is not None:
        (run_ctx.node_dir / RESULT_FILE).write_text(node_file, encoding="utf-8")
    proc = ProcessResult(exit_code=exit_code, stdout=stdout, stderr="", duration_s=duration_s)
    routing = Routing(model=model)
    return asyncio.run(adapter.parse_result(proc, spec(routing), run_ctx))


def schema_arg(inv: Invocation) -> dict[str, Any]:
    return json.loads(inv.args[inv.args.index("--json-schema") + 1])


# --- build_invocation: the flag set ---------------------------------------------------------


def test_build_invocation_flag_set_and_argv_prompt(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={"PATH": "/bin", "HOME": str(tmp_path)})
    inv = adapter.build_invocation(
        spec(routing=Routing(model="gemini-4-argon-high")), ctx(tmp_path)
    )
    assert inv.cmd == "agy"
    assert inv.cwd == tmp_path / "worktree"
    assert inv.stdin is None  # prompt is an argv value, never stdin (design 7.2)
    assert inv.full_env is True
    assert inv.args[0] == "-p"
    assert inv.args[1] == "Do the thing"
    assert inv.args[2:4] == ("--output-format", "json")
    assert inv.args[4] == "--json-schema"
    schema = schema_arg(inv)
    assert "$schema" not in schema
    assert schema.get("type") == "object"
    assert {"id", "status"} <= set(schema.get("required", []))
    assert inv.args[6:] == ("--model", "gemini-4-argon-high", "--dangerously-skip-permissions")


def test_build_invocation_omits_model_and_effort_when_unset(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert "--model" not in inv.args
    assert "--effort" not in inv.args
    assert "--dangerously-skip-permissions" in inv.args


def test_default_model_id_passes_no_model_flag(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(routing=Routing(model="default")), ctx(tmp_path))
    assert "--model" not in inv.args


def test_build_invocation_model_and_effort(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(
        spec(routing=Routing(model="gemini-3.8-flash-medium", effort="low")), ctx(tmp_path)
    )
    assert "--model" in inv.args and "gemini-3.8-flash-medium" in inv.args
    assert "--effort" in inv.args and "low" in inv.args


def test_the_inline_schema_is_exactly_the_generated_result_schema(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    schema = schema_arg(adapter.build_invocation(spec(), ctx(tmp_path)))
    expected = {key: value for key, value in schema_for(ResultSpec).items() if key != "$schema"}
    assert schema == expected


def test_print_timeout_is_not_passed(tmp_path: Path) -> None:
    """`RunContext` exposes no node timeout and the docs disagree on the default, so the
    adapter relies on the supervisor's `budget.timeout_s` instead of `--print-timeout`."""
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert "--print-timeout" not in inv.args


# --- permission mapping (section 7.2 / 9.3) ------------------------------------------------


def test_full_maps_to_dangerously_skip_permissions(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, permission="full"))
    assert "--dangerously-skip-permissions" in inv.args
    assert "--mode" not in inv.args


def test_readonly_maps_to_mode_plan(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, permission="readonly"))
    idx = inv.args.index("--mode")
    assert inv.args[idx + 1] == "plan"
    assert "--dangerously-skip-permissions" not in inv.args


def test_workspace_fails_closed(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    with pytest.raises(PermissionUnsupported, match="workspace"):
        adapter.build_invocation(spec(), ctx(tmp_path, permission="workspace"))


def test_probe_advertises_only_full() -> None:
    adapter = AntigravityAdapter(agent(), cmd=sys.executable, parent_env=dict(os.environ))
    probe = asyncio.run(adapter.probe())
    assert probe.available is True
    assert probe.levels == ("full",)
    assert probe.version is not None
    assert probe.version.startswith("Python")


def test_probe_unavailable_when_command_missing() -> None:
    adapter = AntigravityAdapter(
        agent(), cmd="definitely-not-installed-xyz", parent_env=dict(os.environ)
    )
    probe = asyncio.run(adapter.probe())
    assert probe.available is False
    assert probe.levels == ("full",)


def test_probe_records_the_auth_mode() -> None:
    plain = AntigravityAdapter(
        agent(), cmd=sys.executable, parent_env={"PATH": "/bin", "HOME": "/tmp"}
    )
    assert "auth-mode=keyring" in asyncio.run(plain.probe()).notes
    keyed = AntigravityAdapter(
        agent(),
        cmd=sys.executable,
        parent_env={"PATH": "/bin", "HOME": "/tmp", "GEMINI_API_KEY": "AIza-probe"},
    )
    assert "auth-mode=api-key" in asyncio.run(keyed.probe()).notes


# --- resume -------------------------------------------------------------------------------


def test_resume_flag_when_resume_session_is_set(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, resume_session="conv-123"))
    idx = inv.args.index("--conversation")
    assert inv.args[idx + 1] == "conv-123"


def test_no_resume_flag_by_default(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={})
    assert "--conversation" not in adapter.build_invocation(spec(), ctx(tmp_path)).args


def test_conversation_id_extraction_and_resume_round_trip(tmp_path: Path) -> None:
    env = {
        "status": "SUCCESS",
        "conversation_id": "conv-abc",
        "structured_output": {"status": "done", "summary": "ok"},
    }
    result = parse(tmp_path, json.dumps(env))
    assert result.session is not None
    assert result.session.cli_session_id == "conv-abc"
    adapter = AntigravityAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, resume_session="conv-abc"))
    idx = inv.args.index("--conversation")
    assert inv.args[idx + 1] == "conv-abc"


# --- parsing order (section 7.3) ----------------------------------------------------------


def test_parse_native_structured_output(tmp_path: Path) -> None:
    env = {
        "status": "SUCCESS",
        "conversation_id": "conv-1",
        "response": "the text",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "structured_output": {"status": "done", "summary": "the summary"},
    }
    result = parse(tmp_path, json.dumps(env), duration_s=3.9)
    assert result.status == "done"
    assert result.summary == "the summary"
    assert result.session is not None
    assert result.session.cli_session_id == "conv-1"
    assert result.model == "gemini-4-argon-high"
    assert result.agent == "agy"
    assert result.usage is not None
    assert result.usage.duration_s == 3.9


def test_native_beats_the_node_file(tmp_path: Path) -> None:
    env = {
        "status": "SUCCESS",
        "structured_output": {"status": "done", "summary": "from-native"},
    }
    result = parse(
        tmp_path, json.dumps(env), node_file="id: n1\nstatus: done\nsummary: from-file\n"
    )
    assert result.summary == "from-native"


def test_node_file_fallback_when_no_structured_output(tmp_path: Path) -> None:
    stdout = json.dumps({"status": "SUCCESS", "response": "hi"})
    result = parse(tmp_path, stdout, node_file="id: n1\nstatus: done\nsummary: from-file\n")
    assert result.summary == "from-file"


def test_stdout_block_fallback_in_response(tmp_path: Path) -> None:
    response = '```json\n{"id": "n1", "status": "done", "summary": "from-response-block"}\n```'
    stdout = json.dumps({"status": "SUCCESS", "response": response})
    result = parse(tmp_path, stdout)
    assert result.status == "done"
    assert result.summary == "from-response-block"


def test_stdout_block_fallback_in_stdout(tmp_path: Path) -> None:
    stdout = "prose\n```yaml\nid: n1\nstatus: done\nsummary: from-stdout-block\n```\n"
    result = parse(tmp_path, stdout)
    assert result.status == "done"
    assert result.summary == "from-stdout-block"


def test_synthesized_from_exit_code_and_response(tmp_path: Path) -> None:
    stdout = json.dumps({"status": "SUCCESS", "response": "OK done"})
    assert parse(tmp_path, stdout).status == "done"
    assert parse(tmp_path, stdout).summary == "OK done"
    assert parse(tmp_path, stdout, exit_code=1).status == "failed"


def test_synthesized_without_envelope(tmp_path: Path) -> None:
    result = parse(tmp_path, "not json at all")
    assert result.status == "done"
    assert result.summary == "not json at all"


def test_malformed_envelope_synthesizes(tmp_path: Path) -> None:
    result = parse(tmp_path, '{"status": "SUCCESS", "usage": {"input_tokens": "x"')
    assert result.status == "done"


def test_error_status_raises_result_invalid_with_error_text(tmp_path: Path) -> None:
    stdout = json.dumps({"status": "ERROR", "error": "something broke"})
    with pytest.raises(ResultInvalid, match="something broke"):
        parse(tmp_path, stdout)


def test_invalid_status_raises_result_invalid(tmp_path: Path) -> None:
    stdout = json.dumps({"status": "INVALID"})
    with pytest.raises(ResultInvalid, match="INVALID"):
        parse(tmp_path, stdout)


def test_error_status_with_node_file_still_parses(tmp_path: Path) -> None:
    """A usable node-dir result rescues a terminal ERROR envelope (section 7.3)."""
    stdout = json.dumps({"status": "ERROR", "error": "boom"})
    result = parse(tmp_path, stdout, node_file="id: n1\nstatus: done\nsummary: rescued\n")
    assert result.summary == "rescued"


# --- usage normalization (section 8.5) ----------------------------------------------------


def test_usage_buckets_are_mapped(tmp_path: Path) -> None:
    env = {
        "status": "SUCCESS",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 3,
            "thinking_tokens": 2,
        },
        "structured_output": {"status": "done", "summary": "ok"},
    }
    result = parse(tmp_path, json.dumps(env))
    assert result.usage is not None
    assert result.usage.tokens_in == 10
    assert result.usage.tokens_out == 5
    dump = result.usage.model_dump()
    assert dump["cache_read_tokens"] == 3
    assert dump["thinking_tokens"] == 2


def test_routed_default_model_is_none(tmp_path: Path) -> None:
    env = {
        "status": "SUCCESS",
        "structured_output": {"status": "done", "summary": "ok"},
    }
    assert parse(tmp_path, json.dumps(env), model="default").model is None
    assert parse(tmp_path, json.dumps(env), model=None).model is None
    real = parse(tmp_path, json.dumps(env), model="gemini-4-argon-high")
    assert real.model == "gemini-4-argon-high"


# --- trust boundary (sections 5.4, 8.5) ---------------------------------------------------


def test_agent_declared_operational_fields_are_overwritten(tmp_path: Path) -> None:
    env: dict[str, Any] = {
        "status": "SUCCESS",
        "conversation_id": "conv-real",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "structured_output": {
            "id": "other-node",
            "agent": "cursor",
            "status": "done",
            "summary": "ok",
            "model": "fake-model",
            "session": {"cli_session_id": "fake-session"},
            "usage": {"cost_usd": 9999.0, "tokens_in": 123456, "tokens_out": 654321},
        },
    }
    result = parse(tmp_path, json.dumps(env), model="gemini-4-argon-high")
    assert result.id == "n1"
    assert result.agent == "agy"
    assert result.model == "gemini-4-argon-high"
    assert result.session is not None
    assert result.session.cli_session_id == "conv-real"
    assert result.usage is not None
    assert result.usage.tokens_in == 1
    assert result.usage.tokens_out == 2
    assert result.usage.cost_usd is None


def test_malformed_operational_fields_do_not_fail_validation(tmp_path: Path) -> None:
    env: dict[str, Any] = {
        "status": "SUCCESS",
        "conversation_id": "conv-real",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "structured_output": {
            "status": "done",
            "summary": "ok",
            "id": 999,
            "agent": {"nested": "evil"},
            "model": 123,
            "session": "not-a-session",
            "usage": "evil",
        },
    }
    result = parse(tmp_path, json.dumps(env), model="gemini-4-argon-high")  # must not raise
    assert result.id == "n1"
    assert result.agent == "agy"
    assert result.model == "gemini-4-argon-high"
    assert result.session is not None
    assert result.session.cli_session_id == "conv-real"
    assert result.usage is not None
    assert result.usage.tokens_in == 1


def test_invalid_native_result_raises_result_invalid(tmp_path: Path) -> None:
    env = {"status": "SUCCESS", "structured_output": {"id": "n1"}}  # missing `status`
    with pytest.raises(ResultInvalid):
        parse(tmp_path, json.dumps(env))


# --- environment allowlisting (design 9.4) -------------------------------------------------


def test_invocation_env_contains_no_parent_only_secret(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    parent = {
        "PATH": "/bin",
        "HOME": home,
        "GITHUB_TOKEN": "ghp-secret-noise",
        "AWS_SECRET_ACCESS_KEY": "aws-noise",
    }
    adapter = AntigravityAdapter(agent(), parent_env=parent)
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert inv.full_env is True
    assert "GITHUB_TOKEN" not in inv.env
    assert "AWS_SECRET_ACCESS_KEY" not in inv.env
    assert inv.env["PATH"] == "/bin"
    assert inv.env.get("HOME") == (home if os.name != "nt" else None)


def test_env_forwards_gemini_api_key_only_when_set(tmp_path: Path) -> None:
    plain = AntigravityAdapter(agent(), parent_env={"PATH": "/bin"})
    inv = plain.build_invocation(spec(), ctx(tmp_path))
    assert "GEMINI_API_KEY" not in inv.env

    keyed = AntigravityAdapter(
        agent(), parent_env={"PATH": "/bin", "GEMINI_API_KEY": "AIza-secret"}
    )
    inv = keyed.build_invocation(spec(), ctx(tmp_path))
    assert inv.env["GEMINI_API_KEY"] == "AIza-secret"
    # another provider's key must not leak (section 9.4)
    assert "ANTHROPIC_API_KEY" not in inv.env


def test_env_is_a_complete_env(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(), parent_env={"PATH": "/bin"})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert isinstance(inv.env, CompleteEnv)
    assert inv.full_env is True


def test_env_layers_agent_env_additions(tmp_path: Path) -> None:
    adapter = AntigravityAdapter(agent(env={"MY_AGY_FLAG": "1"}), parent_env={"PATH": "/bin"})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert inv.env["MY_AGY_FLAG"] == "1"


# --- live smoke (intentionally skipped) ----------------------------------------------------


@pytest.mark.skip(
    reason=(
        "agy auth/quota behaviour is unverified [verify] (design section 17); no live smoke "
        "until the doctor canary (task 1.12) confirms headless auth and quota behaviour"
    )
)
def test_live_smoke_intentionally_skipped(tmp_path: Path) -> None:
    raise AssertionError("unreachable")
