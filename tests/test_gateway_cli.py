"""Gateway CLI authentication, isolation and safe configuration publication."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from relay.cli.commands import serve
from relay.cli.main import main
from relay.core.registry import load_config
from relay.gateway import discovery


@pytest.mark.parametrize(
    "arguments",
    [
        ["serve", "--host", "0.0.0.0"],
        ["serve", "--host", "localhost"],
        ["serve", "--concurrency", "0"],
        ["serve", "--max-request-bytes", "-1"],
        ["serve", "--timeout", "nan"],
        ["serve", "--timeout", "inf"],
        ["serve", "--log-level", "verbose"],
        ["serve", "--model", "--dangerously-skip-permissions"],
        ["serve", "--model", ""],
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
    assert "server dependencies" in result.stderr


@pytest.mark.parametrize(
    ("options", "models", "log_level"),
    [
        ([], None, "info"),
        (["--model", "opus", "--model", "fable"], ["opus", "fable"], "info"),
        *[
            (["--log-level", level], None, level)
            for level in ("critical", "error", "warning", "info", "debug", "trace")
        ],
        (["--log-level", "DeBuG"], None, "debug"),
    ],
)
def test_server_uses_global_config_and_fixed_process_options(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    options: list[str],
    models: list[str] | None,
    log_level: str,
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

    def config_loader() -> object:
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
        "log_level": log_level,
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

    def config_loader() -> object:
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


@pytest.mark.parametrize("token", ["", "has space", "line\nbreak", "nonascii-é"])
def test_invalid_tokens_are_rejected(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("RELAY_GATEWAY_TOKEN", token)
    with pytest.raises(ValueError, match="bearer token must be a nonempty printable ASCII token"):
        serve.gateway_token(generate=True)
    with pytest.raises(ValueError, match="bearer token must be a nonempty printable ASCII token"):
        serve.gateway_token(key=token, generate=True)


@pytest.mark.parametrize(
    "command",
    [
        "tui",
        "run",
        "graph",
        "plan",
        "resume",
        "reply",
        "apply",
        "clean",
        "doctor",
        "result",
        "routes",
        "explain",
        "status",
        "agents",
        "opencode-config",
        "replay",
        "cancel",
    ],
)
def test_removed_commands_are_rejected(command: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == 2


def test_help_only_lists_serve(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "serve" in output
    assert "tui" not in output and "opencode-config" not in output


def test_bare_relay_requires_serve(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "choose a command" in capsys.readouterr().err
