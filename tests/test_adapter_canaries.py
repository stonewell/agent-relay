"""Adapter canaries (task 1.12).

These tests drive :func:`relay.core.doctor.run_*_canary` and the `paid`/free doctor checks
they back, using fake CLI executables that emulate valid/invalid structured output, a session
id with a successful/failed resume, quoting/newline echo, a missing command and a hang. No
real CLI, network, credentials or paid call is used; every canary runs in a throwaway
directory that is removed afterwards.

The classification contract under test (design sections 7, 14, 17):

- ``passed``: the behaviour was observed as the design documents it.
- ``failed``: the observed behaviour contradicts the design.
- ``inconclusive``: the behaviour could not be confirmed.
- ``unavailable``: the command could not be resolved (CLI not installed).
- ``error``: the canary itself failed or timed out.

The child's exit code is never the sole evidence; the parsed payload and the echoed text
decide the outcome.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from relay.cli.main import main
from relay.core import registry as registry_module
from relay.core.doctor import (
    ADAPTER_CANARY_SPECS,
    CANARY_KINDS,
    AdapterCanarySpec,
    DoctorContext,
    adapter_canary_check,
    default_registry,
    run_auth_canary,
    run_input_canary,
    run_quote_canary,
    run_session_canary,
    run_structured_canary,
    run_version_canary,
)

WINDOWS = os.name == "nt"


def spec(adapter: str) -> AdapterCanarySpec:
    for candidate in ADAPTER_CANARY_SPECS:
        if candidate.adapter == adapter:
            return candidate
    raise AssertionError(f"no spec for {adapter}")


def _with_cmd(adapter: str, cmd: str) -> AdapterCanarySpec:
    from dataclasses import replace

    return replace(spec(adapter), cmd=cmd)


def py_cli(directory: Path, name: str, source: str) -> Path:
    """A Python fake CLI: a shebang script on POSIX, a `.cmd` shim on Windows."""
    if WINDOWS:
        script = directory / f"{name}.py"
        script.write_text(source, encoding="utf-8")
        shim = directory / f"{name}.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return shim
    script = directory / name
    script.write_text(f"#!/usr/bin/env python3\n{source}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def isolated(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> None:
    monkeypatch.setattr(registry_module, "USER_CONFIG", tmp / "no-user-config.yaml")


# --- version -----------------------------------------------------------------------------------


def test_version_parses_a_semver(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", "import sys; print('2.1.308 (Claude Code)')\n")
    result = run_version_canary(_with_cmd("claude", str(fake)))
    assert result.outcome == "passed"
    assert result.data["version"] == "2.1.308"


def test_version_missing_command_is_unavailable(tmp_path: Path) -> None:
    result = run_version_canary(_with_cmd("codex", "definitely-not-installed-xyz"))
    assert result.outcome == "unavailable"


def test_version_fails_when_the_command_errors(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "agy", "import sys; print('boom', file=sys.stderr); sys.exit(3)\n")
    result = run_version_canary(_with_cmd("agy", str(fake)))
    assert result.outcome == "failed"


# --- auth mode (free; never calls a model or prints a secret) ---------------------------------


def test_claude_auth_mode_detection() -> None:
    s = spec("claude")
    api = run_auth_canary(s, parent_env={"ANTHROPIC_API_KEY": "sk-ant-secret"}, agent_env={})
    assert api.outcome == "passed"
    assert api.data["auth_mode"] == "api-key"
    sub = run_auth_canary(s, parent_env={}, agent_env={})
    assert sub.data["auth_mode"] == "subscription"


def test_codex_auth_mode_detection() -> None:
    s = spec("codex")
    api = run_auth_canary(s, parent_env={"CODEX_API_KEY": "sk-codex"}, agent_env={})
    assert api.data["auth_mode"] == "api-key"
    login = run_auth_canary(s, parent_env={}, agent_env={})
    assert login.data["auth_mode"] == "chatgpt-login"


def test_agy_auth_mode_detection_from_agent_env() -> None:
    s = spec("agy")
    api = run_auth_canary(s, parent_env={}, agent_env={"GEMINI_API_KEY": "k"})
    assert api.data["auth_mode"] == "api-key"
    keyring = run_auth_canary(s, parent_env={}, agent_env={})
    assert keyring.data["auth_mode"] == "keyring"


def test_auth_canary_never_leaks_the_key_value() -> None:
    s = spec("claude")
    result = run_auth_canary(
        s, parent_env={"ANTHROPIC_API_KEY": "sk-ant-super-secret"}, agent_env={}
    )
    rendered = json.dumps(result.data) + " " + result.note
    assert "sk-ant-super-secret" not in rendered
    assert result.data["key_names"] == ["ANTHROPIC_API_KEY"]  # names only, never values


# --- structured output -------------------------------------------------------------------------

_STRUCTURED_OK = (
    "import json, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'type': 'result', 'structured_output': "
    "{'value': 'hello-canary', 'count': 42}}))\n"
)
_STRUCTURED_INVALID = (
    "import json, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'type': 'result', 'structured_output': {'value': 'hello-canary'}}))\n"
)
_TEXT_FALLBACK = (
    "import json, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'type': 'result', "
    '\'result\': \'{"value": "hello-canary", "count": 42}\'}))\n'
)
_GARBAGE = "import sys\nsys.stdin.read()\nprint('just some prose')\n"


def test_structured_native_schema_valid(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _STRUCTURED_OK)
    result = run_structured_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "passed"
    assert result.data["source"] == "native"
    assert result.data["field_names"] == ["count", "value"]


def test_structured_native_schema_invalid_is_classified_failed(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _STRUCTURED_INVALID)
    result = run_structured_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "failed"
    assert result.data["source"] == "native"


def test_structured_text_fallback_when_native_is_missing(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _TEXT_FALLBACK)
    result = run_structured_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "passed"
    assert result.data["source"] == "text"


def test_structured_garbage_is_inconclusive(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _GARBAGE)
    result = run_structured_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "inconclusive"


def test_structured_codex_jsonl_and_output_file(tmp_path: Path) -> None:
    # Codex writes the structured object to the `-o` file and the thread id in the JSONL.
    fake = py_cli(
        tmp_path,
        "codex",
        "import json, sys\n"
        "sys.stdin.read()\n"
        "out = None\n"
        "for i in range(1, len(sys.argv)):\n"
        "    if sys.argv[i] == '-o':\n"
        "        out = sys.argv[i + 1]\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'th-1'}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',"
        " 'text': 'done'}}))\n"
        "open(out, 'w').write(json.dumps({'value': 'hello-canary', 'count': 42}))\n",
    )
    result = run_structured_canary(_with_cmd("codex", str(fake)), parent=tmp_path)
    assert result.outcome == "passed"
    assert result.data["source"] == "native"


def test_structured_agy_envelope(tmp_path: Path) -> None:
    fake = py_cli(
        tmp_path,
        "agy",
        "import json\n"
        "print(json.dumps({'status': 'SUCCESS', 'conversation_id': 'c1',"
        " 'structured_output': {'value': 'hello-canary', 'count': 42}}))\n",
    )
    result = run_structured_canary(_with_cmd("agy", str(fake)), parent=tmp_path)
    assert result.outcome == "passed"


# --- session id extraction and resume ----------------------------------------------------------

_SESSION_FAKE = (
    "import json, os, re, sys\n"
    "state = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'canary-state.txt')\n"
    "prompt = sys.stdin.read()\n"
    "if '--resume' in sys.argv:\n"
    "    token = open(state).read().strip()\n"
    "    print(json.dumps({'type': 'result', 'session_id': 'sess-123',"
    " 'structured_output': {'answer': token}}))\n"
    "else:\n"
    "    m = re.search(r'secret token ([0-9a-f]+)', prompt)\n"
    "    token = m.group(1) if m else 'unknown'\n"
    "    open(state, 'w').write(token)\n"
    "    print(json.dumps({'type': 'result', 'session_id': 'sess-123',"
    " 'structured_output': {'answer': 'ok'}}))\n"
)


def test_session_resume_sees_prior_context(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _SESSION_FAKE)
    result = run_session_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "passed"
    assert result.data["session_field"] == "session_id"
    assert result.data["context_confirmed"] is True


def test_session_extraction_fails_without_a_session_field(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _GARBAGE)
    result = run_session_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "failed"
    assert "no session_id" in result.note


_SESSION_NO_CONTEXT = (
    "import json, sys\n"
    "prompt = sys.stdin.read()\n"
    "if '--resume' in sys.argv:\n"
    "    print(json.dumps({'type': 'result', 'session_id': 'sess-123',"
    " 'structured_output': {'answer': 'I forgot'}}))\n"
    "else:\n"
    "    print(json.dumps({'type': 'result', 'session_id': 'sess-123',"
    " 'structured_output': {'answer': 'ok'}}))\n"
)


def test_session_resume_without_confirmed_context_is_inconclusive(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _SESSION_NO_CONTEXT)
    result = run_session_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "inconclusive"
    assert result.data["extracted"] is True
    assert result.data["context_confirmed"] is False


_SESSION_ECHOED_TOKEN = (
    "import json, os, re, sys\n"
    "state = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'canary-state.txt')\n"
    "prompt = sys.stdin.read()\n"
    "if '--resume' in sys.argv:\n"
    "    token = open(state).read().strip()\n"
    "    answer = token + '-appended'\n"
    "    print(json.dumps({'type': 'result', 'session_id': 'sess-123',"
    " 'structured_output': {'answer': answer}}))\n"
    "else:\n"
    "    m = re.search(r'secret token ([0-9a-f]+)', prompt)\n"
    "    token = m.group(1) if m else 'unknown'\n"
    "    open(state, 'w').write(token)\n"
    "    print(json.dumps({'type': 'result', 'session_id': 'sess-123',"
    " 'structured_output': {'answer': 'ok'}}))\n"
)


def test_session_resume_echoed_token_is_not_confirmed(tmp_path: Path) -> None:
    """A token embedded in a longer answer is not exact equality, so it is not confirmed (F6).

    The echoed prompt contains the token as a prefix but the field value differs from the
    exact token, which a loose substring match would wrongly accept.
    """
    fake = py_cli(tmp_path, "claude", _SESSION_ECHOED_TOKEN)
    result = run_session_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "inconclusive"
    assert result.data["extracted"] is True
    assert result.data["context_confirmed"] is False


# --- quoting / newline round-trip --------------------------------------------------------------


def test_quoting_round_trips_exactly(tmp_path: Path) -> None:
    fake = py_cli(
        tmp_path,
        "claude",
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'result', 'structured_output': {'echo': '\"first\\nsecond\"'}}))\n",
    )
    result = run_quote_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "passed"
    assert result.data["exact"] is True


def test_quoting_mangled_text_is_failed(tmp_path: Path) -> None:
    fake = py_cli(
        tmp_path,
        "claude",
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type': 'result', 'structured_output': {'echo': 'first second'}}))\n",
    )
    result = run_quote_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "failed"
    assert result.data["exact"] is False


def test_quoting_no_echo_is_inconclusive(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _GARBAGE)
    result = run_quote_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == "inconclusive"


# --- timeout and cleanup ------------------------------------------------------------------------


def test_a_hanging_canary_times_out_as_error(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", "import time\ntime.sleep(30)\n")
    result = run_structured_canary(_with_cmd("claude", str(fake)), parent=tmp_path, timeout_s=0.5)
    assert result.outcome == "error"
    assert "timed out" in result.note


def test_the_throwaway_directory_is_removed_afterwards(tmp_path: Path) -> None:
    fake = py_cli(tmp_path, "claude", _STRUCTURED_OK)
    run_structured_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith("relay-canary-")]
    assert leftovers == []


# --- doctor integration: paid gating, ids, output shapes ----------------------------------------


def test_adapter_canaries_register_with_expected_ids_and_paid_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    registry = default_registry(DoctorContext(repo_root=tmp_path))
    by_id = {c.id: c for c in registry}
    for candidate in ADAPTER_CANARY_SPECS:
        for kind in CANARY_KINDS:
            check = by_id[f"adapter.{candidate.adapter}.{kind}"]
            assert check.paid is (kind in {"structured", "session", "quoting", "needs_input"})
            assert check.requires == ()


def test_default_doctor_runs_no_paid_adapter_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    ctx = DoctorContext(repo_root=tmp_path)
    report = asyncio.run(run_checks_default(ctx))
    ids = [c.id for c in report.checks]
    for candidate in ADAPTER_CANARY_SPECS:
        assert f"adapter.{candidate.adapter}.version" in ids  # free
        assert f"adapter.{candidate.adapter}.auth" in ids  # free
        assert f"adapter.{candidate.adapter}.structured" not in ids  # paid
        assert f"adapter.{candidate.adapter}.session" not in ids
        assert f"adapter.{candidate.adapter}.quoting" not in ids


def run_checks_default(ctx: DoctorContext):
    from relay.core.doctor import run_checks

    return run_checks(default_registry(ctx), ctx)


def test_paid_flag_runs_paid_adapter_canaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    fake = py_cli(tmp_path, "claude", _STRUCTURED_OK)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        f"agents:\n  claude:\n    cmd: {fake}\n"
        "  codex:\n    enabled: false\n"
        "  agy:\n    enabled: false\n"
        "    cmd: definitely-not-installed-xyz\n",
        encoding="utf-8",
    )
    ctx = DoctorContext(repo_root=tmp_path)
    from relay.core.doctor import run_checks

    report = asyncio.run(run_checks(default_registry(ctx), ctx, include_paid=True))
    ids = [c.id for c in report.checks]
    for kind in ("structured", "session", "quoting", "needs_input"):
        assert f"adapter.claude.{kind}" in ids


def test_naming_a_structured_check_runs_it_without_paid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    fake = py_cli(tmp_path, "claude", _STRUCTURED_OK)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        f"agents:\n  claude:\n    cmd: {fake}\n", encoding="utf-8"
    )
    code = main(["-C", str(tmp_path), "doctor", "--check", "adapter.claude.structured"])
    out = capsys.readouterr().out
    assert code == 0
    assert "adapter.claude.structured" in out
    assert "passed" in out


def test_adapter_canary_json_output_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    fake = py_cli(tmp_path, "claude", _STRUCTURED_OK)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        f"agents:\n  claude:\n    cmd: {fake}\n", encoding="utf-8"
    )
    assert (
        main(
            [
                "-C",
                str(tmp_path),
                "doctor",
                "--check",
                "adapter.claude.structured",
                "--json",
            ]
        )
        == 0
    )
    body = json.loads(capsys.readouterr().out)
    (check,) = body["checks"]
    assert check["id"] == "adapter.claude.structured"
    assert check["status"] == "pass"
    assert check["data"]["adapter"] == "claude"
    assert check["data"]["kind"] == "structured"
    assert check["data"]["outcome"] == "passed"
    assert check["data"]["source"] == "native"


def test_auth_check_output_leaks_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    assert main(["-C", str(tmp_path), "doctor", "--check", "adapter.claude.auth"]) == 0
    out = capsys.readouterr().out
    assert "sk-ant-very-secret" not in out
    assert "api-key" in out
    assert main(["-C", str(tmp_path), "doctor", "--check", "adapter.claude.auth", "--json"]) == 0
    assert "sk-ant-very-secret" not in capsys.readouterr().out


def test_unavailable_canary_reports_unsupported_with_a_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated(monkeypatch, tmp_path)
    # Point Antigravity at a command that cannot resolve, so the outcome does not depend on
    # whether a real `agy` happens to be installed on this machine.
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "agents.yaml").write_text(
        "agents:\n  agy:\n    cmd: definitely-not-installed-xyz\n", encoding="utf-8"
    )
    ctx = DoctorContext(repo_root=tmp_path)
    registry = default_registry(ctx)
    check = registry.get("adapter.agy.version")
    assert check is not None
    result = asyncio.run(check.run(ctx))
    assert result.status == "unsupported"
    assert result.remedy is not None
    assert "agy" in result.remedy


def test_adapter_canary_check_direct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated(monkeypatch, tmp_path)
    fake = py_cli(tmp_path, "claude", _STRUCTURED_OK)
    check = adapter_canary_check(
        _with_cmd("claude", str(fake)),
        "structured",
        parent_env=dict(os.environ),
        agent_env={},
    )
    result = asyncio.run(check.run(DoctorContext(repo_root=tmp_path)))
    assert result.status == "pass"
    assert result.data["outcome"] == "passed"


@pytest.mark.parametrize(
    "status,questions,expected",
    [
        ("needs_input", [{"id": "q1", "text": "Which choice?"}], "passed"),
        ("done", [], "failed"),
        ("needs_input", [], "failed"),
    ],
)
def test_input_canary_validates_typed_questions(
    tmp_path: Path, status: str, questions: list[dict[str, str]], expected: str
) -> None:
    payload = {"spec_version": 1, "id": "n1", "status": status, "open_questions": questions}
    script = (
        "import json,sys\nsys.stdin.read()\nprint(json.dumps("
        + repr({"type": "result", "structured_output": payload})
        + "))\n"
    )
    fake = py_cli(tmp_path, "claude", script)
    result = run_input_canary(_with_cmd("claude", str(fake)), parent=tmp_path)
    assert result.outcome == expected
    assert not any(p.name.startswith("relay-input-canary-") for p in tmp_path.iterdir())
