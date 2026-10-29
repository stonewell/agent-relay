"""Model discovery from CLI-reported usage: parsing, fail-soft probes, cache and laziness."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from relay.adapters import claude_completion as module
from relay.adapters.claude_completion import ClaudeCompletionAdapter, parse_completion
from relay.adapters.completion import (
    DEFAULT_MODEL_ALIASES,
    CompletionError,
    CompletionProfile,
    CompletionQualification,
    CompletionRequest,
    CompletionResult,
    CompletionTerminationError,
    ObservedModel,
)
from relay.core.platform.proc import ProcessResult, ResolvedCommand
from relay.core.registry import Agent
from relay.gateway import discovery
from relay.gateway.app import create_app
from relay.gateway.discovery import PROBE_PROMPT, ModelCache, discover, probe
from relay.gateway.models import ModelCatalog
from relay.gateway.runtime import GatewayLimits, GatewayRuntime
from test_gateway_models import OBSERVED, FakeBackend, configuration

RECORDED = Path("tests/fixtures/recorded/claude_plain.json").read_text(encoding="utf-8")
PROFILE = CompletionProfile("claude", "2.1.312", "win32", "subscription", "digest-a")


def recorded(**changes: Any) -> ProcessResult:
    payload = json.loads(RECORDED)
    payload.update(changes)
    return ProcessResult(0, json.dumps(payload))


def test_recorded_model_usage_yields_resolved_model_and_limits() -> None:
    result = parse_completion(ProcessResult(0, RECORDED), structured=False)
    assert result.observed == ObservedModel("claude-sonnet-5-5", 1000000, 128000)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        "claude-sonnet-5-5",
        {"a": {"contextWindow": 1, "maxOutputTokens": 1}, "b": {"contextWindow": 1}},
        {"m": {"contextWindow": True, "maxOutputTokens": 1}},
        {"m": {"contextWindow": 0, "maxOutputTokens": 1}},
        {"m": {"contextWindow": 10, "maxOutputTokens": "1"}},
        {"m": {"contextWindow": 10}},
        {"m": [1, 2]},
        {" ": {"contextWindow": 10, "maxOutputTokens": 1}},
    ],
)
def test_ambiguous_or_incomplete_model_usage_is_unknown(usage: object) -> None:
    result = parse_completion(recorded(modelUsage=usage), structured=False)
    assert result.content == "OK" and result.observed is None


def test_probe_uses_qualified_complete_path_in_fresh_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launches: list[dict[str, Any]] = []

    def resolve(command: str, args: tuple[str, ...], **_: Any) -> ResolvedCommand:
        return ResolvedCommand((command, *args))

    def run(argv: ResolvedCommand, **kwargs: Any) -> ProcessResult:
        if "--version" in argv.argv:
            return ProcessResult(0, "2.1.312 (Claude Code)")
        launches.append({"argv": list(argv.argv), **kwargs})
        assert Path(kwargs["cwd"]).is_dir() and not list(Path(kwargs["cwd"]).iterdir())
        return ProcessResult(0, RECORDED)

    monkeypatch.setattr(module, "resolve_command", resolve)
    monkeypatch.setattr(module, "run_process", run)
    parent = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "RELAY_GATEWAY_TOKEN": "x"}
    agent = Agent(cmd="claude")
    unqualified = ClaudeCompletionAdapter(agent, parent_env=parent)
    cancel = threading.Event()
    # Qualification gates probes too: an unreviewed profile launches no model call.
    assert probe(unqualified, "sonnet", cancel=cancel) is None
    assert launches == []
    record = CompletionQualification(unqualified.profile(), "offline test only")
    adapter = ClaudeCompletionAdapter(agent, parent_env=parent, qualifications=[record])
    assert probe(adapter, "sonnet", cancel=cancel, timeout_s=7) == ObservedModel(
        "claude-sonnet-5-5", 1000000, 128000
    )
    assert probe(adapter, "default", cancel=cancel) is not None
    first, second = launches
    assert first["argv"][first["argv"].index("--model") + 1] == "sonnet"
    assert "--model" not in second["argv"]
    assert first["stdin"] == PROBE_PROMPT and 0 < first["timeout_s"] <= 7
    assert first["argv"][first["argv"].index("--tools") + 1] == ""
    assert "RELAY_GATEWAY_TOKEN" not in first["env"]
    assert Path(first["cwd"]) != tmp_path and not Path(first["cwd"]).exists()


class ScriptedBackend(FakeBackend):
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        super().__init__("claude")
        self.failures = failures or {}

    def complete(self, request: CompletionRequest, **kwargs: Any) -> CompletionResult:
        if request.model in self.failures:
            self.probes.append(request.model)
            raise self.failures[request.model]
        return super().complete(request, **kwargs)


def test_failed_probes_are_omitted_and_not_cached(tmp_path: Path) -> None:
    cache = ModelCache(tmp_path / "models.json")
    backend = ScriptedBackend({"sonnet": CompletionError("provider failed")})
    result = discover(backend, PROFILE, DEFAULT_MODEL_ALIASES, cache, probe_missing=True)
    assert result.observed == OBSERVED
    assert result.failed == ["sonnet", "fable"]
    assert cache.load(PROFILE) == OBSERVED
    again = ScriptedBackend()
    result = discover(again, PROFILE, DEFAULT_MODEL_ALIASES, cache, probe_missing=True)
    # Cache hits are not probed again; uncached failures are retried by a new caller.
    assert again.probes == ["sonnet", "fable"]
    result = discover(again, PROFILE, DEFAULT_MODEL_ALIASES, cache, probe_missing=False)
    assert result.undiscovered == ["sonnet", "fable"] and again.probes == ["sonnet", "fable"]


def test_termination_uncertainty_propagates_after_saving_earlier_observations(
    tmp_path: Path,
) -> None:
    cache = ModelCache(tmp_path / "models.json")
    backend = ScriptedBackend({"opus": CompletionTerminationError()})
    with pytest.raises(CompletionTerminationError):
        discover(backend, PROFILE, DEFAULT_MODEL_ALIASES, cache, probe_missing=True)
    assert backend.probes == ["default", "sonnet", "opus"]
    assert cache.load(PROFILE) == {"default": OBSERVED["default"]}


def test_refresh_reprobes_and_drops_failed_aliases(tmp_path: Path) -> None:
    cache = ModelCache(tmp_path / "models.json")
    discover(FakeBackend(), PROFILE, DEFAULT_MODEL_ALIASES, cache, probe_missing=True)
    backend = ScriptedBackend({"opus": CompletionError("gone")})
    result = discover(
        backend, PROFILE, ["default", "opus"], cache, probe_missing=True, refresh=True
    )
    assert backend.probes == ["default", "opus"] and result.failed == ["opus"]
    assert cache.load(PROFILE) == {"default": OBSERVED["default"]}


def test_cache_is_keyed_by_profile_and_replaces_stale_agent_entries(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "models.json"
    cache = ModelCache(path)
    other = CompletionProfile("codex", "1", "win32", "subscription", "x")
    assert cache.store(PROFILE, OBSERVED) and cache.store(other, {"o": OBSERVED["opus"]})
    assert cache.load(PROFILE) == OBSERVED
    upgraded = replace(PROFILE, cli_version="2.1.313")
    reconfigured = replace(PROFILE, config_digest="digest-b")
    assert cache.load(upgraded) == {} and cache.load(reconfigured) == {}
    assert cache.store(upgraded, {"opus": OBSERVED["opus"]})
    assert cache.load(PROFILE) == {}
    assert cache.load(upgraded) == {"opus": OBSERVED["opus"]}
    assert cache.load(other) == {"o": OBSERVED["opus"]}
    # Atomic replacement leaves no temporary files behind.
    assert [item.name for item in path.parent.iterdir()] == ["models.json"]
    assert ModelCache(None).load(PROFILE) == {} and ModelCache(None).store(PROFILE, OBSERVED)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'{"schema": 2, "profiles": []}',
        b'{"schema": 1, "profiles": {}}',
        b'{"schema": 1, "profiles": [1, "x"]}',
    ],
)
def test_corrupt_or_foreign_cache_reads_as_miss_and_is_rewritten(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "models.json"
    path.write_bytes(content)
    cache = ModelCache(path)
    assert cache.load(PROFILE) == {}
    assert cache.store(PROFILE, OBSERVED)
    assert cache.load(PROFILE) == OBSERVED


def test_malformed_entries_and_oversized_cache_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models.json"
    cache = ModelCache(path)
    cache.store(PROFILE, OBSERVED)
    document = json.loads(path.read_text(encoding="utf-8"))
    models = document["profiles"][0]["models"]
    models["opus"]["context_length"] = None
    models["bad"] = {"model": "", "context_length": 1, "max_output_tokens": 1}
    models["flag"] = {"model": "m", "context_length": True, "max_output_tokens": 1}
    path.write_text(json.dumps(document), encoding="utf-8")
    assert cache.load(PROFILE) == {"default": OBSERVED["default"]}
    monkeypatch.setattr(discovery, "CACHE_LIMIT", 10)
    assert cache.load(PROFILE) == {}


def test_unwritable_cache_is_reported_without_losing_observations(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("x", encoding="utf-8")
    cache = ModelCache(blocked / "models.json")
    result = discover(FakeBackend(), PROFILE, ["opus"], cache, probe_missing=True)
    assert not result.stored and result.observed == {"opus": OBSERVED["opus"]}


class BlockingProbeBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.probing, self.release = threading.Event(), threading.Event()

    def complete(self, request: CompletionRequest, **kwargs: Any) -> CompletionResult:
        if request.prompt == PROBE_PROMPT:
            self.probing.set()
            assert self.release.wait(5)
        return super().complete(request, **kwargs)


def client(app: Any) -> TestClient:
    return TestClient(app, base_url="http://localhost", headers={"Authorization": "Bearer t"})


def test_discovery_is_lazy_and_single_flight(tmp_path: Path) -> None:
    backend = BlockingProbeBackend()
    cache = ModelCache(tmp_path / "models.json")
    app = create_app(configuration(), token="t", adapters={"fake": backend}, model_cache=cache)
    with client(app) as http, ThreadPoolExecutor(max_workers=3) as pool:
        assert http.get("/healthz").status_code == 200
        assert http.get("/v1/models/fake/unknown").status_code == 404
        reply = http.post(
            "/v1/chat/completions",
            json={"model": "fake/sonnet", "messages": [{"role": "user", "content": "x"}]},
        )
        assert reply.status_code == 200
        assert reply.headers["X-Relay-Resolved-Model"] == "fake/sonnet"
        # Startup, health, unknown ids and completions never probe.
        assert backend.probes == [] and not backend.probing.is_set()
        assert not cache.path or not cache.path.exists()
        requests = [pool.submit(http.get, "/v1/models") for _ in range(3)]
        assert backend.probing.wait(5)
        time.sleep(0.3)  # the other requests arrive while the first probe is in flight
        backend.release.set()
        listings = [request.result(timeout=10).json() for request in requests]
        assert all(listing == listings[0] for listing in listings)
        assert backend.probes == list(DEFAULT_MODEL_ALIASES)
        ids = {entry["id"] for entry in listings[0]["data"]}
        assert ids == {"fake", "fake/default", "fake/opus", "relay/auto"}
        assert http.get("/v1/models/fake/opus").json()["relay"]["context_length"] == 100000
        assert http.get("/v1/models/fake/sonnet").status_code == 404
        assert backend.probes == list(DEFAULT_MODEL_ALIASES)
    assert cache.load(backend.support.profile if backend.support else PROFILE) == OBSERVED


def test_gateway_reads_cache_and_probes_only_missing_aliases(tmp_path: Path) -> None:
    backend = FakeBackend()
    assert backend.support is not None
    cache = ModelCache(tmp_path / "models.json")
    cache.store(backend.support.profile, {"default": ObservedModel("cached", 5, 4)})
    app = create_app(
        configuration(),
        token="t",
        adapters={"fake": backend},
        models=["default", "opus"],
        model_cache=cache,
    )
    with client(app) as http:
        listing = {entry["id"]: entry["relay"] for entry in http.get("/v1/models").json()["data"]}
    assert backend.probes == ["opus"]
    assert listing["fake/default"]["resolved_model"] == "fake/cached"
    assert listing["fake/opus"]["max_output_tokens"] == 10000


def test_probe_termination_uncertainty_quarantines_gateway() -> None:
    backend = ScriptedBackend({"default": CompletionTerminationError()})
    config = configuration()
    config.agents["claude"] = config.agents.pop("fake")
    app = create_app(config, token="t", adapters={"claude": backend})
    with client(app) as http:
        response = http.get("/v1/models")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "termination_unknown"
        assert not app.state.runtime.healthy
        # A quarantined gateway admits no work and launches no further probes.
        assert http.get("/v1/models").json() == {"object": "list", "data": []}
        chat = http.post(
            "/v1/chat/completions",
            json={"model": "claude", "messages": [{"role": "user", "content": "x"}]},
        )
        assert chat.status_code == 503
    assert backend.probes == ["default"]


@pytest.mark.parametrize("global_limit,agent_limit", [(1, 2), (2, 1), (1, 1)])
def test_discovery_waits_for_chat_admission_without_losing_alias(
    global_limit: int,
    agent_limit: int,
) -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        runtime = GatewayRuntime(GatewayLimits(concurrency=global_limit), {"fake": agent_limit})
        catalog = ModelCatalog(configuration(), {"fake": backend}, models=["default"])
        chat = runtime.start(backend, CompletionRequest("chat", "default"))
        await chat.result  # the HTTP response still owns the permit after the backend finishes
        listing = asyncio.create_task(catalog.discover(runtime))
        try:
            await asyncio.sleep(0.15)
            assert backend.probes == [] and not listing.done()
            assert catalog.state("fake/default") == "undiscovered"
            await chat.drain()
            await asyncio.wait_for(listing, timeout=3)
            assert backend.probes == ["default"]
            assert catalog.state("fake/default") == "observed"
            assert runtime.active == 0
        finally:
            await catalog.close()
            await runtime.shutdown()

    asyncio.run(scenario())


def test_models_http_request_waits_for_active_chat() -> None:
    class BlockingChatBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.started, self.release = threading.Event(), threading.Event()

        def complete(self, request: CompletionRequest, **kwargs: Any) -> CompletionResult:
            if request.prompt != PROBE_PROMPT:
                self.started.set()
                assert self.release.wait(3)
            return super().complete(request, **kwargs)

    backend = BlockingChatBackend()
    config = configuration()
    config.agents["fake"].concurrency = 1
    app = create_app(
        config,
        token="t",
        adapters={"fake": backend},
        models=["default"],
        limits=GatewayLimits(concurrency=1),
    )
    with client(app) as http, ThreadPoolExecutor(max_workers=2) as pool:
        chat = pool.submit(
            http.post,
            "/v1/chat/completions",
            json={"model": "fake", "messages": [{"role": "user", "content": "x"}]},
        )
        try:
            assert backend.started.wait(3)
            listing = pool.submit(http.get, "/v1/models")
            time.sleep(0.15)
            assert backend.probes == [] and not listing.done()
            backend.release.set()
            assert chat.result(timeout=3).status_code == 200
            response = listing.result(timeout=3)
            assert response.status_code == 200
            assert "fake/default" in {entry["id"] for entry in response.json()["data"]}
            assert backend.probes == ["default"]
        finally:
            backend.release.set()


@pytest.mark.parametrize("stop", ["catalog", "runtime"])
def test_shutdown_aborts_discovery_waiting_for_capacity_without_alias_failure(stop: str) -> None:
    async def scenario() -> None:
        backend = FakeBackend()
        runtime = GatewayRuntime(GatewayLimits(concurrency=1), {"fake": 1})
        catalog = ModelCatalog(configuration(), {"fake": backend}, models=["default"])
        chat = runtime.start(backend, CompletionRequest("chat"))
        await chat.result
        listing = asyncio.create_task(catalog.discover(runtime))
        await asyncio.sleep(0.15)
        assert not listing.done()
        try:
            await asyncio.wait_for(
                catalog.close() if stop == "catalog" else runtime.shutdown(), timeout=3
            )
            with pytest.raises(CompletionError):
                await listing
            assert backend.probes == []
            assert catalog.state("fake/default") == "undiscovered"
        finally:
            await catalog.close()
            await runtime.shutdown()

    asyncio.run(scenario())


def test_cancelled_discovery_caller_keeps_shared_probe_but_close_drains_it() -> None:
    class CancellableBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.started, self.stopped = threading.Event(), threading.Event()
            self.paths: list[Path] = []

        def complete(self, request: CompletionRequest, **kwargs: Any) -> CompletionResult:
            self.paths.append(kwargs["cwd"])
            self.started.set()
            assert kwargs["cancel"].wait(3)
            self.stopped.set()
            raise CompletionError("cancelled", status=503, code="request_cancelled")

    async def scenario() -> None:
        backend = CancellableBackend()
        runtime = GatewayRuntime(GatewayLimits(concurrency=1), {"fake": 1})
        catalog = ModelCatalog(configuration(), {"fake": backend}, models=["default"])
        listing = asyncio.create_task(catalog.discover(runtime))
        try:
            assert await asyncio.to_thread(backend.started.wait, 3)
            listing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await listing
            assert runtime.active == 1 and not backend.stopped.is_set()
            with pytest.raises(CompletionError) as caught:
                runtime.start(backend, CompletionRequest("chat"))
            assert caught.value.status == 429
            await asyncio.wait_for(catalog.close(), timeout=3)
            assert backend.stopped.is_set() and runtime.active == 0
            assert all(not path.exists() for path in backend.paths)
            assert catalog.state("fake/default") == "undiscovered"
        finally:
            await catalog.close()
            await runtime.shutdown()

    asyncio.run(scenario())
