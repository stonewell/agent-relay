"""Adapter interface (design section 7.1)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from relay.core.platform.proc import (
    CommandNotFound,
    ProcessResult,
    resolve_command,
    run_process,
)
from relay.core.specs import ResultSpec, TaskSpec
from relay.core.specs.common import PermissionLevel
from relay.core.store import replace_file

__all__ = [
    "AgentAdapter",
    "CommandNotFound",
    "Invocation",
    "Probe",
    "ProcessResult",
    "RESULT_FILE",
    "RunContext",
    "WorkerHandle",
    "archive_previous_result",
    "resolveCommand",
    "run_invocation",
    "start_attempt",
]


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
    prompt_file: Path | None = None
    """When set, the prompt text in `stdin` is written to this file instead of being piped to
    the child's stdin. The adapter must reference the same path in `args`."""


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


def resolveCommand(
    cmd: str,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> list[str]:
    """Resolve a CLI command to a concrete argv, never through a shell (design section 7.2).

    Handles explicit paths, ``PATH`` (and ``PATHEXT`` on Windows) lookup, and Windows
    ``.cmd``/``.bat`` shims, which are wrapped through ``cmd.exe /d /s /c``. Raises
    :class:`CommandNotFound` when the command cannot be resolved.
    """
    return resolve_command(cmd, args, env=env, cwd=cwd)


def run_invocation(invocation: Invocation, *, timeout_s: float | None = None) -> ProcessResult:
    """Resolve and supervise one invocation, passing the prompt on stdin or a file (task 1.7).

    The command is resolved with :func:`resolveCommand` against the invocation's environment,
    and the prompt text (``invocation.stdin``) is either piped to the child or, when
    ``invocation.prompt_file`` is set, written to that file for the child to read from `args`.
    """
    env = {**os.environ, **invocation.env}
    argv = resolveCommand(invocation.cmd, invocation.args, env=env, cwd=invocation.cwd)
    stdin: str | None = invocation.stdin
    if invocation.prompt_file is not None:
        if invocation.stdin is None:
            raise ValueError("prompt_file requires the prompt text in `stdin`")
        invocation.prompt_file.parent.mkdir(parents=True, exist_ok=True)
        invocation.prompt_file.write_text(invocation.stdin, encoding="utf-8", newline="")
        stdin = None
    return run_process(argv, cwd=invocation.cwd, env=env, stdin=stdin, timeout_s=timeout_s)
