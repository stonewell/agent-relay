from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from relay.adapters.completion import (
    DEFAULT_MODEL_ALIASES,
    CompletionProfile,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    ObservedModel,
    model_aliases,
)
from relay.core.registry import Config
from relay.gateway.discovery import PROBE_PROMPT
from relay.gateway.models import ModelCatalog
from relay.gateway.protocol import GatewayError, parse_request

OBSERVED = {
    "default": ObservedModel("m-default", 200000, 32000),
    "opus": ObservedModel("m-opus", 100000, 10000),
}


class FakeBackend:
    """Completion fake; discovery probes are answered from `limits` and kept out of `calls`."""

    def __init__(self, name: str = "fake", *, tools: bool = True) -> None:
        self.id = name
        self.support: CompletionSupport = CompletionSupport(
            CompletionProfile(name, "test", "test", "test", "test"), supports_tools=tools
        )
        self.calls: list[CompletionRequest] = []
        self.probes: list[str] = []
        self.limits: dict[str, ObservedModel] = dict(OBSERVED)
        self.results = [CompletionResult(content="hello\n雪", raw="hello\n雪")]

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport:
        return self.support

    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        assert cwd.is_dir()
        if request.prompt == PROBE_PROMPT:
            self.probes.append(request.model)
            return CompletionResult(content="OK", observed=self.limits.get(request.model))
        self.calls.append(request)
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def configuration() -> Config:
    return Config.model_validate(
        {
            "version": 1,
            "agents": {
                "fake": {
                    "cmd": "never-called",
                    "models": {"deep": "m/deep", "fast": "m/fast"},
                    "model_routing": {"default": {"model": "deep", "effort": "high"}},
                }
            },
        }
    )


def observed_catalog(config: Config, adapters: dict[str, FakeBackend]) -> ModelCatalog:
    catalog = ModelCatalog(config, adapters)
    catalog.observe(probe=True)
    return catalog


def test_alias_ids_bare_default_and_pinning() -> None:
    backend = FakeBackend()
    catalog = ModelCatalog(configuration(), {"fake": backend})
    assert set(catalog.models) == {"fake", *(f"fake/{alias}" for alias in DEFAULT_MODEL_ALIASES)}
    request = parse_request(
        {"model": "fake/sonnet", "messages": [{"role": "user", "content": "x"}]}
    )
    # Routing needs no discovery and does not probe; registry tiers are not gateway models.
    assert catalog.candidates(request)[0].model == "sonnet"
    request.model = "fake"
    assert catalog.candidates(request)[0].model == "default"
    assert catalog.candidates(request)[0].effort is None
    request.model = "fake/deep"
    with pytest.raises(GatewayError, match="unknown model"):
        catalog.candidates(request)
    with pytest.raises(GatewayError, match="unknown model"):
        catalog.public("nope")
    assert backend.probes == [] and backend.calls == []


def test_listing_includes_configured_aliases_with_null_limits() -> None:
    backend = FakeBackend()
    catalog = ModelCatalog(configuration(), {"fake": backend})
    # Named aliases are listed before discovery; routing shortcuts stay internal.
    before = {entry["id"]: entry["relay"] for entry in catalog.listing()["data"]}
    assert set(before) == {
        *(f"fake/{alias}" for alias in DEFAULT_MODEL_ALIASES if alias != "default"),
    }
    assert all(value["context_length"] is None for value in before.values())
    assert catalog.state("fake/opus") == "undiscovered"
    catalog.observe(probe=True)
    assert backend.probes == list(DEFAULT_MODEL_ALIASES)
    listing = {entry["id"]: entry["relay"] for entry in catalog.listing()["data"]}
    assert set(listing) == {
        *(f"fake/{alias}" for alias in DEFAULT_MODEL_ALIASES if alias != "default"),
    }
    assert listing["fake/opus"]["resolved_model"] == "fake/m-opus"
    assert listing["fake/opus"]["model_alias"] == "opus"
    assert (listing["fake/opus"]["context_length"], listing["fake/opus"]["max_output_tokens"]) == (
        100000,
        10000,
    )
    assert catalog.public("fake")["relay"]["resolved_model"] == "fake/m-default"
    assert listing["fake/sonnet"]["context_length"] is None
    assert listing["fake/fable"]["max_output_tokens"] is None
    assert catalog.state("fake/sonnet") == "failed"
    public = catalog.public("fake/sonnet")
    assert public["relay"]["context_length"] is None
    # Tried aliases are not probed again by the same catalog.
    catalog.observe(probe=True)
    assert backend.probes == list(DEFAULT_MODEL_ALIASES)


