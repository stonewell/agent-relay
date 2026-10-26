"""Codex adapter (task 1.10, design sections 7.2, 7.3, 5.4, 9.3, 9.4).

Deterministic tests only: invocation snapshots, strict-schema conversion, golden parsing
of the recorded JSONL, and environment allowlisting. No network.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest

from harness import context, git
from relay.adapters.base import ProcessResult, RunContext, advertised_levels
from relay.adapters.codex import CodexAdapter, ResultInvalid, drop_nulls, strict_schema
from relay.core.specs import ResultSpec, TaskSpec, schema_for
from relay.core.specs.task import Routing

DIR = Path(__file__).parent / "fixtures" / "recorded"


@dataclass(frozen=True)
class _Agent:
    env: dict[str, str]


def _spec(**routing: str) -> TaskSpec:
    return TaskSpec(
        id="n1",
        run_id="2026-10-22-test",
        category="implement",
        goal="Add debounce",
        routing=Routing(**routing),
    )


def _parse(
    adapter: CodexAdapter, proc: ProcessResult, spec: TaskSpec, ctx: RunContext
) -> ResultSpec:
    return asyncio.run(adapter.parse_result(proc, spec, ctx))


# --- build_invocation: snapshot ---------------------------------------------------------------


def test_build_invocation_shape(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    inv = adapter.build_invocation(_spec(), ctx)

    assert inv.cmd == "codex"
    assert inv.args[0] == "exec"
    assert inv.args[-1] == "-"  # prompt comes from stdin, never argv
    assert inv.stdin == "Add debounce"
    assert inv.cwd == ctx.worktree
    assert inv.full_env is True

    assert "--json" in inv.args
    assert "-C" in inv.args and str(ctx.worktree) in inv.args

    # The last-message file goes to the node dir (design 6).
    o_idx = inv.args.index("-o")
    assert Path(inv.args[o_idx + 1]) == ctx.node_dir / "codex-output.json"

    # The schema file is written in strict form.
    schema_idx = inv.args.index("--output-schema")
    schema_path = Path(inv.args[schema_idx + 1])
    assert schema_path == ctx.node_dir / "codex-schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    # The router owns worktrees and `exec` has no top-level approval flag (design 7.2).
    assert "--worktree" not in inv.args
    assert "-a" not in inv.args
    assert "--ask-for-approval" not in inv.args
    assert "--yolo" not in inv.args


def test_build_invocation_model_and_effort(tmp_path: Path) -> None:
    inv = CodexAdapter().build_invocation(
        _spec(model="gpt-5.1-codex", effort="high"), context(tmp_path)
    )
    assert "-m" in inv.args
    assert "gpt-5.1-codex" in inv.args
    assert "-c" in inv.args
    assert "model_reasoning_effort=high" in inv.args


def test_build_invocation_omits_model_flag_for_default_tier(tmp_path: Path) -> None:
    inv = CodexAdapter().build_invocation(_spec(model="default"), context(tmp_path))
    assert "-m" not in inv.args
    assert "-c" not in inv.args  # no effort either


def test_build_invocation_resume_session(tmp_path: Path) -> None:
    thread_id = "00000000-0000-0000-0000-000000000010"
    ctx = replace(context(tmp_path), resume_session=thread_id)
    inv = CodexAdapter().build_invocation(_spec(), ctx)
    assert inv.args[1:3] == ("resume", thread_id)


def test_build_invocation_permission_flags(tmp_path: Path) -> None:
    adapter = CodexAdapter(levels=("full", "workspace", "readonly"))
    expected = {
        "full": ("--dangerously-bypass-approvals-and-sandbox",),
        "readonly": ("-s", "read-only"),
        "workspace": ("-s", "workspace-write"),
    }
    for level, flags in expected.items():
        ctx = replace(context(tmp_path), permission=level)
        inv = adapter.build_invocation(_spec(), ctx)
        for flag in flags:
            assert flag in inv.args
    # Full permission never uses the short `--yolo` alias (design 7.2).
    full = replace(context(tmp_path), permission="full")
    assert "--yolo" not in adapter.build_invocation(_spec(), full).args


def test_build_invocation_fails_closed_for_unadvertised_level(tmp_path: Path) -> None:
    adapter = CodexAdapter()  # never advertises `workspace`
    ctx = replace(context(tmp_path), permission="workspace")
    with pytest.raises(ValueError, match="cannot honour"):
        adapter.build_invocation(_spec(), ctx)


# --- probe ------------------------------------------------------------------------------------


def test_probe_reports_missing_executable() -> None:
    probe = asyncio.run(CodexAdapter(cmd="no-such-codex-9f3a").probe())
    assert probe.available is False
    assert probe.levels == ("full",)


def test_probe_parses_version_and_levels() -> None:
    probe = asyncio.run(CodexAdapter(cmd=sys.executable).probe())
    assert probe.available is True
    assert probe.version is not None and probe.version.count(".") >= 1
    assert probe.levels == advertised_levels("codex")  # only canary-verified platforms (2.30)
    assert "workspace" not in probe.levels


# --- strict schema conversion (design 5.4) ----------------------------------------------------


def _every_object(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        node = cast("dict[str, Any]", schema)
        if isinstance(node.get("properties"), dict):
            yield node
        for value in node.values():
            yield from _every_object(value)
    elif isinstance(schema, list):
        for item in cast("list[Any]", schema):
            yield from _every_object(item)


def _accepts_null(node: Any) -> bool:
    if isinstance(node, dict):
        schema = cast("dict[str, Any]", node)
        node_type = schema.get("type")
        if node_type == "null":
            return True
        if isinstance(node_type, list) and "null" in node_type:
            return True
        for key in ("anyOf", "oneOf"):
            branches = schema.get(key)
            if isinstance(branches, list) and any(
                _accepts_null(b) for b in cast("list[Any]", branches)
            ):
                return True
    return False


def test_strict_schema_every_object_is_strict() -> None:
    strict = strict_schema(schema_for(ResultSpec))
    assert "$schema" not in strict
    for obj in _every_object(strict):
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_strict_schema_makes_optional_properties_nullable() -> None:
    strict = strict_schema(schema_for(ResultSpec))
    props = strict["properties"]
    # Optional fields (non-null defaults in the original) become nullable.
    assert _accepts_null(props["summary"])
    assert _accepts_null(props["artifacts"])
    assert _accepts_null(props["spec_version"])
    # Required fields stay non-nullable.
    assert not _accepts_null(props["id"])
    assert not _accepts_null(props["status"])


def test_strict_schema_rejects_extras_at_every_level() -> None:
    strict = strict_schema(schema_for(ResultSpec))
    assert strict["additionalProperties"] is False
    assert strict["$defs"]["OpenQuestion"]["additionalProperties"] is False
    # Optional fields with non-null defaults are now nullable, e.g. `kind` (default "free").
    kind = strict["$defs"]["OpenQuestion"]["properties"]["kind"]
    assert _accepts_null(kind)


def test_drop_nulls_removes_nested_nulls() -> None:
    value = {"a": 1, "b": None, "c": {"d": None, "e": 2}, "f": [None, 3, {"g": None}]}
    # Null list items are kept (only dict null values are dropped), matching the
    # reference behaviour in tests/test_recorded_outputs.py.
    assert drop_nulls(value) == {"a": 1, "c": {"e": 2}, "f": [None, 3, {}]}


# --- golden parsing of the recorded JSONL -----------------------------------------------------


def test_parse_plain_run_golden(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    proc = ProcessResult(
        exit_code=0, stdout=(DIR / "codex_plain.jsonl").read_text(encoding="utf-8"), duration_s=1.5
    )
    result = _parse(adapter, proc, _spec(), context(tmp_path))
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000010"
    assert result.status == "done"
    assert result.summary == "OK"
    assert result.usage is not None
    assert result.usage.tokens_in == 13990 - 12288  # cached tokens subtracted (section 8.5)
    assert result.usage.tokens_out == 5
    assert result.usage.duration_s == 1.5


def test_parse_schema_run_golden(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "codex-output.json").write_text(
        (DIR / "codex_schema_needs_input_last_message.txt").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    proc = ProcessResult(
        exit_code=0,
        stdout=(DIR / "codex_schema_needs_input.jsonl").read_text(encoding="utf-8"),
        duration_s=2.5,
    )
    result = _parse(adapter, proc, _spec(), ctx)
    assert result.status == "needs_input"
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000011"
    assert result.question_revision == 1
    assert len(result.open_questions) == 3
    assert all(q.options is None for q in result.open_questions)  # nulls dropped, not empty list
    assert result.summary.startswith("The repository contains only a README")
    assert result.usage is not None
    assert result.usage.tokens_in == 43371 - 28544
    assert result.usage.tokens_out == 373


def test_parse_schema_run_from_stream_text_when_no_output_file(tmp_path: Path) -> None:
    """The final agent_message text carries the same JSON; parse it without the `-o` file."""
    adapter = CodexAdapter()
    proc = ProcessResult(
        exit_code=0,
        stdout=(DIR / "codex_schema_needs_input.jsonl").read_text(encoding="utf-8"),
        duration_s=1.0,
    )
    result = _parse(adapter, proc, _spec(), context(tmp_path))
    assert result.status == "needs_input"
    assert len(result.open_questions) == 3


def test_parse_resume_keeps_the_thread_id(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    proc = ProcessResult(
        exit_code=0, stdout=(DIR / "codex_resume.jsonl").read_text(encoding="utf-8"), duration_s=1.0
    )
    result = _parse(adapter, proc, _spec(), context(tmp_path))
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000010"
    assert result.usage is not None
    assert result.usage.tokens_in == 28213 - 26112


def test_parse_rejected_schema_does_not_crash(tmp_path: Path) -> None:
    """A schema-rejected run ends with turn.failed, no message; the parser synthesizes."""
    adapter = CodexAdapter()
    proc = ProcessResult(
        exit_code=1,
        stdout=(DIR / "codex_schema_rejected.jsonl").read_text(encoding="utf-8"),
        duration_s=0.5,
    )
    result = _parse(adapter, proc, _spec(), context(tmp_path))
    assert result.status == "failed"
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000012"


@pytest.mark.parametrize(
    "stdout",
    [
        '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000010"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1',  # truncated JSON
        "not json at all\n",
        '{"type":"thread.started"}\n[1,2,3]\n',
    ],
)
def test_parse_malformed_or_truncated_jsonl_raises(tmp_path: Path, stdout: str) -> None:
    adapter = CodexAdapter()
    proc = ProcessResult(exit_code=0, stdout=stdout)
    with pytest.raises(ResultInvalid, match="malformed"):
        _parse(adapter, proc, _spec(), context(tmp_path))


def test_parse_result_from_node_dir_file(tmp_path: Path) -> None:
    """Parsing order step 2: a `result.spec.yaml` in the node dir wins over the stream."""
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "result.spec.yaml").write_text(
        "id: n1\nstatus: done\nsummary: from node file\n", encoding="utf-8"
    )
    proc = ProcessResult(exit_code=0, stdout="", duration_s=0.1)
    result = _parse(adapter, proc, _spec(), ctx)
    assert result.summary == "from node file"


def test_parse_invalid_native_output_raises_result_invalid(tmp_path: Path) -> None:
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "codex-output.json").write_text('{"status": "bogus"}\n', encoding="utf-8")
    proc = ProcessResult(exit_code=0, stdout="", duration_s=0.1)
    with pytest.raises(ResultInvalid):
        _parse(adapter, proc, _spec(), ctx)


def test_agent_declared_operational_fields_are_overwritten(tmp_path: Path) -> None:
    """The stream and routing own session/model/usage; a structured output may not (5.4/8.5)."""
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "codex-output.json").write_text(
        json.dumps(
            {
                "id": "n1",
                "status": "done",
                "summary": "ok",
                "model": "fake-model",
                "session": {"cli_session_id": "fake-session"},
                "usage": {"cost_usd": 9999.0, "tokens_in": 123456, "tokens_out": 654321},
            }
        ),
        encoding="utf-8",
    )
    stdout = (
        '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000020"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,'
        '"output_tokens":5}}\n'
    )
    proc = ProcessResult(exit_code=0, stdout=stdout, duration_s=1.0)
    result = _parse(adapter, proc, _spec(model="gpt-5.1-codex"), ctx)
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000020"
    assert result.model == "gpt-5.1-codex"
    assert result.usage is not None
    assert result.usage.cost_usd is None  # codex never reports cost; the fake 9999 is gone
    assert result.usage.tokens_in == 100
    assert result.usage.tokens_out == 5


def test_agent_declared_id_and_agent_are_overwritten(tmp_path: Path) -> None:
    """The orchestrator owns `id`/`agent`; a structured output cannot spoof either (5.4)."""
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "codex-output.json").write_text(
        json.dumps({"id": "other-node", "agent": "cursor", "status": "done", "summary": "ok"}),
        encoding="utf-8",
    )
    proc = ProcessResult(exit_code=0, stdout="", duration_s=0.1)
    result = _parse(adapter, proc, _spec(), ctx)
    assert result.id == "n1"
    assert result.agent == "codex"


def test_malformed_operational_fields_do_not_fail_validation(tmp_path: Path) -> None:
    """Mistyped agent-supplied operational fields are stripped, never validated (5.4/8.5)."""
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "codex-output.json").write_text(
        json.dumps(
            {
                "status": "done",
                "summary": "ok",
                "id": 999,
                "agent": ["evil"],
                "model": 123,
                "session": "not-a-session",
                "usage": "evil",
            }
        ),
        encoding="utf-8",
    )
    stdout = (
        '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000020"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":0,'
        '"output_tokens":5}}\n'
    )
    proc = ProcessResult(exit_code=0, stdout=stdout, duration_s=1.0)
    result = _parse(adapter, proc, _spec(model="gpt-5.1-codex"), ctx)  # must not raise
    assert result.id == "n1"
    assert result.agent == "codex"
    assert result.model == "gpt-5.1-codex"
    assert result.session is not None
    assert result.session.cli_session_id == "00000000-0000-0000-0000-000000000020"
    assert result.usage is not None
    assert result.usage.tokens_in == 100
    assert result.usage.tokens_out == 5


def test_default_model_sentinel_is_not_recorded_as_a_model_id(tmp_path: Path) -> None:
    """`default` is not a priced model id; record `None` instead of the literal sentinel."""
    adapter = CodexAdapter()
    proc = ProcessResult(exit_code=0, stdout="", duration_s=0.1)
    assert _parse(adapter, proc, _spec(model="default"), context(tmp_path)).model is None
    assert _parse(adapter, proc, _spec(), context(tmp_path)).model is None
    real = _parse(adapter, proc, _spec(model="gpt-5.1-codex"), context(tmp_path))
    assert real.model == "gpt-5.1-codex"


def test_native_beats_node_file_and_stdout_block(tmp_path: Path) -> None:
    """Parsing-order step 1 (the `-o` file) wins over the node-dir file (step 2).

    Codex's ``--json`` stream is always JSONL, so a fenced stdout block (step 3) cannot
    appear without ``_parse_events`` rejecting the line; the generic native > node-file >
    block ordering is covered in tests/test_parsing.py. This test pins the codex-reachable
    half of the precedence: the native output file beats the node-dir file.
    """
    adapter = CodexAdapter()
    ctx = context(tmp_path)
    (ctx.node_dir / "codex-output.json").write_text(
        json.dumps({"status": "done", "summary": "from native"}), encoding="utf-8"
    )
    (ctx.node_dir / "result.spec.yaml").write_text(
        "id: n1\nstatus: done\nsummary: from node file\n", encoding="utf-8"
    )
    stdout = (
        '{"type":"thread.started","thread_id":"00000000-0000-0000-0000-000000000021"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n'
    )
    proc = ProcessResult(exit_code=0, stdout=stdout, duration_s=0.1)
    result = _parse(adapter, proc, _spec(), ctx)
    assert result.summary == "from native"


# --- environment allowlisting (design 9.4) ----------------------------------------------------


@pytest.fixture
def no_codex_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)


def test_build_invocation_env_has_no_parent_only_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_codex_auth: None
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-noise")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("SOME_RANDOM_VAR", "noise")
    inv = CodexAdapter().build_invocation(_spec(), context(tmp_path))
    assert inv.full_env is True
    assert "GITHUB_TOKEN" not in inv.env
    assert "AWS_SECRET_ACCESS_KEY" not in inv.env
    assert "SOME_RANDOM_VAR" not in inv.env


def test_build_invocation_passes_auth_keys_only_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_codex_auth: None
) -> None:
    adapter = CodexAdapter()
    inv = adapter.build_invocation(_spec(), context(tmp_path))
    assert "OPENAI_API_KEY" not in inv.env
    assert "CODEX_API_KEY" not in inv.env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
    inv = adapter.build_invocation(_spec(), context(tmp_path))
    assert inv.env["OPENAI_API_KEY"] == "sk-test-openai"
    assert "CODEX_API_KEY" not in inv.env

    monkeypatch.setenv("CODEX_API_KEY", "ck-test")
    inv = adapter.build_invocation(_spec(), context(tmp_path))
    assert inv.env["CODEX_API_KEY"] == "ck-test"


def test_build_invocation_layers_agent_env_additions(tmp_path: Path, no_codex_auth: None) -> None:
    adapter = CodexAdapter(agent=_Agent(env={"MY_CODEX_FLAG": "1"}))
    inv = adapter.build_invocation(_spec(), context(tmp_path))
    assert inv.env["MY_CODEX_FLAG"] == "1"


# --- live smoke (optional, requires codex and RELAY_LIVE=1) -----------------------------------


def test_live_smoke(tmp_path: Path) -> None:
    if os.environ.get("RELAY_LIVE") != "1" or shutil.which("codex") is None:
        pytest.skip("live smoke needs codex installed and RELAY_LIVE=1")
    from relay.adapters.base import run_invocation

    adapter = CodexAdapter()
    ctx = context(tmp_path)
    git(ctx.worktree, "init", "-q")
    spec = _spec(model="default")
    spec.goal = (
        "Do not run tools or modify files. Return a result spec with id n1, "
        "status done and summary adapter smoke passed."
    )
    proc = run_invocation(adapter.build_invocation(spec, ctx), timeout_s=60)
    assert proc.exit_code == 0
    result = _parse(adapter, proc, spec, ctx)
    assert result.status == "done"
