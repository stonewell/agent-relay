"""Antigravity completion envelope parsing."""

from __future__ import annotations

import json
from typing import Any, cast

_AGY_STATUSES = frozenset(
    {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}
)


def _try_json(text: str) -> dict[str, Any] | None:
    """Parse `text` as a JSON object, or None."""
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _is_envelope(data: dict[str, Any]) -> bool:
    """True when `data` looks like the `agy` JSON envelope (design section 7.2).

    The envelope carries ``conversation_id``, ``status``, ``response``, ``error``,
    ``structured_output`` and ``usage``; it has no ``"type"`` marker like Claude's.
    """
    if data.get("status") in _AGY_STATUSES:
        return True
    return "conversation_id" in data or "structured_output" in data


def _unwrap_envelope(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("event") == "result" and isinstance(data.get("result"), dict):
        inner = cast("dict[str, Any]", data["result"])
        return inner if _is_envelope(inner) else None
    if _is_envelope(data):
        return data
    return None


def _agy_payload(stdout: str) -> dict[str, Any] | None:
    """Parse the `agy` ``--output-format json`` envelope from stdout, or None.

    The envelope is a single JSON object (the whole stdout for ``--output-format json``);
    a line scan is the fallback in case progress text precedes it.
    """
    text = stdout.strip()
    if not text:
        return None
    data = _try_json(text)
    if data is not None:
        unwrapped = _unwrap_envelope(data)
        if unwrapped is not None:
            return unwrapped
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        data = _try_json(line)
        if data is not None:
            unwrapped = _unwrap_envelope(data)
            if unwrapped is not None:
                return unwrapped
    return None


def _error_text(payload: dict[str, Any]) -> str | None:
    """The envelope's `error` text, when it is a non-empty string."""
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


agy_envelope = _agy_payload
envelope_error = _error_text