def test_configured_aliases_replace_defaults_and_are_validated() -> None:
    catalog = ModelCatalog(configuration(), {"fake": FakeBackend()}, ["opus", "opus", "haiku"])
    assert catalog.aliases == ("opus", "haiku")
    assert set(catalog.models) == {"fake", "fake/opus", "fake/haiku"}
    assert catalog.models["fake"].model == "opus"
    for bad in ("", "-p", "--dangerously-skip-permissions", "has space", "a/b"):
        with pytest.raises(ValueError, match="invalid model alias"):
            model_aliases([bad])
    with pytest.raises(ValueError, match="at least one"):
        model_aliases([])
    assert model_aliases() == DEFAULT_MODEL_ALIASES


def test_listing_omits_routing_shortcuts_and_exact_auto_default_aliases() -> None:
    config = configuration()
    template = config.agents.pop("fake")
    names = ("claude", "codex", "cursor", "agy")
    config.agents.update({name: template.model_copy(deep=True) for name in names})
    aliases = ["default", "auto", "opus", "auto-review", "default-custom"]
    catalog = ModelCatalog(config, {name: FakeBackend(name) for name in names}, aliases)
    listing = catalog.listing()
    assert listing["object"] == "list"
    assert {entry["id"] for entry in listing["data"]} == {
        f"{name}/{alias}" for name in names for alias in ("opus", "auto-review", "default-custom")
    }
    # Listing visibility does not disable explicitly selected routing aliases.
    for model_id in ("claude", "codex/default", "cursor/auto", "relay/auto"):
        request = parse_request({"model": model_id, "messages": [{"role": "user", "content": "x"}]})
        assert catalog.candidates(request)
        assert catalog.public(model_id)["id"] == model_id


def test_listing_hides_bare_shortcut_even_when_it_resolves_to_named_model() -> None:
    catalog = ModelCatalog(configuration(), {"fake": FakeBackend()}, models=["opus"])
    assert [entry["id"] for entry in catalog.listing()["data"]] == ["fake/opus"]
    assert catalog.public("fake")["relay"]["model_alias"] == "opus"


def test_default_only_catalog_has_empty_public_listing() -> None:
    catalog = ModelCatalog(configuration(), {"fake": FakeBackend()}, models=["default", "auto"])
    assert catalog.listing() == {"object": "list", "data": []}
    assert catalog.public("fake/default")["relay"]["model_alias"] == "default"


def test_native_output_limits_are_not_refused_by_capability() -> None:
    catalog = observed_catalog(configuration(), {"fake": FakeBackend()})
    request = parse_request(
        {
            "model": "fake",
            "messages": [{"role": "user", "content": "x"}],
            "max_completion_tokens": 10,
        }
    )
    assert catalog.candidates(request)[0].agent == "fake"


def test_observed_output_limit_rejected_and_auto_filters_candidates() -> None:
    backend = FakeBackend()
    backend.support = replace(backend.support, native_max_tokens=True)
    catalog = observed_catalog(configuration(), {"fake": backend})
    request = parse_request(
        {
            "model": "fake/opus",
            "messages": [{"role": "user", "content": "x"}],
            "max_completion_tokens": 10001,
        }
    )
    with pytest.raises(GatewayError, match="observed model limit") as caught:
        catalog.candidates(request)
    assert caught.value.param == "max_completion_tokens"
    request.model = "relay/auto"
    request.max_completion_tokens = 32001
    with pytest.raises(GatewayError):
        catalog.candidates(request)
    request.max_completion_tokens = 32000
    assert catalog.candidates(request)[0].agent == "fake"


def test_auto_reports_minimum_observed_limits_or_is_omitted() -> None:
    config = configuration()
    config.agents["other"] = config.agents["fake"].model_copy(deep=True)
    other = FakeBackend("other")
    other.limits = {"default": ObservedModel("o-default", 50000, 64000)}
    catalog = observed_catalog(config, {"fake": FakeBackend(), "other": other})
    auto = catalog.public("relay/auto")["relay"]
    assert (auto["context_length"], auto["max_output_tokens"]) == (50000, 32000)
    other.limits = {}
    catalog = observed_catalog(config, {"fake": FakeBackend(), "other": other})
    auto = catalog.public("relay/auto")["relay"]
    assert auto["context_length"] is None and auto["max_output_tokens"] is None
    assert "relay/auto" not in {entry["id"] for entry in catalog.listing()["data"]}
    assert catalog.state("relay/auto") == "failed"


