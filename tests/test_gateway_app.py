from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolParam

from relay.adapters.completion import (
    CompletionError,
    CompletionRequest,
    CompletionResult,
    CompletionUsage,
)
from relay.gateway import app as gateway_app
from relay.gateway.app import GatewayLimits, create_app
from relay.gateway.runtime import GatewayRuntime
from test_gateway_models import FakeBackend, configuration


@pytest.fixture
def client_backend() -> Iterator[tuple[TestClient, FakeBackend]]:
    backend = FakeBackend()
    app = create_app(configuration(), token="test-token", adapters={"fake": backend})
    with TestClient(
        app, base_url="http://localhost", headers={"Authorization": "Bearer test-token"}
    ) as client:
        yield client, backend


def test_auth_every_v1_path_and_health(client_backend: tuple[TestClient, FakeBackend]) -> None:
    client, backend = client_backend
    for path in ("/v1/models", "/v1/models/fake/opus", "/v1/unknown"):
        assert client.get(path, headers={"Authorization": "wrong"}).status_code == 401
    assert client.get("/healthz", headers={"Authorization": "wrong"}).json() == {"status": "ok"}
    assert client.get("/v1/unknown").json()["error"]["param"] is None
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/v1/unknown").json()["error"]["message"] == "route not found"
    wrong_method = client.get("/v1/chat/completions")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["message"] == "method not allowed"
    assert wrong_method.headers["Allow"] == "POST"
    assert backend.calls == []


@pytest.mark.parametrize("stream", [False, True])
def test_invalid_unicode_plain_output_is_502_before_headers(
    client_backend: tuple[TestClient, FakeBackend], stream: bool
) -> None:
    client, backend = client_backend
    backend.results = [CompletionResult(content="\ud800")]
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": stream,
        },
    )
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "completion contains invalid Unicode"


def test_host_origin_and_slash_retrieval(client_backend: tuple[TestClient, FakeBackend]) -> None:
    client, backend = client_backend
    assert client.get("/v1/models/fake/opus").json()["id"] == "fake/opus"
    assert client.get("/v1/models", headers={"Host": "attacker.example"}).status_code == 400
    assert (
        client.get("/v1/models", headers={"Origin": "https://attacker.example"}).status_code == 403
    )
    assert client.get("/v1/models", headers={"Origin": "http://localhost:3000"}).status_code == 200
    assert backend.calls == []


def test_plain_response_and_sse_identical(client_backend: tuple[TestClient, FakeBackend]) -> None:
    client, backend = client_backend
    body = {"model": "fake/opus", "messages": [{"role": "user", "content": "hello"}]}
    response = client.post("/v1/chat/completions", json={**body, "temperature": 0.2})
    assert response.status_code == 200
    assert response.headers["X-Relay-Ignored-Parameters"] == "temperature"
    assert response.headers["X-Relay-Resolved-Model"] == "fake/opus"
    assert response.json()["usage"] is None
    streamed = client.post("/v1/chat/completions", json={**body, "stream": True})
    frames = [line[6:] for line in streamed.text.splitlines() if line.startswith("data: ")]
    chunks = [json.loads(frame) for frame in frames[:-1]]
    content = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    assert content == response.json()["choices"][0]["message"]["content"]
    assert frames[-1] == "[DONE]"
    assert len(backend.calls) == 2


def test_validation_happens_before_dispatch(client_backend: tuple[TestClient, FakeBackend]) -> None:
    client, backend = client_backend
    body = {"model": "fake", "messages": [{"role": "user", "content": "hello"}]}
    response = client.post("/v1/chat/completions", json={**body, "n": 2})
    assert response.status_code == 400 and response.json()["error"]["param"] == "n"
    assert client.post("/v1/chat/completions", content="{").status_code == 400
    assert backend.calls == []


def test_unknown_request_fields_are_ignored(client_backend: tuple[TestClient, FakeBackend]) -> None:
    client, backend = client_backend
    body = {
        "model": "fake",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"k": "v"},
        "stop": ["x"],
        "user": "u1",
    }
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert len(backend.calls) == 1


def test_envelope_repair_usage_aggregation(client_backend: tuple[TestClient, FakeBackend]) -> None:
    client, backend = client_backend
    backend.results = [
        CompletionResult(
            envelope={"content": None, "tool_calls": [{"name": "bad", "arguments": {}}]},
            usage=CompletionUsage(10, 2),
        ),
        CompletionResult(
            envelope={"content": None, "tool_calls": [{"name": "edit", "arguments": {"x": "ok"}}]},
            usage=CompletionUsage(20, 3),
        ),
    ]
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "fake",
            "messages": [{"role": "user", "content": "edit"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "edit",
                        "parameters": {
                            "type": "object",
                            "properties": {"x": {"type": "string"}},
                            "required": ["x"],
                        },
                    },
                }
            ],
            "tool_choice": "required",
        },
    )
    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    assert message["tool_calls"][0]["function"]["arguments"] == '{"x":"ok"}'
    assert response.json()["usage"]["total_tokens"] == 35
    assert len(backend.calls) == 2
    assert backend.calls[0].model == backend.calls[1].model


def test_bounded_chunked_body() -> None:
    backend = FakeBackend()
    app = create_app(
        configuration(),
        token="test-token",
        adapters={"fake": backend},
        limits=GatewayLimits(max_request_bytes=32),
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/v1/chat/completions",
            content=iter([b"a" * 20, b"b" * 20]),
            headers={"Authorization": "Bearer test-token"},
        )
    assert response.status_code == 413
    assert backend.calls == []


