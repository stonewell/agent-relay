"""Foreground loopback-only completion gateway; optional dependencies stay lazy."""

from __future__ import annotations

import argparse
import math
import os
import secrets
import sys
from pathlib import Path

from relay.adapters.completion import DEFAULT_MODEL_ALIASES, model_aliases
from relay.core.registry import load_config

from ..errors import report_error
from ..exitcodes import Exit


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


def gateway_token(*, generate: bool = False, key: str | None = None) -> tuple[str, bool]:
    token = key if key is not None else os.environ.get("RELAY_GATEWAY_TOKEN")
    if token is None and generate:
        return secrets.token_urlsafe(32), True
    if not token or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("bearer token must be a nonempty printable ASCII token")
    return token, False


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("serve", help="run the optional local completion gateway")
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

        from relay.gateway.app import GatewayLimits, create_app
        from relay.gateway.discovery import ModelCache, default_cache_path
    except ImportError:
        return report_error(
            "relay serve: install the optional agent-relay[serve] extra", Exit.INVALID, False
        )
    try:
        token, generated = gateway_token(generate=True, key=args.key)
        config = load_config(project_root=None).config
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
    except ValueError as exc:
        return report_error(f"relay serve: {exc}", Exit.INVALID, False)
    if generated:
        print(f"RELAY_GATEWAY_TOKEN={token}", file=sys.stderr, flush=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        reload=False,
        access_log=False,
        log_level=args.log_level,
    )
    return Exit.OK
