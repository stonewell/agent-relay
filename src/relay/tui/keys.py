"""The effective shortcut map: bindings for the app and key hints for on-screen text."""

from __future__ import annotations

from collections.abc import Mapping

from relay.core.keymap import DEFAULT_KEYS, KEY_ACTIONS, effective_keys

_active: dict[str, str] = dict(DEFAULT_KEYS)


def configure(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Set the process-wide map (one app per process) from `tui.keys` overrides."""
    _active.clear()
    _active.update(effective_keys(overrides or {}))
    return dict(_active)


def pretty(key: str) -> str:
    return "+".join(part.capitalize() if len(part) > 1 else part.upper() for part in key.split("+"))


def label(action: str) -> str:
    """The first key bound to `action` as shown to the operator, or "" when unbound."""
    first = next((k.strip() for k in _active.get(action, "").split(",") if k.strip()), "")
    return pretty(first) if first else ""


def hint(action: str) -> str:
    """` (Ctrl+R)` for a bound action, "" otherwise, to append to a sentence."""
    text = label(action)
    return f" ({text})" if text else ""


def bindings() -> list[tuple[str, str, str]]:
    """(keys, textual action, description) for every bound action."""
    return [
        (_active[name], target, description)
        for name, (target, description) in KEY_ACTIONS.items()
        if _active.get(name, "").strip()
    ]
