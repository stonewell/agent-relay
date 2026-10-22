"""Helpers for running adapter invocations and building temp repositories in tests."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from orch.adapters.base import Invocation, ProcessResult, RunContext
from orch.core.specs import TaskSpec

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout.strip()


def init_repo(path: Path) -> Path:
    """A repository with one commit on `main`."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("# fixture\n", encoding="utf-8", newline="\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    return path


def task(node_id: str = "n1", goal: str = "Do the thing") -> TaskSpec:
    return TaskSpec(id=node_id, run_id="2026-10-22-test", category="implement", goal=goal)


def context(
    tmp: Path, node_id: str = "n1", attempt: int = 1, resume_session: str | None = None
) -> RunContext:
    node_dir = tmp / "node" / node_id
    node_dir.mkdir(parents=True, exist_ok=True)
    worktree = tmp / "worktree"
    worktree.mkdir(exist_ok=True)
    return RunContext(
        run_id="2026-10-22-test",
        node_id=node_id,
        attempt=attempt,
        node_dir=node_dir,
        worktree=worktree,
        resume_session=resume_session,
    )


def run_invocation(invocation: Invocation, timeout_s: float = 30) -> ProcessResult:
    """Run an invocation to completion (the real supervisor arrives with task 1.7)."""
    started = time.monotonic()
    try:
        done = subprocess.run(
            [invocation.cmd, *invocation.args],
            cwd=invocation.cwd,
            input=invocation.stdin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env={**os.environ, **invocation.env},
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessResult(
            exit_code=None,
            stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or "",
            stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or "",
            duration_s=time.monotonic() - started,
            timed_out=True,
        )
    return ProcessResult(
        exit_code=done.returncode,
        stdout=done.stdout,
        stderr=done.stderr,
        duration_s=time.monotonic() - started,
    )
