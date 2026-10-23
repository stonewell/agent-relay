"""Claude adapter: invocation, permission mapping, parsing order and environment (task 1.9).

Offline checks use the recorded Claude envelopes. The explicitly enabled live smoke also
checks a real structured reply and session continuation in a disposable directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from relay.adapters.base import RESULT_FILE, Invocation, ProcessResult, RunContext
from relay.adapters.claude import ClaudeAdapter, PermissionUnsupported, ResultInvalid
from relay.core.registry import Agent
from relay.core.specs import ResultSpec, TaskSpec, schema_for
from relay.core.specs.common import PermissionLevel
from relay.core.specs.task import Routing

DIR = Path(__file__).parent / "fixtures" / "recorded"


def claude_fixture(name: str) -> dict[str, Any]:
    return json.loads((DIR / name).read_text(encoding="utf-8"))


def agent(env: dict[str, str] | None = None) -> Agent:
    return Agent(cmd="claude", env=env or {})


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
) -> ResultSpec:
    adapter = ClaudeAdapter(agent(), parent_env={})
    run_ctx = ctx(tmp)
    if node_file is not None:
        (run_ctx.node_dir / RESULT_FILE).write_text(node_file, encoding="utf-8")
    proc = ProcessResult(exit_code=exit_code, stdout=stdout, stderr="", duration_s=duration_s)
    return asyncio.run(adapter.parse_result(proc, spec(), run_ctx))


def schema_arg(inv: Invocation) -> dict[str, Any]:
    return json.loads(inv.args[inv.args.index("--json-schema") + 1])


# --- build_invocation: the flag set ---------------------------------------------------------


def test_build_invocation_flag_set_and_stdin_prompt(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(
        agent(), parent_env={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-secret"}
    )
    task = spec(routing=Routing(model="claude-sonnet-5-5", effort="high"))
    inv = adapter.build_invocation(task, ctx(tmp_path))
    assert inv.cmd == "claude"
    assert inv.cwd == tmp_path / "worktree"
    assert inv.stdin == "Do the thing\n"
    assert inv.full_env is True
    assert inv.args[:3] == ("-p", "--output-format", "json")
    assert inv.args[3] == "--json-schema"
    schema = schema_arg(inv)
    assert "$schema" not in schema
    assert schema.get("type") == "object"
    assert {"id", "status"} <= set(schema.get("required", []))
    assert inv.args[5:] == (
        "--model",
        "claude-sonnet-5-5",
        "--effort",
        "high",
        "--dangerously-skip-permissions",
        "--bare",
    )


def test_build_invocation_omits_model_and_effort_when_unset(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert "--model" not in inv.args
    assert "--effort" not in inv.args
    assert "--dangerously-skip-permissions" in inv.args
    assert "--bare" not in inv.args


def test_default_model_id_passes_no_model_flag(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(routing=Routing(model="default")), ctx(tmp_path))
    assert "--model" not in inv.args


def test_schema_has_no_schema_key_but_the_generated_file_does(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    schema = schema_arg(adapter.build_invocation(spec(), ctx(tmp_path)))
    assert "$schema" not in schema
    generated = json.loads(
        (Path(__file__).parents[1] / "schemas" / "result-spec.json").read_text(encoding="utf-8")
    )
    assert "$schema" in generated  # the adapter strips exactly this key (section 5.4)


def test_the_inline_schema_is_exactly_the_generated_result_schema(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    schema = schema_arg(adapter.build_invocation(spec(), ctx(tmp_path)))
    expected = {key: value for key, value in schema_for(ResultSpec).items() if key != "$schema"}
    assert schema == expected


# --- permission mapping (section 7.2 / 9.3) ------------------------------------------------


def test_full_maps_to_dangerously_skip_permissions(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, permission="full"))
    assert "--dangerously-skip-permissions" in inv.args
    assert "--permission-mode" not in inv.args


def test_readonly_maps_to_permission_mode_plan(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, permission="readonly"))
    idx = inv.args.index("--permission-mode")
    assert inv.args[idx + 1] == "plan"
    assert "--dangerously-skip-permissions" not in inv.args


def test_workspace_fails_closed(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    with pytest.raises(PermissionUnsupported, match="workspace"):
        adapter.build_invocation(spec(), ctx(tmp_path, permission="workspace"))


def test_probe_advertises_only_full(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), cmd=sys.executable, parent_env=dict(os.environ))
    probe = asyncio.run(adapter.probe())
    assert probe.available is True
    assert probe.levels == ("full",)
    assert probe.version is not None
    assert probe.version.startswith("Python")


def test_probe_records_the_auth_mode() -> None:
    plain = ClaudeAdapter(agent(), cmd=sys.executable, parent_env=dict(os.environ))
    assert "auth-mode=subscription" in asyncio.run(plain.probe()).notes
    keyed = ClaudeAdapter(
        agent(),
        cmd=sys.executable,
        parent_env={**os.environ, "ANTHROPIC_API_KEY": "sk-ant-probe"},
    )
    assert "auth-mode=api-key" in asyncio.run(keyed.probe()).notes


def test_probe_unavailable_when_command_missing() -> None:
    adapter = ClaudeAdapter(
        agent(), cmd="definitely-not-installed-xyz", parent_env=dict(os.environ)
    )
    probe = asyncio.run(adapter.probe())
    assert probe.available is False
    assert probe.levels == ("full",)


# --- auth mode: `--bare` only with an API key (section 7.2) -------------------------------


def test_bare_is_omitted_for_subscription_like_environment(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={"PATH": "/bin", "HOME": str(tmp_path)})
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert "--bare" not in inv.args
    assert "ANTHROPIC_API_KEY" not in inv.env


def test_bare_and_api_key_when_api_key_auth_is_configured(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(
        agent(), parent_env={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-secret"}
    )
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert "--bare" in inv.args
    assert inv.env["ANTHROPIC_API_KEY"] == "sk-ant-secret"


def test_agent_env_api_key_enables_bare_and_is_forwarded(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(
        agent(env={"ANTHROPIC_API_KEY": "sk-ant-from-config"}), parent_env={"PATH": "/bin"}
    )
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert "--bare" in inv.args
    assert inv.env["ANTHROPIC_API_KEY"] == "sk-ant-from-config"


# --- environment: allowlist and no parent-secret leak (section 9.4) -----------------------


def test_invocation_env_contains_no_parent_only_secret(tmp_path: Path) -> None:
    home = str(tmp_path / "home")
    parent = {
        "PATH": "/bin",
        "HOME": home,
        "GITHUB_TOKEN": "ghp-secret-noise",
        "AWS_SECRET_ACCESS_KEY": "aws-noise",
    }
    adapter = ClaudeAdapter(agent(), parent_env=parent)
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert inv.full_env is True
    assert "GITHUB_TOKEN" not in inv.env
    assert "AWS_SECRET_ACCESS_KEY" not in inv.env
    assert inv.env["PATH"] == "/bin"
    assert inv.env.get("HOME") == (home if os.name != "nt" else None)


def test_env_forwards_only_allowlisted_auth_keys(tmp_path: Path) -> None:
    parent = {
        "PATH": "/bin",
        "ANTHROPIC_API_KEY": "sk-ant-secret",
        "OPENAI_API_KEY": "sk-openai-noise",  # another provider's key must not leak
    }
    adapter = ClaudeAdapter(agent(), parent_env=parent)
    inv = adapter.build_invocation(spec(), ctx(tmp_path))
    assert inv.env["ANTHROPIC_API_KEY"] == "sk-ant-secret"
    assert "OPENAI_API_KEY" not in inv.env


# --- resume -------------------------------------------------------------------------------


def test_resume_flag_when_resume_session_is_set(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, resume_session="sess-123"))
    idx = inv.args.index("--resume")
    assert inv.args[idx + 1] == "sess-123"


def test_no_resume_flag_by_default(tmp_path: Path) -> None:
    adapter = ClaudeAdapter(agent(), parent_env={})
    assert "--resume" not in adapter.build_invocation(spec(), ctx(tmp_path)).args


def test_session_id_extraction_and_resume_round_trip(tmp_path: Path) -> None:
    first = claude_fixture("claude_schema_done.json")
    resumed = claude_fixture("claude_resume.json")
    result = parse(tmp_path, json.dumps(first))
    assert result.session is not None
    sid = result.session.cli_session_id
    assert sid == first["session_id"] == resumed["session_id"]
    adapter = ClaudeAdapter(agent(), parent_env={})
    inv = adapter.build_invocation(spec(), ctx(tmp_path, resume_session=sid))
    idx = inv.args.index("--resume")
    assert inv.args[idx + 1] == sid


# --- parsing order (section 7.3) ----------------------------------------------------------


def test_parse_native_structured_output(tmp_path: Path) -> None:
    data = claude_fixture("claude_schema_done.json")
    result = parse(tmp_path, json.dumps(data), duration_s=3.9)
    assert result.status == "done"
    assert result.summary.startswith("The README.md title is")
    assert result.session is not None
    assert result.session.cli_session_id == data["session_id"]
    assert result.model == "claude-sonnet-5-5"
    assert result.agent == "claude"


def test_parse_native_needs_input(tmp_path: Path) -> None:
    data = claude_fixture("claude_schema_needs_input.json")
    result = parse(tmp_path, json.dumps(data))
    assert result.status == "needs_input"
    assert result.question_revision == 1
    assert {q.kind for q in result.open_questions} == {"free", "choice"}
    assert len(result.open_questions) >= 2


def test_agent_declared_operational_fields_are_overwritten(tmp_path: Path) -> None:
    """A `structured_output` may not self-report session, model or usage (section 5.4/8.5)."""
    envelope: dict[str, Any] = {
        "type": "result",
        "session_id": "00000000-0000-0000-0000-000000000099",
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "modelUsage": {"claude-sonnet-5-5": {}},
        "structured_output": {
            "id": "n1",
            "status": "done",
            "summary": "ok",
            "model": "fake-model",
            "session": {"cli_session_id": "fake-session"},
            "usage": {"cost_usd": 9999.0, "tokens_in": 123456, "tokens_out": 654321},
        },
    }
    result = parse(tmp_path, json.dumps(envelope))
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000099"
    assert result.model == "claude-sonnet-5-5"
    assert result.usage is not None
    assert result.usage.cost_usd == pytest.approx(0.42)
    assert result.usage.tokens_in == 1
    assert result.usage.tokens_out == 2


def test_agent_declared_id_and_agent_are_overwritten(tmp_path: Path) -> None:
    """The orchestrator owns `id`/`agent`; a structured output cannot spoof either (5.4)."""
    envelope = {
        "type": "result",
        "structured_output": {
            "id": "other-node",
            "agent": "cursor",
            "status": "done",
            "summary": "ok",
        },
    }
    result = parse(tmp_path, json.dumps(envelope))
    assert result.id == "n1"
    assert result.agent == "claude"


def test_malformed_operational_fields_do_not_fail_validation(tmp_path: Path) -> None:
    """Mistyped agent-supplied operational fields are stripped, never validated (5.4/8.5)."""
    envelope: dict[str, Any] = {
        "type": "result",
        "session_id": "00000000-0000-0000-0000-000000000099",
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "modelUsage": {"claude-sonnet-5-5": {}},
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
    result = parse(tmp_path, json.dumps(envelope))  # must not raise
    assert result.id == "n1"
    assert result.agent == "claude"
    assert result.model == "claude-sonnet-5-5"
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000099"
    assert result.usage is not None
    assert result.usage.tokens_in == 1
    assert result.usage.cost_usd == pytest.approx(0.42)


def test_parse_result_spec_schema_native(tmp_path: Path) -> None:
    data = claude_fixture("claude_result_spec_schema.json")
    result = parse(tmp_path, json.dumps(data))
    assert result.status == "done"
    assert result.id == "n1"


def test_native_beats_the_node_file(tmp_path: Path) -> None:
    data = claude_fixture("claude_schema_done.json")
    result = parse(
        tmp_path,
        json.dumps(data),
        node_file="id: n1\nstatus: done\nsummary: from-file\n",
    )
    assert result.summary.startswith("The README.md title is")


def test_node_file_fallback_when_no_structured_output(tmp_path: Path) -> None:
    stdout = json.dumps(claude_fixture("claude_plain.json"))  # no `structured_output`
    result = parse(tmp_path, stdout, node_file="id: n1\nstatus: done\nsummary: from-file\n")
    assert result.summary == "from-file"


def test_stdout_block_fallback_json(tmp_path: Path) -> None:
    stdout = 'prose\n```json\n{"id": "n1", "status": "done", "summary": "from-block"}\n```\n'
    result = parse(tmp_path, stdout)
    assert result.status == "done"
    assert result.summary == "from-block"


def test_stdout_block_fallback_yaml(tmp_path: Path) -> None:
    stdout = "```yaml\nid: n1\nstatus: done\nsummary: yaml-block\n```\n"
    result = parse(tmp_path, stdout)
    assert result.summary == "yaml-block"


def test_synthesized_from_exit_code_and_last_message(tmp_path: Path) -> None:
    data = claude_fixture("claude_plain.json")  # `result` is "OK", no structured output
    result = parse(tmp_path, json.dumps(data))
    assert result.status == "done"
    assert result.summary == "OK"
    failed = parse(tmp_path, json.dumps(data), exit_code=1)
    assert failed.status == "failed"


def test_malformed_node_file_raises_result_invalid(tmp_path: Path) -> None:
    with pytest.raises(ResultInvalid):
        parse(tmp_path, "plain text", node_file="status: invalid")


def test_unparsable_node_file_raises_result_invalid(tmp_path: Path) -> None:
    with pytest.raises(ResultInvalid):
        parse(tmp_path, "plain text", node_file="id: [unclosed")


def test_invalid_native_result_raises_result_invalid(tmp_path: Path) -> None:
    envelope = {"type": "result", "structured_output": {"id": "n1"}}  # missing `status`
    with pytest.raises(ResultInvalid):
        parse(tmp_path, json.dumps(envelope))


# --- usage normalization (section 8.5) ----------------------------------------------------


def test_usage_buckets_are_normalized_and_disjoint(tmp_path: Path) -> None:
    data = claude_fixture("claude_plain.json")
    result = parse(tmp_path, json.dumps(data))
    assert result.usage is not None
    assert result.usage.tokens_in == 2
    assert result.usage.tokens_out == 4
    assert result.usage.cost_usd == pytest.approx(data["total_cost_usd"])
    dump = result.usage.model_dump()
    assert dump["cache_read_tokens"] == 15552
    assert dump["cache_write_tokens"] == 22018
    # uncached input excludes the cache buckets, so cost needs no subtraction (section 8.5)
    assert dump["tokens_in"] < dump["cache_read_tokens"]


def test_usage_uses_the_model_that_actually_ran(tmp_path: Path) -> None:
    data = claude_fixture("claude_plain.json")
    result = parse(tmp_path, json.dumps(data))
    assert result.model == "claude-sonnet-5-5"


# --- live smoke (optional; skipped without RELAY_LIVE=1) ----------------------------------


@pytest.mark.skipif(os.environ.get("RELAY_LIVE") != "1", reason="live smoke needs RELAY_LIVE=1")
def test_live_smoke_probe_and_parse(tmp_path: Path) -> None:
    import shutil

    from relay.adapters.base import run_invocation

    if shutil.which("claude") is None:
        pytest.skip("claude is not installed")
    adapter = ClaudeAdapter(agent(), parent_env=dict(os.environ))
    probe = asyncio.run(adapter.probe())
    assert probe.available is True
    assert probe.levels == ("full",)
    token = "smoke-" + secrets.token_hex(6)
    task = spec()
    task.goal = (
        f"Do not run tools or modify files. Remember the token {token}. "
        "Return a result spec with id n1, status done and summary smoke ready."
    )
    context = ctx(tmp_path)
    proc = run_invocation(adapter.build_invocation(task, context), timeout_s=75)
    assert proc.exit_code == 0
    result = asyncio.run(adapter.parse_result(proc, task, context))
    assert result.status == "done"
    assert result.session is not None

    task.goal = (
        "Do not run tools or modify files. Return a result spec with status done and summary "
        "exactly equal to the token you were asked to remember in the previous request."
    )
    resumed = replace(context, attempt=2, resume_session=result.session.cli_session_id)
    proc = run_invocation(adapter.build_invocation(task, resumed), timeout_s=75)
    assert proc.exit_code == 0
    result = asyncio.run(adapter.parse_result(proc, task, resumed))
    assert result.status == "done"
    assert result.summary == token
