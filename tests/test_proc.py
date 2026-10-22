"""Process supervision: spawn, capture, timeout and whole-tree kill (task 1.7)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from relay.core.platform.proc import run_process

WINDOWS = sys.platform == "win32"
PY = sys.executable


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check that treats zombies as dead (mirrors `tests/test_locks.py`)."""
    if WINDOWS:
        import ctypes

        # `getattr` instead of `ctypes.WinDLL`: the Windows-only attribute is absent from the
        # Linux typeshed, so strict pyright reports direct access as unknown.
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
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
    try:
        return "Z" not in Path(f"/proc/{pid}/stat").read_text().split(") ")[-1][:2]
    except OSError:
        return True


def wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not pid_alive(pid)


def run(code: str, *, stdin: str | None = None, timeout_s: float | None = None):
    return run_process(
        [PY, "-c", code],
        cwd=Path.cwd(),
        env=dict(os.environ),
        stdin=stdin,
        timeout_s=timeout_s,
    )


# --- spawn and capture ------------------------------------------------------------------------


def test_run_process_captures_stdout_stderr_and_exit_code() -> None:
    result = run("import sys; print('out'); print('err', file=sys.stderr)")
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.duration_s >= 0


def test_run_process_reports_a_nonzero_exit() -> None:
    result = run("import sys; sys.exit(3)")
    assert result.exit_code == 3
    assert result.timed_out is False


def test_run_process_passes_argv_verbatim_without_a_shell() -> None:
    """An argument containing a quote and a newline must survive exactly (no shell mangling)."""
    arg = 'a"b\nc d'
    result = run_process(
        [PY, "-c", "import sys; sys.stdout.write(repr(sys.argv[1:]))", arg],
        cwd=Path.cwd(),
        env=dict(os.environ),
    )
    assert result.stdout == repr([arg])


def test_run_process_passes_stdin_verbatim() -> None:
    """A prompt with quotes and newlines round-trips through stdin unchanged."""
    prompt = 'line one\nline "two"\n\n'
    result = run("import sys; sys.stdout.write(repr(sys.stdin.read()))", stdin=prompt)
    assert result.exit_code == 0
    assert result.stdout == repr(prompt)


def test_run_process_missing_executable_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_process(
            [str(tmp_path / "does-not-exist")],
            cwd=tmp_path,
            env=dict(os.environ),
        )


# --- timeout and tree kill ---------------------------------------------------------------------

SPAWN_GRANDCHILD = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "print(g.pid, flush=True)\n"
    "time.sleep(600)\n"
)


def test_timeout_kills_the_whole_process_tree() -> None:
    result = run(SPAWN_GRANDCHILD, timeout_s=1.0)
    assert result.timed_out
    assert result.exit_code is None
    grandchild = int(result.stdout.strip().splitlines()[-1])
    assert wait_dead(grandchild), "the grandchild survived the timeout"
