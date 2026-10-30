"""HTTP regressions for routing admission and validated completion failures."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from relay.adapters.completion import (
    CompletionError,
    CompletionRequest,
    CompletionResult,
    CompletionUsage,
)
from relay.gateway.app import GatewayLimits, create_app
from test_gateway_app import WaitingBackend
from test_gateway_models import FakeBackend, configuration
from test_gateway_protocol import tool_request


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-flash",
        "gemini-3.8-flash-low",
        "gemini-3.7-flash",
        "gemini-3.7-flash-low",
        "gemini-3.6-flash",
        "gemini-3.6-flash-minimal",
    ],
)
@pytest.mark.parametrize("effort", [None, "minimal", "medium"])
def test_cursor_native_reasoning_selection_survives_http_tool_repair(
    monkeypatch: pytest.MonkeyPatch, stream: bool, model: str, effort: str | None
) -> None:
    from relay.adapters import cursor_completion
    from relay.core.platform.proc import ProcessResult
    from relay.gateway.models import ModelCatalog

    calls: list[tuple[tuple[str, ...], str]] = []
    base = model.removesuffix("-low").removesuffix("-minimal")

    def models(*_: object) -> tuple[str, ...]:
        return tuple(
            f"gemini-{version}-flash-{value}"
            for version in ("3.8", "3.7", "3.6")
            for value in ("low", "medium", "high")
        ) + ("gemini-3.6-flash-minimal",)

    def command(cmd: str, args: tuple[str, ...], **_: Any) -> tuple[str, ...]:
        return (cmd, *args)

    def run(argv: tuple[str, ...], **kwargs: Any) -> ProcessResult:
        calls.append((argv, kwargs["stdin"]))
        content = (
            "invalid envelope"
            if len(calls) == 1
            else json.dumps(
                {"content": None, "tool_calls": [{"name": "edit", "arguments": {"text": "ok"}}]}
            )
        )
        return ProcessResult(
            0,
            json.dumps(
                {"type": "result", "subtype": "success", "is_error": False, "result": content}
            ),
        )

    monkeypatch.setattr(cursor_completion, "_enumerated_models", models)
    monkeypatch.setattr(cursor_completion, "resolve_command", command)
    monkeypatch.setattr(cursor_completion, "run_process", run)
    config = configuration()
    config.agents["cursor"] = config.agents.pop("fake")
    cursor = cursor_completion.CursorCompletionAdapter(config.agents["cursor"], parent_env={})
    catalog = ModelCatalog(config, {"cursor": cursor})
    app = create_app(config, token="test-token", adapters={"cursor": cursor}, catalog=catalog)
    body = tool_request(tool_choice="required", stream=stream)
    body.update(model=f"cursor/{model}", reasoning_effort=effort)
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post("/v1/chat/completions", json=body)
    if effort == "minimal" and base != "gemini-3.6-flash":
        assert response.status_code == 400, response.text
        assert response.json()["error"]["param"] == "reasoning_effort"
        assert calls == []
        return
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    native = model if effort is None else f"{base}-{effort}"
    assert all(argv[-2:] == ("--model", native) for argv, _ in calls)
    assert json.loads(calls[0][1].splitlines()[1])["effort"] == effort
    if stream:
        chunks = [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        assert all(chunk["model"] == f"cursor/{model}" for chunk in chunks)
        assert "data: [DONE]" in response.text
    else:
        assert response.json()["model"] == f"cursor/{model}"
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


def test_auto_skips_busy_agent_but_explicit_alias_stays_pinned() -> None:
    config = configuration()
    config.agents["fake"].concurrency = 1
    config.agents["backup"] = config.agents["fake"].model_copy(deep=True)
    config.defaults.fallback = ["fake", "backup"]
    busy, backup = WaitingBackend(), FakeBackend("backup")
    app = create_app(
        config,
        token="test-token",
        adapters={"fake": busy, "backup": backup},
        limits=GatewayLimits(concurrency=2, timeout_s=5),
    )
    body = {"model": "fake/opus", "messages": [{"role": "user", "content": "hello"}]}
    with (
        TestClient(
            app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
        ) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        first = pool.submit(client.post, "/v1/chat/completions", json=body)
        try:
            assert busy.started.wait(2)
            pinned = client.post("/v1/chat/completions", json={**body, "model": "fake"})
            assert pinned.status_code == 429
            assert pinned.headers["Retry-After"] == "1"
            automatic = client.post(
                "/v1/chat/completions",
                json={**body, "model": "relay/auto", "reasoning_effort": "medium"},
            )
            assert automatic.status_code == 200, automatic.text
            assert automatic.headers["X-Relay-Resolved-Model"] == "backup/default"
            assert automatic.json()["model"] == "relay/auto"
            assert len(backup.calls) == 1
            assert backup.calls[0].effort == "medium"
        finally:
            busy.release.set()
        assert first.result(timeout=3).status_code == 200
        assert len(busy.calls) == 1


class FailingBackend(FakeBackend):
    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        self.calls.append(request)
        raise CompletionError("provider unavailable", status=503, code="model_unavailable")


@pytest.mark.parametrize("stream", [False, True])
def test_auto_provider_failure_never_retries_or_switches_backend(stream: bool) -> None:
    config = configuration()
    config.agents["backup"] = config.agents["fake"].model_copy(deep=True)
    config.defaults.fallback = ["fake", "backup"]
    failing, backup = FailingBackend(), FakeBackend("backup")
    app = create_app(config, token="test-token", adapters={"fake": failing, "backup": backup})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "relay/auto",
                "stream": stream,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert len(failing.calls) == 1
    assert backup.calls == []


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_tool_output_and_failed_repair_never_escape_as_calls(stream: bool) -> None:
    backend = FakeBackend()
    backend.results = [
        CompletionResult(
            envelope={
                "content": "unvalidated text",
                "tool_calls": [{"name": "edit", "arguments": {"text": 9}}],
            }
        )
    ]
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post("/v1/chat/completions", json=tool_request(stream=stream))
    assert response.status_code == 502
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "invalid_completion"
    assert "unvalidated text" not in response.text
    assert "[DONE]" not in response.text
    assert len(backend.calls) == 2
    assert backend.calls[0].model == backend.calls[1].model


@pytest.mark.parametrize("stream", [False, True])
def test_repair_with_unknown_attempt_usage_does_not_report_partial_counts(stream: bool) -> None:
    backend = FakeBackend()
    backend.results = [
        CompletionResult(envelope={"content": None, "tool_calls": []}),
        CompletionResult(
            envelope={
                "content": None,
                "tool_calls": [{"name": "edit", "arguments": {"text": "ok"}}],
            },
            usage=CompletionUsage(11, 7),
        ),
    ]
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    body = tool_request(tool_choice="required", stream=stream)
    if stream:
        body["stream_options"] = {"include_usage": True}
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200, response.text
    assert len(backend.calls) == 2
    if stream:
        frames = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        assert frames[-1] == "[DONE]"
        chunks = [json.loads(frame) for frame in frames[:-1]]
        assert all(chunk["choices"] for chunk in chunks)
        assert all(chunk.get("usage") is None for chunk in chunks)
    else:
        assert response.json()["usage"] is None
        assert response.json()["choices"][0]["finish_reason"] == "tool_calls"


@pytest.mark.parametrize("stream", [False, True])
def test_schema_instance_cross_product_cannot_escape_validation_budget(stream: bool) -> None:
    # Each input is small; applying every branch to every value is the expensive part.
    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"allOf": [{"type": "integer"} for _ in range(64)]},
            }
        },
    }
    backend = FakeBackend()
    backend.results = [
        CompletionResult(
            envelope={
                "content": "budget-unvalidated-text",
                "tool_calls": [{"name": "edit", "arguments": {"values": list(range(10000))}}],
            }
        )
    ]
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    body = tool_request(
        tools=[{"type": "function", "function": {"name": "edit", "parameters": schema}}],
        stream=stream,
    )
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post("/v1/chat/completions", json=body)
        assert client.get("/healthz").json() == {"status": "ok"}
    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "invalid_completion"
    assert "budget-unvalidated-text" not in response.text
    assert len(backend.calls) == 2


def test_repeated_enum_references_are_bounded_before_dispatch() -> None:
    schema = {
        "type": "object",
        "$defs": {"values": {"enum": list(range(4000))}},
        "properties": {"x": {"allOf": [{"$ref": "#/$defs/values"} for _ in range(1000)]}},
    }
    backend = FakeBackend()
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    body = tool_request(
        tools=[{"type": "function", "function": {"name": "edit", "parameters": schema}}]
    )
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["param"] == "tools"
    assert backend.calls == []
