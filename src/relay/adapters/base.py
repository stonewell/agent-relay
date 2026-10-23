"""Adapter interface (design section 7.1)."""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from relay.core.env import CompleteEnv
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
    full_env: bool = False
    """When True, `env` is the complete child environment and the parent environment is not
    merged in. Adapters normally do not need to set this: an `env` built with
    ``relay.core.env.build_env`` is a ``CompleteEnv``, which ``run_invocation`` always treats
    as complete (fail-closed against a forgotten flag). A plain ``dict`` with
    ``full_env=False`` keeps the legacy merge-with-parent behaviour."""
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

    This returns the list form only; it drops the raw Windows command line carried by
    :func:`relay.core.platform.proc.resolve_command`. ``run_invocation`` uses the richer
    ``resolve_command`` directly so the shim reaches ``cmd.exe`` without a second quoting
    pass.
    """
    return list(resolve_command(cmd, args, env=env, cwd=cwd).argv)


def _write_prompt_file(path: Path, text: str) -> None:
    """Write the prompt atomically with mode 0o600 (run-store atomic-write rule, section 6).

    A temp file in the same directory is written, flushed and fsynced, then ``os.replace``d
    over the target so a reader sees the old or the new bytes, never a partial write. The
    prompt may contain secrets, so the file is created 0o600.
    """
    temp = path.with_name(f".tmp-{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        with open(temp, "xb") as handle:
            os.chmod(temp, 0o600)
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    if os.name != "nt":  # a rename is only durable once its directory is fsynced
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def run_invocation(invocation: Invocation, *, timeout_s: float | None = None) -> ProcessResult:
    """Resolve and supervise one invocation, passing the prompt on stdin or a file (task 1.7).

    The command is resolved with :func:`resolve_command` against the invocation's environment,
    and the prompt text (``invocation.stdin``) is either piped to the child or, when
    ``invocation.prompt_file`` is set, written atomically to that file for the child to read
    from `args`. The child environment is ``invocation.env`` verbatim when ``full_env`` is
    True *or* ``env`` is a :class:`~relay.core.env.CompleteEnv` (a ``build_env`` result);
    otherwise the parent environment is the base and `env` adds to it.
    """
    complete = invocation.full_env or isinstance(invocation.env, CompleteEnv)
    env = dict(invocation.env) if complete else {**os.environ, **invocation.env}
    resolved = resolve_command(invocation.cmd, invocation.args, env=env, cwd=invocation.cwd)
    stdin: str | None = invocation.stdin
    if invocation.prompt_file is not None:
        if invocation.stdin is None:
            raise ValueError("prompt_file requires the prompt text in `stdin`")
        invocation.prompt_file.parent.mkdir(parents=True, exist_ok=True)
        _write_prompt_file(invocation.prompt_file, invocation.stdin)
        stdin = None
    return run_process(resolved, cwd=invocation.cwd, env=env, stdin=stdin, timeout_s=timeout_s)
