"""``relay agents`` (design sections 3 and 14; task 1.22).

Lists the merged ``agents.yaml`` configuration: enabled state, command, categories, models and
effective permission cap, plus a free availability probe of enabled agents (``<cmd> --version``,
as in section 3). No model is called and the probe never runs ``--help`` interactively, so the
command costs nothing. Missing CLIs are reported as ``available: false``; the command never
prints a traceback for them.

Secrets stay out of the output: ``env`` and ``pricing`` are configuration, not status, and are
never rendered. An invalid configuration is invalid input (exit 2).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from relay.core import registry as registry_module
from relay.core.platform.proc import CommandNotFound, resolve_command, run_process
from relay.core.registry import Agent, ConfigError, load_config

from ..exitcodes import Exit

_PROBE_TIMEOUT_S = 10.0
_LEVELS: tuple[str, ...] = ("full",)
"""Levels advertised by the adapters in this phase (sections 7.2 and 9.3)."""


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("agents", help="list configured agents and their availability")
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
    available: bool | None
    version: str | None = None
    note: str | None = None
    if agent.enabled:
        available, version, note = _probe(agent.cmd)
    else:
        available = None
        note = "agent disabled"
    return {
        "name": name,
        "enabled": agent.enabled,
        "cmd": agent.cmd,
        "available": available,
        "version": version,
        "levels": list(_LEVELS),
        "permissions": agent.permissions or default_permissions,
        "categories": list(agent.categories),
        "models": dict(agent.models),
        "concurrency": agent.concurrency,
        "cost": agent.cost,
        "note": note,
    }


def _probe(cmd: str) -> tuple[bool, str | None, str | None]:
    """Resolve and run ``<cmd> --version`` without a shell, model call or interactive prompt."""
    try:
        resolved = resolve_command(cmd, ("--version",))
    except CommandNotFound:
        return False, None, "command not found on PATH"
    except (OSError, ValueError) as exc:
        return False, None, f"cannot resolve command: {exc}"
    try:
        proc = run_process(
            resolved,
            cwd=Path.cwd(),
            env={**os.environ},
            timeout_s=_PROBE_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never crash `relay agents`
        return False, None, f"probe failed: {exc}"
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    first = lines[0].strip() if lines else None
    if proc.timed_out:
        return False, first, f"--version timed out after {_PROBE_TIMEOUT_S:g}s"
    if proc.exit_code != 0:
        detail = f"--version exited {proc.exit_code}"
        return False, first, detail
    return True, first, None


# --- output -----------------------------------------------------------------------------------


def _render(entries: list[dict[str, Any]]) -> str:
    header = (
        "NAME",
        "ENABLED",
        "AVAILABLE",
        "VERSION",
        "CMD",
        "PERMISSIONS",
        "CATEGORIES",
        "MODELS",
    )
    rows = [
        [
            str(entry["name"]),
            _yes_no(entry["enabled"]),
            _availability(entry["available"]),
            str(entry["version"] or "-"),
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
    notes = [
        f"{entry['name']}: {entry['note']}"
        for entry in entries
        if isinstance(entry.get("note"), str) and entry["note"]
    ]
    if notes:
        lines.append("")
        lines.extend(f"  {note}" for note in notes)
    return "\n".join(lines)


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _availability(value: Any) -> str:
    if value is None:
        return "-"
    return "yes" if value else "no"


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _fail(message: str, code: Exit, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message}), file=sys.stderr)
    else:
        print(message, file=sys.stderr)
    return int(code)