def test_auto_classifies_and_filters_only_qualified_capabilities() -> None:
    config = configuration()
    config.agents["fake"].categories = ["docs-for-code"]
    config.agents["debugger"] = config.agents["fake"].model_copy(deep=True)
    config.agents["debugger"].categories = ["bugfix"]
    catalog = ModelCatalog(
        config, {"fake": FakeBackend(tools=True), "debugger": FakeBackend("debugger", tools=False)}
    )
    request = parse_request(
        {
            "model": "relay/auto",
            "messages": [{"role": "user", "content": "fix this crash regression"}],
        }
    )
    assert catalog.candidates(request)[0].agent == "debugger"
    request = parse_request(
        {
            "model": "relay/auto",
            "messages": [{"role": "user", "content": "fix this crash regression"}],
            "tools": [
                {"type": "function", "function": {"name": "edit", "parameters": {"type": "object"}}}
            ],
        }
    )
    assert [model.agent for model in catalog.candidates(request)] == ["debugger", "fake"]


def test_reasoning_effort_filters_auto_and_rejects_unsupported_explicit_backend() -> None:
    config = configuration()
    config.agents["cursor"] = config.agents["fake"].model_copy(deep=True)
    cursor = FakeBackend("cursor")
    cursor.support = replace(cursor.support, supports_reasoning_effort=False)
    catalog = ModelCatalog(config, {"fake": FakeBackend(), "cursor": cursor}, models=["default"])
    request = parse_request(
        {
            "model": "relay/auto",
            "messages": [{"role": "user", "content": "x"}],
            "reasoning_effort": "high",
        }
    )
    assert [model.agent for model in catalog.candidates(request)] == ["fake"]
    request.model = "cursor"
    with pytest.raises(GatewayError) as caught:
        catalog.candidates(request)
    assert caught.value.param == "reasoning_effort"
    request.reasoning_effort = None
    assert catalog.candidates(request)[0].agent == "cursor"


def test_default_registry_adds_codex_and_opt_in_cursor_with_backend_specific_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relay.adapters import codex_completion, cursor_completion
    from relay.core.registry import load_config
    from relay.gateway.discovery import default_adapters

    def codex_models(*_: object) -> tuple[str, ...]:
        return ("gpt-codex",)

    def cursor_models(*_: object) -> tuple[str, ...]:
        return ("auto", "cursor-deep")

    monkeypatch.setattr(codex_completion, "_enumerated_models", codex_models)
    monkeypatch.setattr(cursor_completion, "_enumerated_models", cursor_models)
    config = load_config(user_config=None, project_root=None).config
    adapters = default_adapters(config)
    assert "codex" in adapters and "cursor" not in adapters
    assert adapters["codex"].id == "codex"
    catalog = ModelCatalog(config, adapters)
    assert catalog.agent_aliases["codex"] == ("default", "gpt-codex")
    assert "codex/gpt-codex" in catalog.models
    assert "codex/sonnet" not in catalog.models
    config.agents["cursor"].enabled = True
    adapters = default_adapters(config)
    catalog = ModelCatalog(config, adapters)
    assert catalog.agent_aliases["cursor"] == ("default", "auto", "cursor-deep")
    assert "cursor" in catalog.models and "cursor/auto" in catalog.models
    assert "cursor/cursor-deep" in catalog.models
    assert catalog.models["cursor"].support.supports_reasoning_effort is False
    config.agents["codex"].enabled = False
    assert "codex" not in default_adapters(config)


def test_explicit_model_filter_does_not_enumerate_cli_models() -> None:
    class EnumeratingBackend(FakeBackend):
        @property
        def default_aliases(self) -> tuple[str, ...]:
            raise AssertionError("explicit models must not enumerate CLI models")

    adapter = EnumeratingBackend()
    catalog = ModelCatalog(configuration(), {"fake": adapter}, models=["explicit"])
    assert catalog.agent_aliases["fake"] == ("explicit",)
    assert set(catalog.models) == {"fake", "fake/explicit"}
    assert not adapter.probes
