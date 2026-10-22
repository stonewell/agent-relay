"""Interim run lock: exclusive-create, staleness, and `interrupted` marking (design section 6).

The lock is the file `.agents/locks/<repo-key>-<run-id>.lock`: it is created with exclusive
create semantics, carries the owner's PID and process-start identity, and is removed on normal
release. A crash leaves the file behind, which is the stale-lock case.

The same file runs on Linux and Windows. A few assertions about the process-start marker are
Linux/Windows-specific (macOS cannot read a start marker, so the conservative `UNKNOWN` path
applies there instead) and are skipped elsewhere.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from relay.core import runlock as rl
from relay.core.platform.procid import (
    IdentityState,
    ProcessIdentity,
    check_identity,
    current_identity,
)
from relay.core.runrecord import RunState
from relay.core.store import RunStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
_HAS_START_MARKER = sys.platform in ("linux", "win32")


def pyenv() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    return env


def spawn(code: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=pyenv(),
    )


def readline(proc: subprocess.Popen[str]) -> str:
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line, f"child produced no output; stderr: {proc.stderr.read() if proc.stderr else ''}"
    return line


def make_run(repo: Path, run_id: str = "run1", state: RunState = "running") -> RunStore:
    store = RunStore.create(repo, run_id=run_id)
    if state != "running":
        record = store.read_run()
        record.state = state
        store.write_run(record)
    return store


def write_owner(repo: Path, run_id: str, pid: int, start: int | None) -> None:
    """Plant a lock file naming ``pid``/``start`` as the recorded owner (as a crash would)."""
    path = rl.lock_path(repo, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "start": start}), encoding="utf-8")


def dead_pid() -> int:
    """A pid that belonged to a process which has now exited."""
    child = spawn("import os; print(os.getpid(), flush=True)")
    pid = int(readline(child))
    child.wait()
    return pid


# --- acquire and release ----------------------------------------------------------------------


def test_acquire_creates_the_lock_file_and_release_removes_it(tmp_path: Path) -> None:
    make_run(tmp_path)
    lock = rl.RunLock.acquire(tmp_path, "run1")
    assert lock.held
    assert lock.identity.pid == os.getpid()
    path = rl.lock_path(tmp_path, "run1")
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "pid": os.getpid(),
        "start": lock.identity.start,
    }
    lock.release()
    assert not lock.held
    assert not path.exists()
    assert rl.inspect_owner(tmp_path, "run1").state is rl.OwnerState.ABSENT


def test_second_acquisition_is_refused(tmp_path: Path) -> None:
    make_run(tmp_path)
    first = rl.RunLock.acquire(tmp_path, "run1")
    try:
        with pytest.raises(rl.RunLockHeld):
            rl.RunLock.acquire(tmp_path, "run1")
        # takeover intent never overrides a live owner
        with pytest.raises(rl.RunLockHeld):
            rl.RunLock.acquire(tmp_path, "run1", takeover=True)
    finally:
        first.release()


def test_release_is_idempotent(tmp_path: Path) -> None:
    make_run(tmp_path)
    lock = rl.RunLock.acquire(tmp_path, "run1")
    lock.release()
    lock.release()
    assert not lock.held
    # the lock is free again: a fresh acquire of a *different* run id works
    make_run(tmp_path, "run2")
    other = rl.RunLock.acquire(tmp_path, "run2")
    assert other.held
    other.release()


def test_unreleased_lock_remains_after_a_crash(tmp_path: Path) -> None:
    """A holder that dies without releasing leaves the lock file behind (stale, not removed)."""
    make_run(tmp_path)
    child = spawn(
        """
        import os, sys
        from pathlib import Path
        from relay.core.runlock import RunLock
        RunLock.acquire(Path(sys.argv[1]), sys.argv[2])
        os._exit(0)  # hard crash: no release, no cleanup
        """,
        str(tmp_path),
        "run1",
    )
    child.wait()
    assert rl.lock_path(tmp_path, "run1").exists()
    assert rl.inspect_owner(tmp_path, "run1").state is rl.OwnerState.STALE


# --- staleness --------------------------------------------------------------------------------


def test_stale_lock_detected_after_the_owner_exits(tmp_path: Path) -> None:
    make_run(tmp_path)
    child = spawn(
        """
        import sys
        from pathlib import Path
        from relay.core.runlock import RunLock
        RunLock.acquire(Path(sys.argv[1]), sys.argv[2])
        print("locked", flush=True)
        """,
        str(tmp_path),
        "run1",
    )
    assert readline(child) == "locked"
    child.wait()  # the process exits without releasing: the lock file is left behind
    owner = rl.inspect_owner(tmp_path, "run1")
    assert owner.state is rl.OwnerState.STALE
    assert owner.identity is not None
    assert check_identity(owner.identity) is IdentityState.DEAD


@pytest.mark.skipif(not _HAS_START_MARKER, reason="start marker is not readable on this platform")
def test_pid_reuse_is_not_mistaken_for_the_original_owner(tmp_path: Path) -> None:
    """A live PID with a different start marker (PID reuse) reads as stale, never alive."""
    make_run(tmp_path)
    me = current_identity()
    assert me.start is not None
    write_owner(tmp_path, "run1", me.pid, me.start + 1_000_000)
    owner = rl.inspect_owner(tmp_path, "run1")
    assert owner.state is rl.OwnerState.STALE
    assert owner.identity == ProcessIdentity(pid=me.pid, start=me.start + 1_000_000)
    with pytest.raises(rl.RunLockStale):
        rl.RunLock.acquire(tmp_path, "run1")


def test_unreadable_owner_is_never_taken_over(tmp_path: Path) -> None:
    make_run(tmp_path)
    path = rl.lock_path(tmp_path, "run1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {", encoding="utf-8")
    assert rl.inspect_owner(tmp_path, "run1").state is rl.OwnerState.UNKNOWN
    with pytest.raises(rl.RunLockUnknown):
        rl.RunLock.acquire(tmp_path, "run1")
    with pytest.raises(rl.RunLockUnknown):
        rl.RunLock.acquire(tmp_path, "run1", takeover=True)


def test_empty_lock_file_is_unknown_and_refused(tmp_path: Path) -> None:
    """The create-then-write window leaves an empty file, which is UNKNOWN, never dead."""
    make_run(tmp_path)
    path = rl.lock_path(tmp_path, "run1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert rl.inspect_owner(tmp_path, "run1").state is rl.OwnerState.UNKNOWN
    with pytest.raises(rl.RunLockUnknown):
        rl.RunLock.acquire(tmp_path, "run1")
    with pytest.raises(rl.RunLockUnknown):
        rl.RunLock.acquire(tmp_path, "run1", takeover=True)


def test_partial_lock_file_is_unknown_and_refused(tmp_path: Path) -> None:
    make_run(tmp_path)
    path = rl.lock_path(tmp_path, "run1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pid": 1, "start":', encoding="utf-8")
    assert rl.inspect_owner(tmp_path, "run1").state is rl.OwnerState.UNKNOWN
    with pytest.raises(rl.RunLockUnknown):
        rl.RunLock.acquire(tmp_path, "run1", takeover=True)


# --- takeover ---------------------------------------------------------------------------------


def test_stale_lock_is_reported_not_replaced_without_intent(tmp_path: Path) -> None:
    make_run(tmp_path)
    write_owner(tmp_path, "run1", dead_pid(), 1)
    with pytest.raises(rl.RunLockStale):
        rl.RunLock.acquire(tmp_path, "run1")
    # the stale owner record is untouched and the run state is unchanged
    assert rl.inspect_owner(tmp_path, "run1").state is rl.OwnerState.STALE
    assert RunStore.open(tmp_path, "run1").read_run().state == "running"


def test_stale_lock_is_taken_over_with_retry_resume_intent(tmp_path: Path) -> None:
    make_run(tmp_path)
    write_owner(tmp_path, "run1", dead_pid(), 1)
    lock = rl.RunLock.acquire(tmp_path, "run1", takeover=True)
    try:
        assert lock.held
        owner = rl.inspect_owner(tmp_path, "run1")
        assert owner.identity == lock.identity
        assert owner.state is rl.OwnerState.ALIVE
        assert RunStore.open(tmp_path, "run1").read_run().state == "interrupted"
    finally:
        lock.release()


# --- interrupted marking ----------------------------------------------------------------------


def test_mark_interrupted_flips_running_and_is_idempotent(tmp_path: Path) -> None:
    make_run(tmp_path)
    assert rl.mark_interrupted(tmp_path, "run1") is True
    assert RunStore.open(tmp_path, "run1").read_run().state == "interrupted"
    assert rl.mark_interrupted(tmp_path, "run1") is False


def test_mark_interrupted_leaves_terminal_states_alone(tmp_path: Path) -> None:
    for state in ("done", "failed", "blocked", "cancelled"):
        run_id = f"run-{state}"
        make_run(tmp_path, run_id, state=state)
        assert rl.mark_interrupted(tmp_path, run_id) is False
        assert RunStore.open(tmp_path, run_id).read_run().state == state


def test_mark_interrupted_of_a_missing_run_is_a_noop(tmp_path: Path) -> None:
    assert rl.mark_interrupted(tmp_path, "ghost") is False


# --- processes --------------------------------------------------------------------------------


CONCURRENT = """
    import sys, time
    from pathlib import Path
    from relay.core.runlock import RunLock, RunLockError

    start = float(sys.argv[3])
    while time.time() < start:
        pass
    try:
        lock = RunLock.acquire(Path(sys.argv[1]), sys.argv[2])
    except RunLockError:
        print("held", flush=True)
    else:
        print("won", flush=True)
        time.sleep(0.5)
        lock.release()
"""


def test_concurrent_acquire_from_two_processes(tmp_path: Path) -> None:
    make_run(tmp_path)
    start = time.time() + 2.0
    procs = [spawn(CONCURRENT, str(tmp_path), "run1", repr(start)) for _ in range(2)]
    outcomes = [readline(p) for p in procs]
    for p in procs:
        p.wait(timeout=30)
    assert outcomes.count("won") == 1, outcomes
    assert outcomes.count("held") == 1, outcomes
