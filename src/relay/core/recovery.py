"""Conservative whole-attempt foreground recovery (design section 6)."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from relay.core.platform.procid import IdentityState, ProcessIdentity, check_identity
from relay.core.runrecord import NodeRecord
from relay.core.store import RunStore


def _group_alive(pid: int) -> bool:
    if sys.platform.startswith("linux"):
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_text(errors="replace").rsplit(") ", 1)[1].split()
            except FileNotFoundError:
                continue
            except (OSError, IndexError):
                return True
            if len(fields) < 3:
                return True
            if fields[2] == str(pid) and fields[0] != "Z":
                return True
        return False
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        return True
    try:
        kill_group(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def stop_old_worker(node: NodeRecord) -> bool:
    """Never signal a PID without confirming its process-start identity first."""
    worker = node.worker
    if node.worker_pending or worker is None:
        return not node.worker_pending
    identity = ProcessIdentity(worker.pid, worker.start)
    state = check_identity(identity)
    if state is IdentityState.UNKNOWN:
        return False
    if os.name == "nt":
        # Owner death closes the foreground job handle. Allow its tree to finish terminating.
        deadline = time.monotonic() + 2.0
        while state is IdentityState.ALIVE and time.monotonic() < deadline:
            time.sleep(0.02)
            state = check_identity(identity)
        return state is IdentityState.DEAD
    if state is IdentityState.DEAD:
        return not _group_alive(worker.pid)
    if not sys.platform.startswith("linux"):
        return False
    try:
        if os.getpgid(worker.pid) != worker.pid:
            return False
    except ProcessLookupError:
        return not _group_alive(worker.pid)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(worker.pid, signal.SIGTERM)
    deadline = time.monotonic() + 0.25
    while _group_alive(worker.pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _group_alive(worker.pid):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
    deadline = time.monotonic() + 2.0
    while _group_alive(worker.pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _group_alive(worker.pid)


def prepare_interrupted(store: RunStore, node_id: str) -> bool:
    record = store.read_run()
    node = record.nodes[node_id]
    node.recovery_pending = True
    if node.attempts and node.state == "running" and not node.process_tracking:
        node.worker_pending = True
    if not stop_old_worker(node):
        node.state = "blocked"
        node.blocked_reason = "worker_unconfirmed"
        record.state = "blocked"
        store.write_run(record)
        return False
    if node.attempt_started_at is not None:
        started = datetime.fromisoformat(node.attempt_started_at.replace("Z", "+00:00"))
        elapsed = max(0.0, (datetime.now(UTC) - started).total_seconds())
        node.active_duration_s += (
            min(elapsed, node.attempt_timeout_s) if node.attempt_timeout_s is not None else elapsed
        )
        node.attempt_started_at = None
        node.attempt_timeout_s = None
    node.worker = None
    node.worker_pending = False
    store.write_run(record)
    return True


P = ParamSpec("P")
T = TypeVar("T")


def foreground_interrupts(
    function: Callable[P, Awaitable[T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(function)
    async def execute(*args: P.args, **kwargs: P.kwargs) -> T:
        main_thread = threading.current_thread() is threading.main_thread()
        previous = signal.getsignal(signal.SIGINT) if main_thread else None
        if main_thread:
            signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            return await function(*args, **kwargs)
        finally:
            if main_thread and previous is not None:
                signal.signal(signal.SIGINT, previous)

    return execute
