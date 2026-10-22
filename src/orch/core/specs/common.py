"""Shared building blocks for the spec models (design section 5)."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

SPEC_VERSION = 1
"""Highest spec major version this build understands (section 5.4)."""

CURRENT_PHASE = 1
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
    """Normalise a repository-relative path or glob and reject unsafe forms."""
    path = value.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if not path or "\x00" in path:
        raise ValueError("path must not be empty")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError(f"{value!r} must be relative to the repository root")
    if ".." in path.split("/"):
        raise ValueError(f"{value!r} must not contain '..'")
    return path


def intersects_reserved(pattern: str) -> bool:
    """True if a glob names a reserved path or something inside it (sections 6 and 8.1).

    A bare ``*`` or ``**`` does not name a path, so whole-repository scopes stay legal;
    the orchestrator filters runtime state out of them separately.
    """
    segments = pattern.split("/")
    for root in RESERVED_ROOTS:
        # `position` equals the segments matched so far: the loop breaks on the first miss.
        for position, segment in enumerate(segments):
            if segment == "**":
                if position > 0:
                    return True
                break
            if position >= len(root) or segment == "*" or not fnmatchcase(root[position], segment):
                break
            if position + 1 == len(root):
                return True
    return False


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
