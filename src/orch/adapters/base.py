"""Adapter interface (design section 7.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from orch.core.specs import ResultSpec, TaskSpec
from orch.core.specs.common import PermissionLevel


@dataclass(frozen=True)
class Probe:
    available: bool
    version: str | None = None
    levels: tuple[PermissionLevel, ...] = ("full",)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunContext:
    """What an adapter needs besides the task spec."""

    run_id: str
    node_id: str
    attempt: int
    node_dir: Path
    worktree: Path
    permission: PermissionLevel = "full"
    resume_session: str | None = None


@dataclass(frozen=True)
class Invocation:
    cmd: str
    args: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(default_factory=lambda: {})
    stdin: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False


class WorkerHandle(Protocol):
    """A running worker process, supervised by the orchestrator."""

    def kill_tree(self) -> None: ...


class AgentAdapter(Protocol):
    id: str

    async def probe(self) -> Probe: ...

    def build_invocation(self, task: TaskSpec, ctx: RunContext) -> Invocation: ...

    async def parse_result(
        self, proc: ProcessResult, task: TaskSpec, ctx: RunContext
    ) -> ResultSpec: ...

    async def cancel(self, handle: WorkerHandle) -> None: ...
