"""Gateway CLI authentication, isolation and safe configuration publication."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from relay.adapters.completion import DEFAULT_MODEL_ALIASES, ObservedModel
from relay.cli.commands import opencode_config, serve
from relay.cli.main import main
from relay.cli.phases import COMMAND_PHASE, EARLY_ACCESS, IMPLEMENTED, availability_error
from relay.core.registry import load_config
from relay.core.specs.common import CURRENT_PHASE
from relay.gateway import discovery
from test_gateway_models import FakeBackend


def test_gateway_commands_do_not_advance_other_phase_gates() -> None:
    assert CURRENT_PHASE == 2
    assert availability_error("serve") is None
    assert availability_error("opencode-config") is None
    assert availability_error("replay") == "relay replay: available from phase 4"
    # The exemption lives in the phase table and must stay a real, implemented early access.
    assert {"serve", "opencode-config"} == EARLY_ACCESS
    assert EARLY_ACCESS <= IMPLEMENTED
    assert all(COMMAND_PHASE[command] > CURRENT_PHASE for command in EARLY_ACCESS)


@pytest.mark.parametrize(
    "arguments",
    [
        ["serve", "--host", "0.0.0.0"],
        ["serve", "--host", "localhost"],
        ["serve", "--concurrency", "0"],
        ["serve", "--max-request-bytes", "-1"],
        ["serve", "--timeout", "nan"],
        ["serve", "--timeout", "inf"],
        ["serve", "--model", "--dangerously-skip-permissions"],
        ["serve", "--model", ""],
        ["opencode-config", "--model", "a b"],
        ["opencode-config", "--timeout", "0"],
    ],
)
def test_unsafe_server_options_are_rejected(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(arguments)
    assert exc.value.code == 2


def test_core_cli_imports_without_optional_server_dependencies(tmp_path: Path) -> None:
    code = """
import builtins, sys
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name.split('.')[0] in {'fastapi', 'uvicorn', 'jsonschema'}:
        raise ImportError('deliberately unavailable')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
from relay.cli.main import main
assert not any(name.startswith('relay.gateway') for name in sys.modules)
assert main(['serve']) == 2
assert not any(name.startswith('relay.gateway') for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "agent-relay[serve]" in result.stderr


@pytest.mark.parametrize(
    ("options", "models"),
    [([], None), (["--model", "opus", "--model", "fable"], ["opus", "fable"])],
)
def test_server_uses_global_config_and_fixed_process_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    options: list[str],
    models: list[str] | None,
) -> None:
    import uvicorn

    fake_app = ModuleType("relay.gateway.app")
    calls: dict[str, object] = {}

    def limits(**kwargs: object) -> dict[str, object]:
        return kwargs

    def create_app(config: object, **kwargs: object) -> object:
        calls["config"] = config
        calls.update(kwargs)
        return "application"

    def launch(app: object, **kwargs: object) -> None:
        calls["app"] = app
        calls["uvicorn"] = kwargs

    def config_loader(*, project_root: Path | None) -> object:
        assert project_root is None
        return load_config(user_config=None)

    fake_app.GatewayLimits = limits  # type: ignore[attr-defined]
    fake_app.create_app = create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "relay.gateway.app", fake_app)
    monkeypatch.setattr(serve, "load_config", config_loader)
    monkeypatch.setattr(uvicorn, "run", launch)
    monkeypatch.delenv("RELAY_GATEWAY_TOKEN", raising=False)
    cache = tmp_path / "cache" / "models.json"
    monkeypatch.setattr(discovery, "default_cache_path", lambda: cache)
    arguments = ["-C", str(tmp_path), "serve", "--host", "::1", "--concurrency", "2", *options]
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert output.out == ""
    token = str(calls["token"])
    assert len(token) >= 43
    assert output.err == f"RELAY_GATEWAY_TOKEN={token}\n"
    assert calls["uvicorn"] == {
        "host": "::1",
        "port": 8765,
        "workers": 1,
        "reload": False,
        "access_log": False,
    }
    assert calls["limits"] == {
        "concurrency": 2,
        "timeout_s": 1800,
        "max_request_bytes": 8388608,
        "max_output_bytes": 8388608,
    }
    assert calls["models"] == models
    model_cache = calls["model_cache"]
    assert isinstance(model_cache, discovery.ModelCache) and model_cache.path == cache
    # Startup never probes or writes the model cache; discovery waits for /v1/models.
    assert not (tmp_path / ".agents").exists() and not cache.parent.exists()


