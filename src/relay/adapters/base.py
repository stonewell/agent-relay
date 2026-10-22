"""Adapter interface (design section 7.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from relay.core.specs import ResultSpec, TaskSpec
from relay.core.specs.common import PermissionLevel
from relay.core.store import replace_file


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


RESULT_FILE = "result.spec.yaml"


def archive_previous_result(ctx: RunContext) -> Path | None:
    """Move an earlier attempt's node-dir result to `attempts/NN/` (design section 8.2).

    Called through `start_attempt`, so the parsing order of section 7.3 only ever sees the
    current attempt's file while earlier ones (for example a `needs_input` question set) stay
    inspectable. An archived file is never overwritten: a second file for the same attempt
    (an attempt re-entered after a crash) gets a numbered name next to it.
    """
    current = ctx.node_dir / RESULT_FILE
    if ctx.attempt <= 1 or not current.is_file():
        return None
    directory = ctx.node_dir / "attempts" / f"{ctx.attempt - 1:02d}"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / RESULT_FILE
    copy = 1
    while target.exists():
        copy += 1
        target = directory / f"result-{copy}.spec.yaml"
    replace_file(current, target)
    return target


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


def start_attempt(adapter: AgentAdapter, task: TaskSpec, ctx: RunContext) -> Invocation:
    """The orchestrator's single entry point for starting an attempt with any adapter."""
    archive_previous_result(ctx)
    return adapter.build_invocation(task, ctx)
