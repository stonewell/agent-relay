"""Shared building blocks for the spec models (design section 5)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from fnmatch import translate
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

SPEC_VERSION = 1
"""Highest spec major version this build understands (section 5.4)."""

CURRENT_PHASE = 2
"""Roadmap phase of this build. Features from later phases are rejected (section 16)."""

COST_FEATURE_PHASE = 4

Category = Literal[
    "implement",
    "bugfix",
    "refactor",
    "test",
    "review",
    "debug",
    "migrate",
    "docs-for-code",
    "codebase-research",
    "architecture",
]
Complexity = Literal["low", "medium", "high"]
PermissionLevel = Literal["full", "workspace", "readonly"]
Access = Literal["read", "write"]
NodeStatus = Literal["done", "failed", "needs_input", "blocked", "cancelled"]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# `.git` and the runtime subtrees, as path segments (section 6, reserved paths).
RESERVED_ROOTS: tuple[tuple[str, ...], ...] = (
    (".git",),
    (".agents", "locks"),
    (".agents", "runs"),
    (".agents", "worktrees"),
    (".agents", "shadow-git"),
    (".agents", "scratch"),
    (".agents", "sessions"),
    (".agents", "apply"),
)


def require_phase(feature: str, phase: int) -> None:
    """Reject a feature that arrives in a later phase with the CLI's standard wording."""
    if phase > CURRENT_PHASE:
        raise ValueError(f"{feature} is available from phase {phase}")


def _check_id(value: str) -> str:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{value!r} is not a path-safe id (letters, digits, '.', '_', '-')")
    if value.split(".")[0].lower() in _WINDOWS_DEVICES:
        raise ValueError(f"{value!r} is a reserved Windows device name")
    return value


def normalize_repo_path(value: str) -> str:
    """Normalise a repository-relative path or glob and reject unsafe forms.

    Empty and `.` segments are dropped; a trailing `/` is kept.
    """
    path = value.strip().replace("\\", "/")
    if "\x00" in path:
        raise ValueError(f"{value!r} must not contain NUL")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"{value!r} must be relative to the repository root")
    segments = [segment for segment in path.split("/") if segment not in ("", ".")]
    if ".." in segments:
        raise ValueError(f"{value!r} must not contain '..'")
    if not segments:
        raise ValueError(f"{value!r} must name a path below the repository root")
    canonical = "/".join(segments)
    return canonical + "/" if path.endswith("/") else canonical


_GENERIC = frozenset({"*", "**"})
# `~` before a digit or glob character may be an 8.3 short name; `:` may be an NTFS stream.
_ALIAS = re.compile(r"~[0-9?*\[]|:")
_TRAILING = ("", ".", " ", "..", ". ", " .", "  ")


def _may_name(segment: str, name: str) -> bool:
    """True if a glob segment can refer to the reserved segment `name` on some host.

    Case is ignored on every host, so a spec validates the same everywhere. Windows also
    resolves trailing dots and spaces, stream suffixes and 8.3 short names to the same file;
    a segment that could be one of those is assumed to match.
    """
    if _ALIAS.search(segment):
        return True
    pattern = re.compile(translate(segment), re.IGNORECASE)
    stripped = segment if segment.endswith("]") else segment.rstrip(". ") or segment
    stripped_pattern = re.compile(translate(stripped), re.IGNORECASE)
    return stripped_pattern.match(name) is not None or any(
        pattern.match(name + suffix) for suffix in _TRAILING
    )


def _reaches(segments: Sequence[str], root: tuple[str, ...], named: bool = False) -> bool:
    """True if `segments` can match `root` or a path inside it with a non-generic segment.

    Only `*` and `**` are generic: they match runtime state by accident, which the
    orchestrator filters out of broad scopes separately (section 6).
    """
    if not root:
        return named
    if not segments:
        return False
    head, rest = segments[0], segments[1:]
    if head == "**":
        return _reaches(rest, root, named) or _reaches(segments, root[1:], named)
    return _may_name(head, root[0]) and _reaches(rest, root[1:], named or head not in _GENERIC)


def intersects_reserved(pattern: str) -> bool:
    """True if a normalised glob names a reserved path or something inside it (sections 6, 8.1).

    `pattern` is the output of `normalize_repo_path`. Scopes made only of `*` and `**`
    segments, such as `**` or `*/hooks/*`, do not name a reserved path and stay legal.
    """
    segments = pattern.rstrip("/").split("/")
    return any(_reaches(segments, root) for root in RESERVED_ROOTS)


def _check_scope_path(value: str) -> str:
    path = normalize_repo_path(value)
    if intersects_reserved(path):
        raise ValueError(f"{value!r} targets reserved runtime paths (.git, .agents/runs, ...)")
    return path


NodeId = Annotated[str, AfterValidator(_check_id)]
RunId = Annotated[str, AfterValidator(_check_id)]
ScopePath = Annotated[str, AfterValidator(_check_scope_path)]
Sha = Annotated[str, Field(min_length=4, max_length=64)]


class SpecModel(BaseModel):
    """Base for all specs: unknown optional fields are preserved (section 5.4)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class VersionedSpec(SpecModel):
    spec_version: int = SPEC_VERSION

    @field_validator("spec_version")
    @classmethod
    def _supported(cls, value: int) -> int:
        if value > SPEC_VERSION:
            raise ValueError(
                f"spec_version {value} is newer than the supported major version {SPEC_VERSION}"
            )
        if value < 1:
            raise ValueError("spec_version must be 1 or greater")
        return value
