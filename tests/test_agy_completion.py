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
from relay.adapters.completion import (
    CompletionCapable,
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionUsage,
    ObservedModel,
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
    assert COMPLETION_FLAGS == ("--mode", "plan", "--sandbox")
    for flag in COMPLETION_FLAGS:
        assert flag in args
    assert "--model" not in args

    req_model = CompletionRequest("test prompt", "gemini-2.5-pro", effort="high")
    args_model = AgyCompletionAdapter.arguments(req_model)
    assert "--model" in args_model
    assert args_model[args_model.index("--model") + 1] == "gemini-2.5-pro"
    assert "--effort" in args_model
    assert args_model[args_model.index("--effort") + 1] == "high"

    req_schema = CompletionRequest("test prompt", "default", output_schema={"type": "object"})
    args_schema = AgyCompletionAdapter.arguments(req_schema)
    assert "--json-schema" in args_schema
    assert args_schema[args_schema.index("--json-schema") + 1] == '{"type":"object"}'


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


SLUGS = ("gemini-4-argon-medium", "claude-sonnet-5-5-high")


def test_default_adapters_and_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy_completion, "_enumerated_models", lambda *_, **__: SLUGS)
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
    assert set(catalog.models) == {"agy", *(f"agy/{slug}" for slug in ("default", *SLUGS))}

    # With explicit models override
    catalog_explicit = ModelCatalog(cfg, adapters, models=["m1", "m2"])
    assert catalog_explicit.agent_aliases["agy"] == ("m1", "m2")
    assert set(catalog_explicit.models) == {"agy", "agy/m1", "agy/m2"}

    # Enumeration failure falls back to the configured models
    monkeypatch.setattr(agy_completion, "_enumerated_models", lambda *_, **__: ())
    fallback = default_adapters(cfg)["agy"]
    assert isinstance(fallback, AgyCompletionAdapter)
    assert fallback.default_aliases == ("default",)


def test_agy_catalog_lists_enumerated_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy_completion, "_enumerated_models", lambda *_, **__: SLUGS)
    cfg = Config.model_validate({"version": 1, "agents": {"agy": {"enabled": True, "cmd": "agy"}}})
    catalog = ModelCatalog(cfg, default_adapters(cfg))
    assert set(catalog.models) == {"agy", "agy/default", *(f"agy/{slug}" for slug in SLUGS)}
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
