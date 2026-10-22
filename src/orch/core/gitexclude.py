"""Repository-local exclude block for runtime state (design section 6).

The block goes in the file named by `git rev-parse --git-path info/exclude`: it is shared by
linked worktrees, never committed, and `.gitignore` is never touched.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .specs.common import RESERVED_ROOTS
from .store import atomic_write, read_text

START = "# >>> orch runtime state"
END = "# <<< orch runtime state"
NOTE = "# Managed by orch. Edits between the markers are overwritten."


class ExcludeError(RuntimeError):
    """The exclude file cannot be located or its block is malformed."""


@dataclass(frozen=True)
class ExcludeResult:
    path: Path
    changed: bool


def block_lines() -> list[str]:
    """The block's lines: one anchored directory pattern per runtime subtree (not `.git`)."""
    patterns = ["/" + "/".join(root) + "/" for root in RESERVED_ROOTS if root != (".git",)]
    return [START, NOTE, *patterns, END]


def exclude_path(repo_root: Path) -> Path:
    """Locate `info/exclude`, which for linked worktrees lives in the shared git directory."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-path", "info/exclude"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ExcludeError("git is not installed or not on PATH") from exc
    if proc.returncode != 0:
        raise ExcludeError(f"{repo_root} is not inside a git repository: {proc.stderr.strip()}")
    located = Path(proc.stdout.strip())
    return located if located.is_absolute() else (repo_root / located).resolve()


def _split(text: str) -> tuple[list[str], str]:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the trailing newline is re-added on write
    return lines, newline


def _without_blocks(lines: list[str], source: Path) -> tuple[list[str], int | None]:
    """Remove every marked block; returns the remaining lines and where the first one was."""
    kept: list[str] = []
    first: int | None = None
    inside = False
    for line in lines:
        if line.strip() == START:
            if inside:
                raise ExcludeError(f"{source}: nested '{START}' marker")
            inside = True
            if first is None:
                first = len(kept)
            continue
        if line.strip() == END:
            if not inside:
                raise ExcludeError(f"{source}: '{END}' without a start marker")
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        raise ExcludeError(f"{source}: '{START}' has no matching '{END}'; fix or remove it")
    return kept, first


def _render(existing: str, source: Path) -> str:
    lines, newline = _split(existing)
    kept, first = _without_blocks(lines, source)
    position = len(kept) if first is None else first
    merged = [*kept[:position], *block_lines(), *kept[position:]]
    return newline.join(merged) + newline


def ensure_exclude_block(repo_root: Path) -> ExcludeResult:
    """Add or refresh the block; idempotent, preserves every other line."""
    path = exclude_path(repo_root)
    existing = read_text(path) if path.exists() else ""
    updated = _render(existing, path)
    if updated == existing:
        return ExcludeResult(path, changed=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, updated)
    return ExcludeResult(path, changed=True)


BlockState = Literal["current", "missing", "outdated", "malformed", "no-repository"]


def check_exclude_block(repo_root: Path) -> tuple[BlockState, str]:
    """For `orch doctor`: the block's state and a one-line explanation."""
    try:
        path = exclude_path(repo_root)
    except ExcludeError as exc:
        return "no-repository", str(exc)
    existing = read_text(path) if path.exists() else ""
    try:
        wanted = _render(existing, path)
    except ExcludeError as exc:
        return "malformed", str(exc)
    if wanted == existing:
        return "current", str(path)
    if START not in existing:
        return "missing", f"no orch block in {path}"
    return "outdated", f"orch block in {path} differs from this version"
