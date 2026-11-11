"""Foreground loopback-only completion gateway; server imports stay lazy."""

from __future__ import annotations

import argparse
import copy
import math
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from relay.adapters.completion import DEFAULT_MODEL_ALIASES, model_aliases
from relay.core import registry
from relay.core.registry import load_config

from ..errors import report_error
from ..exitcodes import Exit

LOG_FILE_FORMAT = "%(asctime)s %(levelname)s req=%(request_id)s agent=%(agent)s %(message)s"
LOG_FILE_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 3


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def model_alias(value: str) -> str:
    try:
        return model_aliases([value])[0]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def add_model_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        type=model_alias,
        metavar="ALIAS",
        help=(
            "CLI model alias to offer; repeat to list several "
            f"(default: {', '.join(DEFAULT_MODEL_ALIASES)})"
        ),
    )


def log_file_path(port: int) -> Path:
    """One rotating log file per relay instance, beside the user config."""
    return registry.USER_CONFIG.parent / "logs" / f"relay-{port}.log"


def log_config(base: dict[str, Any], path: Path) -> dict[str, Any]:
    """Extend uvicorn's logging config with a context-tagged file sink; console is unchanged."""
    config = copy.deepcopy(base)
    config.setdefault("filters", {})["relay_context"] = {
        "()": "relay.gateway.logctx.ContextFilter"
    }
    config.setdefault("formatters", {})["relay_file"] = {"format": LOG_FILE_FORMAT}
    config.setdefault("handlers", {})["relay_file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(path),
        "maxBytes": LOG_FILE_BYTES,
        "backupCount": LOG_FILE_BACKUPS,
        "encoding": "utf-8",
        "delay": True,
        "formatter": "relay_file",
        "filters": ["relay_context"],
    }
    # "uvicorn.error" still propagates to the "uvicorn" console handler.
    logger = config.setdefault("loggers", {}).setdefault("uvicorn.error", {"level": "INFO"})
    logger["handlers"] = [*logger.get("handlers", []), "relay_file"]
    return config


def gateway_token(*, generate: bool = False, key: str | None = None) -> tuple[str, bool]:
    token = key if key is not None else os.environ.get("RELAY_GATEWAY_TOKEN")
    if token is None and generate:
        return secrets.token_urlsafe(32), True
    if not token or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("bearer token must be a nonempty printable ASCII token")
    return token, False


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("serve", help="run the local completion gateway")
    parser.add_argument("--host", choices=("127.0.0.1", "::1"), default="127.0.0.1")
    parser.add_argument("--port", type=positive_int, default=8765)
    parser.add_argument(
        "--log-level",
        type=str.lower,
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        default="info",
        help="server logging level (default: INFO)",
    )
    parser.add_argument(
        "--key",
        metavar="KEY",
        help="fixed API bearer token (overrides RELAY_GATEWAY_TOKEN; generated if omitted)",
    )
    parser.add_argument("--concurrency", type=positive_int)
    parser.add_argument("--timeout", type=positive_float)
    parser.add_argument("--max-request-bytes", type=positive_int, default=8 * 1024 * 1024)
    parser.add_argument("--max-output-bytes", type=positive_int, default=8 * 1024 * 1024)
    add_model_option(parser)


def run(args: argparse.Namespace, repo_root: Path) -> int:
    if args.port > 65535:
        return report_error("relay serve: port must be between 1 and 65535", Exit.INVALID, False)
    try:
        import uvicorn
        from uvicorn.config import LOGGING_CONFIG

        from relay.gateway.app import GatewayLimits, create_app
        from relay.gateway.discovery import ModelCache, default_cache_path
    except ImportError:
        return report_error(
            "relay serve: install agent-relay with its server dependencies", Exit.INVALID, False
        )
    try:
        token, generated = gateway_token(generate=True, key=args.key)
        config = load_config().config
        limits = GatewayLimits(
            concurrency=args.concurrency or config.defaults.concurrency,
            timeout_s=args.timeout or config.defaults.timeout_s,
            max_request_bytes=args.max_request_bytes,
            max_output_bytes=args.max_output_bytes,
        )
        app = create_app(
            config,
            token=token,
            limits=limits,
            host=args.host,
            models=args.models,
            model_cache=ModelCache(default_cache_path()),
        )
        log_path = log_file_path(args.port)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError) as exc:
        return report_error(f"relay serve: {exc}", Exit.INVALID, False)
    if generated:
        print(f"RELAY_GATEWAY_TOKEN={token}", file=sys.stderr, flush=True)
    print(f"relay log file: {log_path}", file=sys.stderr, flush=True)
    # workers stays 1: admission state (GatewayRuntime) is per-process, so more
    # workers would multiply the concurrency limit and break per-agent caps.
    # Scale with --concurrency instead; it maps to parallel backend threads.
    # Ctrl+C must not wait up to --timeout for open streams: after a short grace uvicorn
    # cancels them, and lifespan shutdown cancels and drains their completions.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        reload=False,
        access_log=False,
        log_level=args.log_level,
        log_config=log_config(LOGGING_CONFIG, log_path),
        timeout_graceful_shutdown=2,
    )
    return Exit.OK
