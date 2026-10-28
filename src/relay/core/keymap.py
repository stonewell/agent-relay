"""TUI shortcut names, defaults and validation (configured under `tui.keys` in agents.yaml).

Kept free of Textual so the configuration model can validate keys without importing the UI.
An empty string unbinds an action: it stays available from the command palette and, for the
contextual ones, the action buttons.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# action id -> (Textual action, description shown in the footer / command palette)
KEY_ACTIONS: dict[str, tuple[str, str]] = {
    "quit": ("quit", "Quit"),
    "send": ("send", "Send"),
    "toggle_mode": ("toggle_mode", "Answer/request"),
    "help": ("help", "Help"),
    "next_view": ("next_view", "Next view"),
    "view_conversation": ("view('conversation')", "Chat"),
    "view_tasks": ("view('tasks')", "Tasks"),
    "view_inspector": ("view('inspector')", "Inspect"),
    "plan": ("plan", "Plan"),
    "deliver": ("deliver", "Deliver"),
    "sessions": ("sessions", "Sessions"),
    "stop": ("stop", "Stop"),
    "coordinator": ("coordinator", "Coordinator"),
}

# Few chords, one function key. Everything else is in the command palette (Ctrl+P) and, when
# it applies, the action buttons. Ctrl+A/E/K/U/W/D/X/C/V/Z/Y stay with the text editor;
# Ctrl+B is avoided (tmux prefix) and Ctrl+G is the Emacs "abort".
DEFAULT_KEYS: dict[str, str] = {
    "quit": "ctrl+q",
    "send": "ctrl+s",
    "toggle_mode": "ctrl+t",
    "help": "f1",
    "next_view": "ctrl+n",
    "view_conversation": "",
    "view_tasks": "",
    "view_inspector": "",
    "plan": "ctrl+r",
    "deliver": "",
    "sessions": "",
    "stop": "ctrl+g",
    "coordinator": "",
}

# Keys the UI itself needs (focus, dialogs, editing newline, the palette, terminal aliases).
RESERVED = frozenset(
    {"enter", "tab", "escape", "ctrl+c", "ctrl+p", "ctrl+h", "ctrl+i", "ctrl+j", "ctrl+m"}
)

_KEY = re.compile(r"^[a-z0-9_]+(\+[a-z0-9_]+)*$")


def effective_keys(overrides: Mapping[str, str]) -> dict[str, str]:
    """Defaults with `overrides` applied; raises ValueError on a bad name, key or duplicate."""
    unknown = sorted(set(overrides) - set(KEY_ACTIONS))
    if unknown:
        raise ValueError(f"unknown tui key action(s) {unknown}; known: {sorted(KEY_ACTIONS)}")
    keys = {**DEFAULT_KEYS, **overrides}
    owner: dict[str, str] = {}
    for action, value in keys.items():
        for key in (k.strip() for k in value.split(",")) if value.strip() else ():
            if not _KEY.match(key):
                raise ValueError(f"tui key for {action!r}: {key!r} is not a key name (e.g. ctrl+r)")
            if key in RESERVED:
                raise ValueError(f"tui key for {action!r}: {key!r} is reserved")
            if key in owner and owner[key] != action:
                raise ValueError(f"tui key {key!r} is bound to both {owner[key]!r} and {action!r}")
            owner[key] = action
    return keys
