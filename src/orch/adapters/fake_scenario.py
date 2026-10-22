"""Scenario file format shared by the `fake` adapter and its stub agent process."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from orch.core.specs.common import NodeStatus

OutputMode = Literal["json", "file", "block", "none"]


class Scenario(BaseModel):
    """What the stub agent does in one attempt. All fields are optional."""

    model_config = ConfigDict(extra="forbid")

    write: dict[str, str] = {}
    """Files to create or overwrite, relative to the worktree."""
    delete: list[str] = []
    commit: str | None = None
    """Commit message: the agent stages everything and commits (a self-committing agent)."""
    stdout: str = ""
    stderr: str = ""
    output: OutputMode = "json"
    """`json`: native structured output; `file`: result.spec.yaml in the node dir;
    `block`: fenced JSON block in stdout; `none`: no result at all (synthesized candidate)."""
    status: NodeStatus = "done"
    summary: str = "fake agent finished"
    decisions: list[str] = []
    questions: list[dict[str, Any]] = Field(default=[])
    """Typed open questions; with `status: needs_input` they make the attempt block."""
    session_id: str | None = None
    exit_code: int = 0
    sleep_s: float = Field(default=0.0, ge=0)
    hang: bool = False
    """Sleep until killed, after the file changes and before any output."""
    echo_env: list[str] = []
    """Environment variable names whose values are recorded in the observation file."""
    usage: dict[str, int] = {}
    malformed_result: bool = False
    """Emit a result that fails schema validation, to exercise the repair path."""