def test_existing_token_is_never_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "private-token")
    assert serve.gateway_token(generate=True) == ("private-token", False)
    assert capsys.readouterr().out == ""


def test_gateway_token_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RELAY_GATEWAY_TOKEN", raising=False)
    generated_token, is_gen = serve.gateway_token(generate=True)
    assert is_gen is True
    assert len(generated_token) >= 43
    assert all(33 <= ord(c) <= 126 for c in generated_token)

    with pytest.raises(ValueError, match="bearer token must be a nonempty printable ASCII token"):
        serve.gateway_token(generate=False)

    assert serve.gateway_token(key="explicit-key", generate=True) == ("explicit-key", False)
    assert serve.gateway_token(key="explicit-key", generate=False) == ("explicit-key", False)

    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "env-token")
    assert serve.gateway_token(key="override-key", generate=True) == ("override-key", False)
    assert serve.gateway_token(generate=True) == ("env-token", False)


def test_server_cli_with_key_option(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import uvicorn

    fake_app = ModuleType("relay.gateway.app")
    calls: dict[str, object] = {}

    def limits(**kwargs: object) -> dict[str, object]:
        return kwargs

    def create_app(config: object, **kwargs: object) -> object:
        calls.update(kwargs)
        return "application"

    def launch(app: object, **kwargs: object) -> None:
        calls["app"] = app
        calls["uvicorn"] = kwargs

    def config_loader(*, project_root: Path | None) -> object:
        assert project_root is None
        return load_config(user_config=None)

    fake_app.GatewayLimits = limits  # type: ignore[attr-defined]
    fake_app.create_app = create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "relay.gateway.app", fake_app)
    monkeypatch.setattr(serve, "load_config", config_loader)
    monkeypatch.setattr(uvicorn, "run", launch)
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", "ignored-env-token")
    monkeypatch.setattr(discovery, "default_cache_path", lambda: tmp_path / "cache.json")

    assert main(["serve", "--key", "cli-key-123"]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""
    assert calls["token"] == "cli-key-123"

    assert main(["serve", "--key", "invalid with spaces"]) == 2
    err_output = capsys.readouterr().err
    assert "relay serve: bearer token must be a nonempty printable ASCII token" in err_output


@pytest.mark.parametrize("catalog", [None, [], {}, {"data": None}])
def test_config_rejects_invalid_catalogs(catalog: object) -> None:
    with pytest.raises(ValueError, match="invalid model catalog"):
        opencode_config.provider_config(catalog, "http://127.0.0.1:8765/v1")


def test_ipv6_config_url_is_canonical() -> None:
    assert opencode_config.validate_base_url("http://[::1]:1234/v1/") == (
        "http://[::1]:1234/v1",
        "::1",
        1234,
    )


@pytest.mark.parametrize("token", ["", "has space", "line\nbreak", "nonascii-é"])
def test_invalid_tokens_are_rejected(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", token)
    with pytest.raises(ValueError, match="bearer token must be a nonempty printable ASCII token"):
        serve.gateway_token(generate=True)
    with pytest.raises(ValueError, match="bearer token must be a nonempty printable ASCII token"):
        serve.gateway_token(key=token, generate=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "http://localhost/v1",
        "https://127.0.0.1/v1",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://secret@127.0.0.1/v1",
        "http://127.0.0.1/v1?token=secret",
        "http://127.0.0.1/v1#secret",
        "http://127.0.0.1/other",
        "http://127.1/v1",
    ],
)
def test_config_rejects_unsafe_urls_before_probing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str
) -> None:
    def forbidden(*args: object) -> object:
        pytest.fail("invalid URL must never load backends or probe")

    monkeypatch.setattr(discovery, "default_adapters", forbidden)
    assert main(["opencode-config", "--base-url", url, "--refresh", "--json"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "base URL must" in json.loads(output.err)["error"]


def config_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[list[FakeBackend], Path]:
    """opencode-config against fake backends and a private cache; no CLI or model call."""
    backends: list[FakeBackend] = []
    cache = tmp_path / "state" / "gateway-models.json"

    def adapters(config: object) -> dict[str, FakeBackend]:
        backends.append(FakeBackend("claude"))
        return {"claude": backends[-1]}

    def config_loader(*, project_root: Path | None) -> object:
        assert project_root is None
        return load_config(user_config=None)

    monkeypatch.setattr(opencode_config, "load_config", config_loader)
    monkeypatch.setattr(discovery, "default_adapters", adapters)
    monkeypatch.setattr(discovery, "default_cache_path", lambda: cache)
    monkeypatch.delenv("RELAY_GATEWAY_TOKEN", raising=False)
    return backends, cache


def test_config_without_cache_reports_undiscovered_models_and_never_probes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    backends, cache = config_environment(monkeypatch, tmp_path)
    assert main(["-C", str(tmp_path), "opencode-config"]) == 0
    output = capsys.readouterr()
    provider = json.loads(output.out)["provider"]["relay"]
    assert set(provider["models"]) == {
        "claude",
        "claude/default",
        "claude/sonnet",
        "claude/opus",
        "claude/fable",
        "relay/auto",
    }
    assert all("limit" not in model for model in provider["models"].values())
    assert provider["options"] == {
        "baseURL": "http://127.0.0.1:8765/v1",
        "apiKey": "{env:RELAY_GATEWAY_TOKEN}",
    }
    assert "not yet discovered" in output.err and "--refresh" in output.err
    assert "claude/opus" in output.err and "relay/auto" in output.err
    assert backends[0].probes == [] and backends[0].calls == []
    assert not cache.exists() and not (tmp_path / ".agents").exists()


def test_config_refresh_probes_caches_and_later_reads_cache(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    backends, cache = config_environment(monkeypatch, tmp_path)
    assert main(["opencode-config", "--refresh"]) == 0
    output = capsys.readouterr()
    assert backends[0].probes == list(DEFAULT_MODEL_ALIASES)
    models = json.loads(output.out)["provider"]["relay"]["models"]
    assert models["claude/opus"] == {
        "name": "claude/opus",
        "tool_call": True,
        "limit": {"context": 100000, "output": 10000},
    }
    assert set(models) == {
        "claude",
        "claude/default",
        "claude/sonnet",
        "claude/opus",
        "claude/fable",
        "relay/auto",
    }
    assert models["relay/auto"]["limit"] == models["claude"]["limit"]
    assert models["claude"]["limit"] == {"context": 200000, "output": 32000}
    # Failed probes keep their entry without limits; limits are never invented.
    assert "listed without observed context/output limits (probe failed)" in output.err
    assert "claude/sonnet" in output.err and "claude/fable" in output.err
    assert "not yet discovered" not in output.err
    assert cache.is_file()

    assert main(["opencode-config"]) == 0
    cached = capsys.readouterr()
    assert backends[1].probes == []
    assert json.loads(cached.out)["provider"]["relay"]["models"] == models
    assert "claude/sonnet" in cached.err and "--refresh" in cached.err

    assert main(["opencode-config", "--model", "opus", "--json"]) == 0
    selected = capsys.readouterr()
    assert backends[2].probes == []
    assert set(json.loads(selected.out)["provider"]["relay"]["models"]) == {
        "claude",
        "claude/opus",
        "relay/auto",
    }
    assert selected.err == ""

    # An added alias is undiscovered until refreshed; refresh probes only what is asked.
    assert main(["opencode-config", "--model", "opus", "--model", "haiku", "--json"]) == 0
    added = capsys.readouterr()
    warnings = [json.loads(line)["warning"] for line in added.err.splitlines()]
    assert len(warnings) == 1 and "claude/haiku" in warnings[0]
    assert backends[3].probes == []
    backends_before = len(backends)
    assert main(["opencode-config", "--model", "haiku", "--refresh"]) == 0
    capsys.readouterr()
    assert backends[backends_before].probes == ["haiku"]


def test_config_refresh_replaces_changed_observations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    backends, _cache = config_environment(monkeypatch, tmp_path)
    assert main(["opencode-config", "--refresh", "--model", "opus"]) == 0
    capsys.readouterr()
    original = FakeBackend.__init__

    def upgraded(self: FakeBackend, name: str = "fake", *, tools: bool = True) -> None:
        original(self, name, tools=tools)
        self.limits = {"opus": ObservedModel("m-opus-2", 400000, 20000)}

    monkeypatch.setattr(FakeBackend, "__init__", upgraded)
    assert main(["opencode-config", "--refresh", "--model", "opus"]) == 0
    models = json.loads(capsys.readouterr().out)["provider"]["relay"]["models"]
    assert models["claude/opus"]["limit"] == {"context": 400000, "output": 20000}
    assert backends[-1].probes == ["opus"]
