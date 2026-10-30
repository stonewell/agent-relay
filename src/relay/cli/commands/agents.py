"""``relay agents`` (design sections 3 and 14; task 1.22).

Lists the merged ``agents.yaml`` configuration: enabled state, command, categories, models and
effective permission cap. It reports configuration only: no agent CLI is resolved or run, so
the command costs nothing and its output does not depend on what is installed. Missing CLIs
surface when real work runs.

Secrets stay out of the output: ``env`` and ``pricing`` are configuration, not status, and are
never rendered. An invalid configuration is invalid input (exit 2).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from relay.core import registry as registry_module
from relay.core.registry import Agent, ConfigError, load_config

from ..exitcodes import Exit


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("agents", help="list configured agents")
    parser.add_argument("--json", action="store_true", help="print the agent list as JSON")


def run(args: argparse.Namespace, repo_root: Path) -> int:
    as_json = bool(args.json)
    try:
        loaded = load_config(project_root=repo_root, user_config=registry_module.USER_CONFIG)
    except ConfigError as exc:
        return _fail(f"relay agents: {exc}", Exit.INVALID, as_json)

    config = loaded.config
    entries = [
        _agent_payload(name, agent, config.defaults.permissions)
        for name, agent in sorted(config.agents.items())
    ]
    if as_json:
        _print_json({"count": len(entries), "agents": entries})
    elif not entries:
        print("no agents configured")
    else:
        print(_render(entries))
    return int(Exit.OK)


def _agent_payload(name: str, agent: Agent, default_permissions: str) -> dict[str, Any]:
    return {
        "name": name,
        "enabled": agent.enabled,
        "cmd": agent.cmd,
        "permissions": agent.permissions or default_permissions,
        "categories": list(agent.categories),
        "models": dict(agent.models),
        "concurrency": agent.concurrency,
        "cost": agent.cost,
    }


# --- output -----------------------------------------------------------------------------------


def _render(entries: list[dict[str, Any]]) -> str:
    header = (
        "NAME",
        "ENABLED",
        "CMD",
        "PERMISSIONS",
        "CATEGORIES",
        "MODELS",
    )
    rows = [
        [
            str(entry["name"]),
            _yes_no(entry["enabled"]),
            str(entry["cmd"]),
            str(entry["permissions"]),
            ",".join(str(item) for item in entry["categories"]) or "-",
            ",".join(f"{tier}={model}" for tier, model in entry["models"].items()) or "-",
        ]
        for entry in entries
    ]
    widths = [max(len(row[index]) for row in [list(header), *rows]) for index in range(len(header))]
    lines: list[str] = []
    for row in [list(header), *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=True)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
