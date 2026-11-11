"""Cursor command resolution and completion schema instructions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from relay.core.platform.proc import CommandNotFound, resolve_command

DEFAULT_COMMAND = "agent"
LEGACY_COMMAND = "cursor-agent"


def resolve_cursor_command(cmd: str, env: Mapping[str, str]) -> str:
    """The Cursor command to launch for the configured `cmd`.

    The legacy ``cursor-agent`` alias is tried only when `cmd` is the built-in ``agent`` name
    and it does not resolve; an explicitly configured command is never replaced. When neither
    resolves, `cmd` is returned and launching it reports the missing executable.
    """
    if cmd != DEFAULT_COMMAND:
        return cmd
    for candidate in (DEFAULT_COMMAND, LEGACY_COMMAND):
        try:
            resolve_command(candidate, (), env=env)
        except CommandNotFound:
            continue
        return candidate
    return cmd


def schema_instructions(schema: Mapping[str, Any]) -> str:
    """Prompt text requesting one JSON object for `schema`, as the CLI has no schema flag."""
    return (
        "\n\nWhen you are finished, reply with only one JSON object (no prose and no code "
        "fence) that conforms to this JSON Schema:\n" + json.dumps(schema, separators=(",", ":"))
    )
