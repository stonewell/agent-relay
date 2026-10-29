"""Completion adapters remain unavailable until their own exact profile is reviewed."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from relay.adapters import claude_completion as module
from relay.adapters.claude_completion import ClaudeCompletionAdapter, parse_completion
from relay.adapters.completion import (
    QUALIFICATIONS,
    CompletionError,
    CompletionInvalid,
    CompletionProfile,
    CompletionQualification,
    CompletionRequest,
    CompletionUsage,
    aggregate_usage,
)
from relay.core.platform.proc import ProcessResult, ResolvedCommand
from relay.core.registry import Agent


def response(**values: Any) -> ProcessResult:
    return ProcessResult(0, json.dumps({"type": "result", "result": "hello", **values}))


def test_usage_records_disjoint_buckets_and_never_invents_missing_counters() -> None:
    recorded = Path("tests/fixtures/recorded/claude_plain.json").read_text(encoding="utf-8")
    result = parse_completion(ProcessResult(0, recorded), structured=False)
    assert result.usage is not None
    assert result.usage.prompt_tokens == 2 + 22018 + 15552
    assert result.usage.cached_tokens == 15552
    assert result.usage.cache_write_tokens == 22018
    assert result.usage.as_openai()["prompt_tokens_details"] == {"cached_tokens": 15552}
    assert parse_completion(response(), structured=False).usage is None
    assert (
        parse_completion(
            response(usage={"input_tokens": 1, "output_tokens": 2}), structured=False
        ).usage
        is None
    )
    assert aggregate_usage(result.usage, None) is None
    assert aggregate_usage(
        CompletionUsage(2, 3, 1, 0), CompletionUsage(3, 4, 2, 0)
    ) == CompletionUsage(5, 7, 3, 0)


def test_envelope_native_field_preferred_and_failure_cannot_be_repaired_as_success() -> None:
    envelope = {"content": None, "tool_calls": [{"name": "edit", "arguments": {"value": "x"}}]}
    result = parse_completion(response(structured_output=envelope, result="bogus"), structured=True)
    assert result.envelope == envelope
    assert (
        parse_completion(response(result=json.dumps(envelope)), structured=True).envelope
        == envelope
    )
    with pytest.raises(CompletionInvalid):
        parse_completion(response(result="not JSON"), structured=True)
    with pytest.raises(CompletionError) as caught:
        parse_completion(
            response(structured_output=envelope, stop_reason="max_tokens"), structured=True
        )
    assert not isinstance(caught.value, CompletionInvalid)
    with pytest.raises(CompletionError) as caught:
        parse_completion(response(is_error=True, result="secret provider error"), structured=True)
    assert "secret" not in str(caught.value)
    assert not isinstance(caught.value, CompletionInvalid)


@pytest.mark.parametrize(
    ("proc", "status"),
    [
        (ProcessResult(None, timed_out=True), 504),
        (ProcessResult(1, stderr="not logged in: secret"), 503),
        (ProcessResult(1, stderr="context_length_exceeded: secret"), 400),
        (ProcessResult(1, stderr="other secret"), 502),
    ],
)
def test_process_error_categories_do_not_expose_stderr(proc: ProcessResult, status: int) -> None:
    with pytest.raises(CompletionError) as caught:
        parse_completion(proc, structured=False)
    assert caught.value.status == status
    assert "secret" not in str(caught.value)


def test_qualification_drift_blocks_launch_and_gateway_token_never_reaches_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str], str | None]] = []
    parent = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "RELAY_GATEWAY_TOKEN": "secret"}

    def resolve(command: str, args: tuple[str, ...], **_: Any) -> ResolvedCommand:
        return ResolvedCommand((command, *args))

    def run(argv: ResolvedCommand, **kwargs: Any) -> ProcessResult:
        calls.append((list(argv.argv), kwargs["env"], kwargs.get("stdin")))
        return ProcessResult(0, "2.1.312 (Claude Code)") if "--version" in argv.argv else response()

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    agent = Agent(cmd="claude")
    unqualified = ClaudeCompletionAdapter(agent, parent_env=parent)
    profile = unqualified.profile()
    assert unqualified.inspect() is None
    request = CompletionRequest("private prompt", "sonnet")
    kwargs = {
        "cwd": tmp_path,
        "cancel": threading.Event(),
        "timeout_s": 10,
        "max_output_bytes": 1024,
    }
    with pytest.raises(CompletionError, match="not qualified"):
        unqualified.complete(request, **kwargs)  # pyright: ignore[reportArgumentType]
    assert all("--version" in argv for argv, _, _ in calls)
    record = CompletionQualification(profile, "offline test only", supports_tools=True)
    adapter = ClaudeCompletionAdapter(agent, parent_env=parent, qualifications=[record])
    assert adapter.complete(request, **kwargs).content == "hello"  # pyright: ignore[reportArgumentType]
    argv, env, stdin = calls[-1]
    assert stdin == "private prompt" and "private prompt" not in argv
    assert "RELAY_GATEWAY_TOKEN" not in env
    assert argv[argv.index("--tools") + 1] == ""
    assert "--resume" not in argv and "--no-session-persistence" in argv
    before = len([call for call in calls if "--version" not in call[0]])
    config = tmp_path / ".claude"
    config.mkdir()
    (config / "settings.json").write_text('{"new_policy":true}', encoding="utf-8")
    assert adapter.inspect() is None
    with pytest.raises(CompletionError, match="not qualified"):
        adapter.complete(request, **kwargs)  # pyright: ignore[reportArgumentType]
    assert len([call for call in calls if "--version" not in call[0]]) == before
    changed_env = {**parent, "CLAUDE_CONFIG_DIR": str(tmp_path / "other")}
    assert ClaudeCompletionAdapter(agent, parent_env=changed_env).profile() != profile


def test_plain_schema_arguments_are_separate_and_native_token_limit_is_never_invented() -> None:
    request = CompletionRequest("hello", "default")
    assert "--json-schema" not in ClaudeCompletionAdapter.arguments(request)
    args = ClaudeCompletionAdapter.arguments(replace(request, output_schema={"type": "object"}))
    assert args[args.index("--json-schema") + 1] == '{"type":"object"}'
    assert "--model" not in args
    assert sys.platform


def test_reviewed_record_allows_only_exact_plain_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = next(
        item
        for item in QUALIFICATIONS
        if item.profile.config_digest
        == "49d8e0ee09a8f4f1318b17eedebc6c8d2c85885f082989334416aa3d393ab2d9"
    )
    assert not record.supports_tools
    assert not hasattr(record, "model_limits")
    selected = [record.profile]
    dispatched: list[tuple[str, ...]] = []

    def profile(
        _self: ClaudeCompletionAdapter, _env: object, _cwd: Path, _timeout: float
    ) -> CompletionProfile:
        return selected[0]

    def resolve(command: str, args: tuple[str, ...], **_: Any) -> ResolvedCommand:
        dispatched.append(args)
        return ResolvedCommand((command, *args))

    def run(*_args: Any, **_kwargs: Any) -> ProcessResult:
        return response()

    monkeypatch.setattr(ClaudeCompletionAdapter, "_profile", profile)
    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    adapter = ClaudeCompletionAdapter(Agent(cmd="claude"), parent_env={})
    support = adapter.inspect()
    assert support is not None
    assert not support.supports_tools and not support.native_max_tokens
    assert (
        adapter.complete(
            CompletionRequest("hello"),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=10,
            max_output_bytes=1024,
        ).content
        == "hello"
    )
    before = len(dispatched)
    with pytest.raises(CompletionError, match="Tool output is not qualified"):
        adapter.complete(
            CompletionRequest("hello", output_schema={}),
            cwd=tmp_path,
            cancel=threading.Event(),
            timeout_s=10,
            max_output_bytes=1024,
        )
    assert len(dispatched) == before
    for changed in (
        replace(record.profile, cli_version="2.1.313"),
        replace(record.profile, platform="linux"),
        replace(record.profile, auth_mode="api-key"),
        replace(record.profile, config_digest="other"),
    ):
        selected[0] = changed
        assert adapter.inspect() is None
        with pytest.raises(CompletionError, match="not qualified"):
            adapter.complete(
                CompletionRequest("hello"),
                cwd=tmp_path,
                cancel=threading.Event(),
                timeout_s=10,
                max_output_bytes=1024,
            )
    assert len(dispatched) == before


def test_profile_ignores_observed_bookkeeping_but_preserves_unknown_and_security_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def resolve(command: str, args: tuple[str, ...], **_: Any) -> ResolvedCommand:
        return ResolvedCommand((command, *args))

    def version(*_args: Any, **_kwargs: Any) -> ProcessResult:
        return ProcessResult(0, "2.1.312 (Claude Code)")

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", version)
    adapter = ClaudeCompletionAdapter(
        Agent(cmd="claude"), parent_env={"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    )
    path = tmp_path / ".claude.json"
    original: dict[str, Any] = {
        "numStartups": 10,
        "tipsHistory": {"tip": 2},
        "cachedGrowthBookFeaturesAt": 100,
        "cachedGrowthBookFeatures": {"securityFeature": True},
        "oauthAccount": {"accountUuid": "account-one"},
        "mcpServers": {"safe": {"command": "safe"}},
        "projects": {"repo": {"hasTrustDialogAccepted": False, "lastDuration": 10}},
        "pluginUsage": {"test": {"lastUsedAt": 100, "lastUsedNumStartups": 10, "usageCount": 1}},
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    before = adapter.profile()
    observed = json.loads(json.dumps(original))
    observed["numStartups"] = 11
    observed["tipsHistory"] = {"another_tip": 20}
    observed["cachedGrowthBookFeaturesAt"] = 200
    observed["projects"]["repo"]["lastDuration"] = 20
    observed["projects"]["repo"]["lastModelUsage"] = {"model": {"tokens": 100}}
    observed["pluginUsage"]["test"] = {"lastUsedAt": 200, "usageCount": 2}
    path.write_text(json.dumps(observed, indent=4), encoding="utf-8")
    assert adapter.profile() == before
    changes: list[tuple[str, Any]] = [
        ("futureSetting", {"unknown": True}),
        ("oauthAccount", {"accountUuid": "account-two"}),
        ("mcpServers", {"new": {"command": "new"}}),
        ("cachedGrowthBookFeatures", {"securityFeature": False}),
        ("projects", {"repo": {"hasTrustDialogAccepted": True, "lastDuration": 10}}),
        ("projects", {"repo": {"hasTrustDialogAccepted": False}, "new-project": {}}),
        ("pluginUsage", {"test": {"unknownFlag": True, "usageCount": 2}}),
    ]
    for key, changed in changes:
        path.write_text(json.dumps({**original, key: changed}), encoding="utf-8")
        assert adapter.profile() != before, key
    path.write_text("invalid", encoding="utf-8")
    with pytest.raises(CompletionError, match="cannot be qualified"):
        adapter.profile()


def test_cheap_parameter_refusals_spawn_nothing_and_version_probe_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    parent = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}

    def resolve(command: str, args: tuple[str, ...], **_: Any) -> ResolvedCommand:
        return ResolvedCommand((command, *args))

    def run(argv: ResolvedCommand, **_: Any) -> ProcessResult:
        calls.append(argv.argv)
        return ProcessResult(0, "2.1.312 (Claude Code)") if "--version" in argv.argv else response()

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    agent = Agent(cmd="claude")
    profile = ClaudeCompletionAdapter(agent, parent_env=parent).profile()
    adapter = ClaudeCompletionAdapter(
        agent, parent_env=parent, qualifications=[CompletionQualification(profile, "offline")]
    )
    kwargs: dict[str, Any] = {
        "cwd": tmp_path,
        "cancel": threading.Event(),
        "timeout_s": 10,
        "max_output_bytes": 1024,
    }
    calls.clear()
    with pytest.raises(CompletionError) as caught:
        adapter.complete(CompletionRequest("x", max_tokens=5), **kwargs)
    assert caught.value.param == "max_tokens"
    with pytest.raises(CompletionError, match="Tool output is not qualified"):
        adapter.complete(CompletionRequest("x", output_schema={}), **kwargs)
    assert calls == []  # neither refusal probed the CLI
    for _ in range(3):
        assert adapter.complete(CompletionRequest("x"), **kwargs).content == "hello"
    assert len([argv for argv in calls if "--version" in argv]) == 1
    # Configuration is still re-hashed per launch: a change blocks the next one immediately.
    (tmp_path / ".claude.json").write_text('{"new": true}', encoding="utf-8")
    with pytest.raises(CompletionError, match="not qualified"):
        adapter.complete(CompletionRequest("x"), **kwargs)
    # An expired probe is repeated.
    monkeypatch.setattr(module, "VERSION_CACHE_TTL_S", 0.0)
    adapter._version_cache = None  # pyright: ignore[reportPrivateUsage]
    adapter.profile()
    adapter.profile()
    assert len([argv for argv in calls if "--version" in argv]) == 3
