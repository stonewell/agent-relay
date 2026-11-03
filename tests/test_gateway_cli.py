"""Gateway CLI authentication, isolation and safe configuration publication."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from relay.cli.commands import serve
from relay.cli.main import main
from relay.core import registry
from relay.core.registry import load_config
from relay.gateway import discovery


def isolate_user_config(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Point the user config (and so the log directory) at a temporary home."""
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # USER_CONFIG is resolved at import time, so the module attribute is what serve reads.
    monkeypatch.setattr(registry, "USER_CONFIG", home / ".config" / "relay" / "agents.yaml")
    return home / ".config" / "relay" / "logs"


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
    log_file = isolate_user_config(monkeypatch, tmp_path / "home") / "relay-8765.log"
    arguments = ["-C", str(tmp_path), "serve", "--host", "::1", "--concurrency", "2", *options]
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert output.out == ""
    token = str(calls["token"])
    assert len(token) >= 43
    assert output.err == f"RELAY_GATEWAY_TOKEN={token}\nrelay log file: {log_file}\n"
    assert calls["uvicorn"] == {
        "host": "::1",
        "port": 8765,
        "workers": 1,
        "reload": False,
        "access_log": False,
        "log_level": log_level,
        "log_config": serve.log_config(uvicorn.config.LOGGING_CONFIG, log_file),
        "timeout_graceful_shutdown": 2,
    }
    assert log_file.parent.is_dir() and not log_file.exists()
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
    logs = isolate_user_config(monkeypatch, tmp_path / "home")

    assert main(["serve", "--key", "cli-key-123"]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"relay log file: {logs / 'relay-8765.log'}\n"
    assert calls["token"] == "cli-key-123"

    assert main(["serve", "--key", "invalid with spaces"]) == 2
    err_output = capsys.readouterr().err
    assert "relay serve: bearer token must be a nonempty printable ASCII token" in err_output


def test_log_config_adds_rotating_context_file_sink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import contextvars
    import logging
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    from relay.gateway.logctx import AGENT, REQUEST_ID

    path = isolate_user_config(monkeypatch, tmp_path / "home") / "relay-9001.log"
    assert serve.log_file_path(9001) == path
    config = serve.log_config(LOGGING_CONFIG, path)
    handler = config["handlers"]["relay_file"]
    assert handler == {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(path),
        "maxBytes": 10 * 1024 * 1024,
        "backupCount": 3,
        "encoding": "utf-8",
        "delay": True,
        "formatter": "relay_file",
        "filters": ["relay_context"],
    }
    assert config["filters"]["relay_context"] == {"()": "relay.gateway.logctx.ContextFilter"}
    assert config["loggers"]["uvicorn.error"]["handlers"] == ["relay_file"]
    # uvicorn's own console handler and the shared base config stay intact.
    assert config["handlers"]["default"] == LOGGING_CONFIG["handlers"]["default"]
    assert config["loggers"]["uvicorn"] == LOGGING_CONFIG["loggers"]["uvicorn"]
    assert "handlers" not in LOGGING_CONFIG["loggers"]["uvicorn.error"]

    # The dictConfig entries resolve and produce context-tagged lines (on a private logger).
    path.parent.mkdir(parents=True)
    probe = logging.getLogger("relay.test.logfile")
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": config["filters"],
            "formatters": {"relay_file": config["formatters"]["relay_file"]},
            "handlers": {"relay_file": handler},
            "loggers": {
                probe.name: {"handlers": ["relay_file"], "level": "DEBUG", "propagate": False}
            },
        }
    )
    try:
        probe.info("outside")

        def tagged() -> None:
            REQUEST_ID.set("chatcmpl-x")
            AGENT.set("fake")
            probe.debug("inside")

        contextvars.copy_context().run(tagged)
    finally:
        for attached in probe.handlers[:]:
            probe.removeHandler(attached)
            attached.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith(" INFO req=- agent=- outside")
    assert lines[1].endswith(" DEBUG req=chatcmpl-x agent=fake inside")


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
