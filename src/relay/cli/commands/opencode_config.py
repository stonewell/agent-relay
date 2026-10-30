"""Print OpenCode provider configuration from the discovered gateway model catalog.

The catalog is the one `relay serve` builds: qualified backends, the configured model
aliases and the limits the CLI itself reported when each alias was probed. Without
``--refresh`` only cached observations are used and no model call is made; ``--refresh``
probes every alias (one short billed CLI call each) and updates the shared cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from relay.core.registry import load_config

from ..errors import report_error
from ..exitcodes import Exit
from .serve import add_model_option, positive_float


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser(
        "opencode-config", help="print provider config for the local completion gateway"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765/v1")
    add_model_option(parser)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="probe every model alias now (billed CLI calls) and update the cache",
    )
    parser.add_argument(
        "--timeout", type=positive_float, help="seconds allowed per model probe (default: 120)"
    )
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def validate_base_url(value: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(value)
        port = 80 if parsed.port is None else parsed.port
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path.rstrip("/") != "/v1"
            or parsed.query
            or parsed.fragment
            or not 1 <= port <= 65535
        ):
            raise ValueError
    except ValueError:
        raise ValueError(
            "base URL must be http://127.0.0.1:PORT/v1 or http://[::1]:PORT/v1"
        ) from None
    host = parsed.hostname
    authority = f"[{host}]" if host == "::1" else host
    return f"http://{authority}:{port}/v1", host, port


def provider_config(
    catalog: object,
    base_url: str,
    omitted: list[str] | None = None,
    *,
    effort_models: set[str] | None = None,
) -> dict[str, object]:
    """Build the provider snippet; models without observed limits are listed without one."""
    if not isinstance(catalog, dict):
        raise ValueError("gateway returned an invalid model catalog")
    entries = cast(dict[str, object], catalog).get("data")
    if not isinstance(entries, list):
        raise ValueError("gateway returned an invalid model catalog")
    models: dict[str, object] = {}
    for raw in cast(list[object], entries):
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, object], raw)
        model_id, raw_metadata = entry.get("id"), entry.get("relay")
        if not isinstance(model_id, str) or not model_id or not isinstance(raw_metadata, dict):
            if omitted is not None and isinstance(model_id, str) and model_id:
                omitted.append(model_id)
            continue
        metadata = cast(dict[str, object], raw_metadata)
        context, output = metadata.get("context_length"), metadata.get("max_output_tokens")
        model: dict[str, object] = {
            "name": model_id,
            "tool_call": metadata.get("supports_tools") is True,
        }
        if effort_models is None or model_id in effort_models:
            model["variants"] = {
                effort: {"reasoningEffort": effort} for effort in ("low", "medium", "high")
            }
        if type(context) is int and type(output) is int and context > 0 and output > 0:
            model["limit"] = {"context": context, "output": output}
        # Unprobed models keep their entry: OpenCode falls back to its own default limits.
        models[model_id] = model
    return {
        "provider": {
            "relay": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Relay",
                "options": {"baseURL": base_url, "apiKey": "{env:RELAY_GATEWAY_TOKEN}"},
                "models": models,
            }
        }
    }


def run(args: argparse.Namespace, repo_root: Path) -> int:
    from relay.adapters.completion import CompletionTerminationError
    from relay.gateway.discovery import (
        PROBE_TIMEOUT_S,
        ModelCache,
        default_adapters,
        default_cache_path,
    )
    from relay.gateway.models import ModelCatalog

    notices: list[str] = []
    try:
        base_url, _host, _port = validate_base_url(args.base_url)
        config = load_config(project_root=None).config
        catalog = ModelCatalog(
            config, default_adapters(config), args.models, ModelCache(default_cache_path())
        )
        try:
            catalog.observe(
                probe=args.refresh,
                refresh=args.refresh,
                timeout_s=args.timeout or PROBE_TIMEOUT_S,
            )
        except CompletionTerminationError:
            raise ValueError("a model probe's process termination could not be confirmed") from None
        invalid: list[str] = []
        effort_models = {
            name
            for name, model in catalog.models.items()
            if model.support.supports_reasoning_effort
        }
        if any(model.support.supports_reasoning_effort for model in catalog.defaults):
            effort_models.add("relay/auto")
        snippet = provider_config(catalog.listing(), base_url, invalid, effort_models=effort_models)
    except ValueError as exc:
        return report_error(f"relay opencode-config: {exc}", Exit.INVALID, args.json)
    if not catalog.defaults:
        notices.append("relay opencode-config: no models configured; no models listed")
    ids = [*catalog.models, *(["relay/auto"] if catalog.defaults else [])]
    undiscovered = [i for i in ids if catalog.state(i) == "undiscovered"]
    failed = [i for i in ids if catalog.state(i) == "failed"]
    if undiscovered:
        notices.append(
            "relay opencode-config: models not yet discovered (no cached CLI observation for "
            "this CLI version/configuration): "
            + ", ".join(undiscovered)
            + "; run `relay opencode-config --refresh` to probe them (one short billed CLI "
            "call per model alias)"
        )
    if failed:
        notices.append(
            "relay opencode-config: listed without observed context/output limits (probe "
            "failed): "
            + ", ".join(failed)
            + "; run `relay opencode-config --refresh` to probe them again"
        )
    if invalid:
        notices.append(
            "relay opencode-config: omitted models with unusable catalog metadata: "
            + ", ".join(invalid)
        )
    print(json.dumps(snippet, indent=2, ensure_ascii=True))
    for notice in notices:
        # stdout stays a pure config snippet; explain an empty or partial model list on stderr.
        print(
            json.dumps({"warning": notice}, ensure_ascii=True) if args.json else notice,
            file=sys.stderr,
        )
    return Exit.OK