class WaitingBackend(FakeBackend):
    def __init__(self, *, failure: bool = False) -> None:
        super().__init__()
        self.started, self.finished = threading.Event(), threading.Event()
        self.release = threading.Event()
        self.failure = failure
        self.scratch: Path | None = None

    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        self.started.set()
        self.scratch = cwd
        try:
            while not self.release.wait(0.01):
                if cancel.is_set():
                    raise CompletionError("cancelled", status=503)
            if self.failure:
                raise CompletionError("provider failed")
            return super().complete(
                request,
                cwd=cwd,
                cancel=cancel,
                timeout_s=timeout_s,
                max_output_bytes=max_output_bytes,
            )
        finally:
            self.finished.set()


@contextmanager
def local_server(
    backend: FakeBackend, *, limits: GatewayLimits | None = None
) -> Generator[tuple[int, GatewayRuntime]]:
    limits = limits or GatewayLimits(timeout_s=3)
    runtime = GatewayRuntime(limits, {"fake": 1})
    app = create_app(
        configuration(),
        token="test-token",
        adapters={"fake": backend},
        limits=limits,
        runtime=runtime,
    )
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        try:
            yield port, runtime
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def raw_completion(port: int, *, stream: bool = False) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port), timeout=3)
    body = json.dumps(
        {"model": "fake", "stream": stream, "messages": [{"role": "user", "content": "hello"}]}
    ).encode()
    connection.sendall(
        (
            f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Authorization: Bearer test-token\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode()
        + body
    )
    return connection


@pytest.mark.parametrize("stream", [False, True])
def test_real_http_disconnect_drains(stream: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_app, "KEEPALIVE_INTERVAL", 0.02)
    backend = WaitingBackend()
    with local_server(backend) as (port, runtime):
        connection = raw_completion(port, stream=stream)
        assert backend.started.wait(2)
        if stream:
            assert b"200 OK" in connection.recv(4096)
        connection.close()
        assert backend.finished.wait(2)
        deadline = time.monotonic() + 2
        while runtime.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert runtime.active == 0
        assert backend.scratch is not None and not backend.scratch.exists()


def test_real_http_post_keepalive_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_app, "KEEPALIVE_INTERVAL", 0.02)
    backend = WaitingBackend(failure=True)
    with local_server(backend) as (port, _runtime):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {"model": "fake", "stream": True, "messages": [{"role": "user", "content": "x"}]}
            ),
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.readline() == b": keepalive\n"
        backend.release.set()
        remainder = response.read()
        assert b'"error"' in remainder and b"[DONE]" not in remainder
        assert b'"finish_reason"' not in remainder
        connection.close()


def test_real_http_chunked_body_limit() -> None:
    backend = FakeBackend()
    with local_server(backend, limits=GatewayLimits(max_request_bytes=32)) as (port, _runtime):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=iter([b"a" * 20, b"b" * 20]),
            encode_chunked=True,
            headers={"Authorization": "Bearer test-token"},
        )
        response = connection.getresponse()
        assert response.status == 413
        response.read()
        connection.close()
        assert backend.calls == []


@pytest.mark.parametrize("stream", [False, True])
def test_slow_http_consumer_retains_admission_until_deadline(stream: bool) -> None:
    backend = FakeBackend()
    backend.results = [CompletionResult(content="x" * (7 * 1024 * 1024))]
    with local_server(backend, limits=GatewayLimits(concurrency=1, timeout_s=0.8)) as (
        port,
        runtime,
    ):
        connection = raw_completion(port, stream=stream)
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        deadline = time.monotonic() + 0.5
        while not backend.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.calls
        time.sleep(0.05)
        assert runtime.active == 1
        other = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        other.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps({"model": "fake", "messages": [{"role": "user", "content": "x"}]}),
            headers={"Authorization": "Bearer test-token"},
        )
        response = other.getresponse()
        assert response.status == 429
        assert response.getheader("Retry-After") == "1"
        response.read()
        other.close()
        deadline = time.monotonic() + 3
        while runtime.active and time.monotonic() < deadline:
            time.sleep(0.01)
        connection.close()
        assert runtime.active == 0


def test_pinned_openai_sdk_real_http_tools_roundtrip_and_stream() -> None:
    backend = FakeBackend()
    backend.results = [
        CompletionResult(
            envelope={
                "content": None,
                "tool_calls": [{"name": "edit", "arguments": {"text": "hello\n雪"}}],
            }
        ),
        CompletionResult(envelope={"content": "finished", "tool_calls": []}),
        CompletionResult(content="stream\n雪", usage=CompletionUsage(3, 4)),
    ]
    tools: list[ChatCompletionToolParam] = [
        {
            "type": "function",
            "function": {
                "name": "edit",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
        }
    ]
    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": "edit"}]
    with (
        local_server(backend) as (port, _runtime),
        OpenAI(api_key="test-token", max_retries=0, base_url=f"http://127.0.0.1:{port}/v1") as sdk,
    ):
        first = sdk.chat.completions.create(model="fake/opus", messages=messages, tools=tools)
        calls = first.choices[0].message.tool_calls
        assert calls and calls[0].type == "function"
        call = calls[0]
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call.id, "content": "edited"},
            ]
        )
        second = sdk.chat.completions.create(model="fake/opus", messages=messages, tools=tools)
        assert second.choices[0].message.content == "finished"
        assert call.id in backend.calls[1].prompt
        chunks = list(
            sdk.chat.completions.create(
                model="fake/opus",
                messages=[{"role": "user", "content": "hello"}],
                stream=True,
                stream_options={"include_usage": True},
            )
        )
        assert (
            "".join(chunk.choices[0].delta.content or "" for chunk in chunks if chunk.choices)
            == "stream\n雪"
        )
        assert chunks[-1].choices == [] and chunks[-1].usage is not None
        assert chunks[-1].usage.total_tokens == 7
