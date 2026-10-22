"""Lifetime lock behaviour, including the spike questions for design sections 6 and 15.

Everything here runs real processes. The same file runs on Linux (including WSL mounts) so
the supported-platform matrix in the design is backed by executable checks.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from relay.core.platform.locks import LifetimeLock, LockHeld, probe_lock_support

SRC = str(Path(__file__).resolve().parents[1] / "src")
WINDOWS = sys.platform == "win32"
FOREVER = "import time; time.sleep(600)"


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


def pid_alive(pid: int) -> bool:
    if WINDOWS:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; treat it as dead.
    try:
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][:2]
    except OSError:
        return True


def kill(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def acquire_within(path: Path, seconds: float) -> float | None:
    """Seconds until the lock could be taken, or None if it never could."""
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        lock = LifetimeLock(path)
        try:
            lock.acquire()
        except LockHeld:
            time.sleep(0.02)
            continue
        lock.release()
        return time.monotonic() - started
    return None


HOLDER = """
    import os, subprocess, sys, time
    from pathlib import Path
    from relay.core.platform.locks import LifetimeLock

    lock = LifetimeLock(Path(sys.argv[1]))
    lock.acquire()
    print("locked", flush=True)
    mode = sys.argv[2]
    kid = None
    if mode == "default":
        kid = subprocess.Popen([sys.executable, "-c", "@FOREVER@"])
    elif mode == "no_close_fds":
        kid = subprocess.Popen([sys.executable, "-c", "@FOREVER@"], close_fds=False)
    elif mode == "inherit_stdio":
        kid = subprocess.Popen([sys.executable, "-c", "@FOREVER@"], close_fds=False,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elif mode == "fork":
        pid = os.fork()
        if pid == 0:
            time.sleep(600)
            os._exit(0)
        print(pid, flush=True)
    if kid is not None:
        print(kid.pid, flush=True)
    time.sleep(600)
""".replace("@FOREVER@", FOREVER)


# --- basics -----------------------------------------------------------------------------------


def test_acquire_release_reacquire(tmp_path: Path) -> None:
    lock = LifetimeLock(tmp_path / "a.lock")
    assert not lock.held
    lock.acquire()
    assert lock.held
    lock.release()
    lock.release()  # idempotent
    lock.acquire()
    lock.release()


def test_a_second_handle_in_the_same_process_conflicts(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    with LifetimeLock(path), pytest.raises(LockHeld, match="locked by another"):
        LifetimeLock(path).acquire()
    LifetimeLock(path).acquire()  # free again after the context exits


def test_acquiring_twice_through_one_object_is_a_programming_error(tmp_path: Path) -> None:
    lock = LifetimeLock(tmp_path / "a.lock")
    lock.acquire()
    with pytest.raises(RuntimeError, match="already locked"):
        lock.acquire()
    lock.release()


def test_missing_directory_is_a_plain_error_not_lock_held(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        LifetimeLock(tmp_path / "nope" / "a.lock").acquire()


def test_lock_file_stays_readable_while_held(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    path.write_text("diagnostic: pid 42\n")
    with LifetimeLock(path):
        assert path.read_text() == "diagnostic: pid 42\n"  # the locked byte is far past EOF


def test_held_lock_file_deletion_policy(tmp_path: Path) -> None:
    """Windows refuses to delete a held lock file; Unix allows it, so `relay clean` must not."""
    path = tmp_path / "a.lock"
    with LifetimeLock(path):
        if WINDOWS:
            with pytest.raises(PermissionError):
                path.unlink()
        else:
            path.unlink()  # allowed: two contenders could then lock different inodes
            assert not path.exists()


# --- other processes --------------------------------------------------------------------------


def test_another_process_cannot_take_a_held_lock(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    probe = """
        import sys
        from pathlib import Path
        from relay.core.platform.locks import LifetimeLock, LockHeld
        try:
            LifetimeLock(Path(sys.argv[1])).acquire()
            print("acquired")
        except LockHeld:
            print("held")
    """
    with LifetimeLock(path):
        child = spawn(probe, str(path))
        assert readline(child) == "held"
        child.wait()
    child = spawn(probe, str(path))
    assert readline(child) == "acquired"
    child.wait()


def test_the_lock_is_released_when_the_holder_is_killed(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    holder = spawn(HOLDER, str(path), "none")
    try:
        assert readline(holder) == "locked"
        with pytest.raises(LockHeld):
            LifetimeLock(path).acquire()
        holder.kill()  # TerminateProcess / SIGKILL: no cleanup code runs
        holder.wait()
    finally:
        holder.kill()
    lag = acquire_within(path, 5)
    assert lag is not None, "lock was not released after the holder was killed"
    print(f"\nlock reacquired {lag * 1000:.0f} ms after the holder was killed")


@pytest.mark.parametrize("mode", ["default", "no_close_fds", "inherit_stdio"])
def test_workers_started_by_exec_do_not_inherit_the_lock(tmp_path: Path, mode: str) -> None:
    """Design 6: descriptors are not inherited by workers, so a surviving worker cannot keep
    the lock alive after the owner dies."""
    path = tmp_path / "a.lock"
    holder = spawn(HOLDER, str(path), mode)
    worker = 0
    try:
        assert readline(holder) == "locked"
        worker = int(readline(holder))
        assert pid_alive(worker)
        holder.kill()
        holder.wait()
        lag = acquire_within(path, 5)
        assert lag is not None, f"worker ({mode}) kept the lock alive"
        assert pid_alive(worker), "the worker should have outlived its owner for this test"
    finally:
        holder.kill()
        if worker:
            kill(worker)


@pytest.mark.skipif(WINDOWS, reason="fork exists only on Unix")
def test_a_bare_fork_keeps_a_unix_lock_alive_after_the_owner_dies(tmp_path: Path) -> None:
    """The hazard behind 'start workers by exec only': a forked child shares the open file
    description, so flock outlives the owner for as long as the child lives."""
    path = tmp_path / "a.lock"
    holder = spawn(HOLDER, str(path), "fork")
    child = 0
    try:
        assert readline(holder) == "locked"
        child = int(readline(holder))
        holder.kill()
        holder.wait()
        assert acquire_within(path, 0.5) is None  # still held by the forked child
        kill(child)
        assert acquire_within(path, 5) is not None
    finally:
        holder.kill()
        if child:
            kill(child)


RACER = """
    import sys, time
    from pathlib import Path
    from relay.core.platform.locks import LifetimeLock, LockHeld

    start = float(sys.argv[2])
    while time.time() < start:
        pass
    lock = LifetimeLock(Path(sys.argv[1]))
    try:
        lock.acquire()
    except LockHeld:
        print("held", flush=True)
    else:
        print("won", flush=True)
        time.sleep(1.0)  # keep it while the others try
"""


def test_simultaneous_cold_start_has_exactly_one_winner(tmp_path: Path) -> None:
    contenders = 8
    for round_number in range(4):
        path = tmp_path / f"race-{round_number}.lock"
        start = time.time() + 2.5
        procs = [spawn(RACER, str(path), repr(start)) for _ in range(contenders)]
        outcomes = [readline(p) for p in procs]
        for p in procs:
            p.wait(timeout=30)
        assert outcomes.count("won") == 1, outcomes
        assert outcomes.count("held") == contenders - 1, outcomes


# --- filesystem probe (doctor) ----------------------------------------------------------------


def test_probe_reports_support_and_cleans_up(tmp_path: Path) -> None:
    result = probe_lock_support(tmp_path / "locks")
    assert result.supported, result.reason
    assert list((tmp_path / "locks").iterdir()) == []


def test_probe_reports_an_unusable_location(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    result = probe_lock_support(blocker / "sub")  # a directory cannot be created under a file
    assert not result.supported
    assert str(blocker) in result.reason
