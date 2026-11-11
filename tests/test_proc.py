"""Process supervision: spawn, capture, timeout and whole-tree kill (task 1.7)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from relay.core.platform import proc
from relay.core.platform.proc import (
    CommandNotFound,
    ProcessCancelled,
    ProcessResult,
    ProcessStalled,
    ResolvedCommand,
    cancel_processes_when,
    resolve_command,
    run_process,
)

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
    except FileNotFoundError:
        # The entry vanished between the signal check and the stat read: the process is gone.
        return False
    except OSError:
        # Unreadable for another reason (e.g. hidepid): fall back to the signal probe.
        return True


def wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
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


@pytest.mark.skipif(WINDOWS, reason="POSIX process-group signal escalation")
def test_timeout_kills_stubborn_descendant_after_parent_exits() -> None:
    child_code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(600)"
    )
    code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(600)\n"
    )
    result = run(code, timeout_s=1)
    assert result.timed_out
    grandchild = int(next(line for line in result.stdout.splitlines() if line.isdigit()))
    assert wait_dead(grandchild)


def test_descendant_holding_the_pipes_does_not_hang_a_finished_run() -> None:
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        "print('done', flush=True)\n"
    )
    started = time.monotonic()
    result = run(code, timeout_s=60)
    assert time.monotonic() - started < 20
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.stdout == "done\n"


def test_timeout_is_enforced_when_the_child_never_reads_stdin() -> None:
    started = time.monotonic()
    result = run("import time; time.sleep(600)", stdin="x" * (4 * 1024 * 1024), timeout_s=1.0)
    assert result.timed_out
    assert time.monotonic() - started < 20


@pytest.mark.skipif(
    WINDOWS or proc._waitid is None,  # pyright: ignore[reportPrivateUsage]
    reason="POSIX non-reaping leader wait",
)
def test_group_is_never_signalled_after_the_leader_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = proc._killpg  # pyright: ignore[reportPrivateUsage]
    assert real is not None
    calls: list[int | None] = []

    def record(pid: int, sig: int) -> None:
        calls.append(process.returncode)
        real(pid, sig)

    monkeypatch.setattr(proc, "_killpg", record)
    process = proc._spawn([PY, "-c", "pass"], cwd=tmp_path, env=dict(os.environ))  # pyright: ignore[reportPrivateUsage]
    result = process.run()
    assert result.exit_code == 0
    assert calls and all(code is None for code in calls)  # swept before the reap
    calls.clear()
    process.kill_tree()  # a late kill must not reach a group ID that may be reused
    assert calls == []


# --- diagnostics logging -----------------------------------------------------------------------


def silent_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "uvicorn.error" and record.getMessage().startswith("cli silent ")
    ]


def test_a_silent_child_logs_cli_silent_with_growing_silence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(proc, "SILENCE_LOG_INTERVAL_S", 0.2)
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = run("import time; time.sleep(1.2)")
    assert result.exit_code == 0
    lines = silent_lines(caplog)
    assert len(lines) >= 2
    since = [float(line.split("since_output_s=")[1].split()[0]) for line in lines]
    assert all(earlier < later for earlier, later in zip(since, since[1:], strict=False))
    assert all("stdout_bytes=0 stderr_bytes=0" in line for line in lines)
    exits = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cli exit ")]
    assert len(exits) == 1
    assert "code=0 timed_out=False" in exits[0]
    assert "first_output_s=None" in exits[0]


def test_a_chatty_child_logs_no_cli_silent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The interval leaves room for interpreter start-up before the first line.
    monkeypatch.setattr(proc, "SILENCE_LOG_INTERVAL_S", 0.5)
    code = "import time\nfor i in range(30):\n    print(i, flush=True)\n    time.sleep(0.05)\n"
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = run(code)
    assert result.exit_code == 0
    assert silent_lines(caplog) == []
    exits = [r for r in caplog.records if r.getMessage().startswith("cli exit ")]
    assert len(exits) == 1 and exits[0].levelno == logging.INFO
    assert "first_output_s=None" not in exits[0].getMessage()


def test_cli_exit_logs_flags_only_and_a_redacted_stderr_tail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "sk-fake-relay-secret-0123456789"
    code = (
        "import os, sys\n"
        "sys.stdin.read()\n"
        "print('STDOUT_SENTINEL')\n"
        "sys.stderr.write('fatal: key=' + os.environ['RELAY_TEST_FAKE_KEY'] + '\\n')\n"
        "sys.exit(2)\n"
    )
    argv = [PY, "-c", code, "--relay-flag", "--opt=VALUE_SENTINEL", "ARG_SENTINEL"]
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = run_process(
            argv,
            cwd=Path.cwd(),
            env={**os.environ, "RELAY_TEST_FAKE_KEY": secret},
            stdin="STDIN_SENTINEL",
        )
    assert result.exit_code == 2
    assert "STDOUT_SENTINEL" in result.stdout
    exits = [r for r in caplog.records if r.getMessage().startswith("cli exit ")]
    assert len(exits) == 1
    message = exits[0].getMessage()
    assert exits[0].levelno == logging.WARNING
    assert "code=2 timed_out=False" in message
    assert "flags=-c --relay-flag --opt" in message
    assert "fatal: key=<redacted>" in message
    for leaked in (secret, "VALUE_SENTINEL", "ARG_SENTINEL", "STDOUT_SENTINEL", "STDIN_SENTINEL"):
        assert leaked not in caplog.text


def test_option_names_show_config_keys_in_every_form_but_never_values() -> None:
    argv = [
        "agy",
        "-c",
        "model=V1",
        "-c=sandbox=V2",
        "-capproval_policy=V3",
        "--config",
        "a.b=V4",
        "--config=x_y=V5",
        "-c",
        "not a key",
        "--opt=V6",
        "ARG",
    ]
    names = proc._option_names(argv)  # pyright: ignore[reportPrivateUsage]
    assert names == (
        "-c model",
        "-c sandbox",
        "-c approval_policy",
        "--config a.b",
        "--config x_y",
        "-c",
        "--opt",
    )
    assert not any(value in " ".join(names) for value in ("V1", "V2", "V3", "V4", "V5", "V6"))


def test_cli_exit_is_logged_for_a_timeout(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        result = run("import time; time.sleep(600)", timeout_s=0.5)
    assert result.timed_out
    exits = [r for r in caplog.records if r.getMessage().startswith("cli exit ")]
    assert len(exits) == 1
    assert exits[0].levelno == logging.WARNING
    assert "code=None timed_out=True" in exits[0].getMessage()


# --- no-output watchdog ------------------------------------------------------------------------

# Writes a grandchild's PID to argv[1] (output would be lost when the watchdog raises),
# optionally prints one line, then stays silent.
GRANDCHILD_PID_FILE = (
    "import subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "with open(sys.argv[1], 'w') as f:\n"
    "    f.write(str(g.pid))\n"
    "if sys.argv[2] == 'speak':\n"
    "    print('started', flush=True)\n"
    "time.sleep(600)\n"
)


def watched(
    argv: list[str],
    *,
    stdin: str | None = None,
    first: float | None = None,
    idle: float | None = None,
) -> ProcessResult:
    return run_process(
        argv,
        cwd=Path.cwd(),
        env=dict(os.environ),
        stdin=stdin,
        timeout_s=60.0,
        first_output_timeout_s=first,
        idle_timeout_s=idle,
    )


def read_pid(path: Path) -> int:
    deadline = time.monotonic() + 5.0
    while not path.exists() or not path.read_text():
        assert time.monotonic() < deadline, "the child never reported its grandchild"
        time.sleep(0.02)
    return int(path.read_text())


def test_first_output_limit_kills_a_child_that_never_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = time.monotonic()
    with (
        caplog.at_level(logging.INFO, logger="uvicorn.error"),
        pytest.raises(ProcessStalled) as caught,
    ):
        watched([PY, "-c", "import time; time.sleep(600)"], first=0.5, idle=0.2)
    assert time.monotonic() - started < 20
    assert caught.value.limit == "first-output"
    assert caught.value.limit_s == 0.5 and caught.value.silent_s >= 0.5
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("cli stalled limit=first-output limit_s=0.5") for m in messages)
    exits = [m for m in messages if m.startswith("cli exit ")]
    assert len(exits) == 1 and "error=ProcessStalled" in exits[0]


def test_idle_limit_kills_the_tree_after_output_stops(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    with (
        caplog.at_level(logging.WARNING, logger="uvicorn.error"),
        pytest.raises(ProcessStalled) as caught,
    ):
        watched([PY, "-c", GRANDCHILD_PID_FILE, str(pid_file), "speak"], first=30.0, idle=0.5)
    assert time.monotonic() - started < 20
    assert caught.value.limit == "idle" and caught.value.silent_s >= 0.5
    assert "cli stalled limit=idle" in caplog.text
    assert wait_dead(read_pid(pid_file)), "the grandchild survived the idle kill"


def test_slow_but_steady_output_is_not_killed() -> None:
    # 1.5 s in total, past the idle limit, but never silent for longer than 0.05 s (16x margin).
    code = "import time\nfor i in range(30):\n    print(i, flush=True)\n    time.sleep(0.05)\n"
    result = watched([PY, "-c", code], first=10.0, idle=0.8)
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout.split() == [str(i) for i in range(30)]


def test_stderr_output_counts_as_liveness() -> None:
    # 1.2 s of stderr-only output, past the idle limit, with 0.05 s gaps (16x margin).
    code = (
        "import sys, time\nfor i in range(24):\n    print(i, file=sys.stderr, flush=True)\n"
        "    time.sleep(0.05)\nprint('done')\n"
    )
    result = watched([PY, "-c", code], first=10.0, idle=0.8)
    assert result.exit_code == 0 and result.stdout == "done\n"


def test_output_racing_the_stall_decision_is_a_finished_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child writes and exits in the poll where the limit expires: its result stands."""
    real_exited = proc._leader_exited  # pyright: ignore[reportPrivateUsage]
    real_pump = proc._stream_pump  # pyright: ignore[reportPrivateUsage]
    real_kill = proc.SupervisedProcess.kill_tree
    killed = threading.Event()
    hidden = [False]

    def exited(popen: subprocess.Popen[str]) -> bool:
        if not hidden[0]:
            # The loop's first check runs after the child exited but reports it alive.
            hidden[0] = True
            deadline = time.monotonic() + 15.0
            while not real_exited(popen) and time.monotonic() < deadline:
                time.sleep(0.01)
            return False
        return real_exited(popen)

    def kill_tree(self: proc.SupervisedProcess) -> None:
        killed.set()
        real_kill(self)

    def pump(*args: Any) -> None:
        killed.wait(15.0)  # count no bytes until the watchdog has decided to kill
        real_pump(*args)

    monkeypatch.setattr(proc, "_leader_exited", exited)
    monkeypatch.setattr(proc, "_stream_pump", pump)
    monkeypatch.setattr(proc.SupervisedProcess, "kill_tree", kill_tree)
    result = watched([PY, "-c", "print('done')"], first=0.01)
    assert killed.is_set()  # the first-output limit did fire
    assert result.exit_code == 0 and not result.timed_out
    assert result.stdout == "done\n"


def test_a_stall_without_late_output_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exited leader alone does not cancel the stall: bytes must have arrived late."""
    real_exited = proc._leader_exited  # pyright: ignore[reportPrivateUsage]
    hidden = [False]

    def exited(popen: subprocess.Popen[str]) -> bool:
        if not hidden[0]:
            hidden[0] = True
            deadline = time.monotonic() + 15.0
            while not real_exited(popen) and time.monotonic() < deadline:
                time.sleep(0.01)
            return False
        return real_exited(popen)

    monkeypatch.setattr(proc, "_leader_exited", exited)
    with pytest.raises(ProcessStalled):
        watched([PY, "-c", "pass"], first=0.01)


def test_a_child_that_reads_all_stdin_before_replying_gets_the_whole_prompt() -> None:
    # ~500 KB is far beyond any pipe buffer: the feed thread must keep writing while the
    # child reads, and the silent read phase must stay inside the first-output limit.
    prompt = "".join(f"filler text for a long prompt, line {i}\n" for i in range(13_000))
    assert len(prompt.encode("utf-8")) > 500_000
    code = "import sys\ndata = sys.stdin.read()\nprint(len(data), data[-12:], sep='|')\n"
    result = watched([PY, "-c", code], stdin=prompt, first=30.0, idle=30.0)
    assert result.exit_code == 0
    assert result.stdout == f"{len(prompt)}|{prompt[-12:]}\n"


def test_cancel_during_silence_kills_the_child_tree(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    event = threading.Event()
    done = threading.Event()

    def cancel_once_reported() -> None:
        # Cancel only after the child has reported its grandchild, however slow the spawn.
        deadline = time.monotonic() + 15.0
        try:
            while not done.is_set() and time.monotonic() < deadline:
                if pid_file.exists() and pid_file.read_text():
                    break
                time.sleep(0.02)
        finally:
            event.set()

    canceller = threading.Thread(target=cancel_once_reported, daemon=True)
    canceller.start()
    started = time.monotonic()
    try:
        with cancel_processes_when(event), pytest.raises(ProcessCancelled):
            watched([PY, "-c", GRANDCHILD_PID_FILE, str(pid_file), "quiet"], first=30, idle=30)
    finally:
        done.set()
        canceller.join(timeout=5.0)
    assert time.monotonic() - started < 20
    assert wait_dead(read_pid(pid_file)), "the grandchild survived cancellation"


def test_cancel_during_stdin_feed_stops_promptly() -> None:
    event = threading.Event()
    timer = threading.Timer(0.5, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with cancel_processes_when(event), pytest.raises(ProcessCancelled):
            watched(
                [PY, "-c", "import time; time.sleep(600)"],
                stdin="x" * (4 * 1024 * 1024),
                first=30.0,
                idle=30.0,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 20


# --- command resolution ------------------------------------------------------------------------


def shim_dir(tmp_path: Path) -> Path:
    (tmp_path / "claude").write_text("#!/bin/sh\n")  # npm's POSIX-sh shim
    (tmp_path / "claude.cmd").write_text("@echo off\r\n")
    return tmp_path


def test_windows_resolution_skips_an_extensionless_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "IS_WINDOWS", True)
    directory = shim_dir(tmp_path)
    env = {"PATH": str(directory), "PATHEXT": ".EXE;.CMD", "COMSPEC": "cmd.exe"}
    for cmd in ("claude", str(directory / "claude")):
        resolved = resolve_command(cmd, ("-p",), env=env, cwd=tmp_path)
        assert resolved.argv[0] == "cmd.exe"
        assert resolved.windows_command_line is not None
        assert "claude.cmd" in resolved.windows_command_line.lower()


def test_windows_resolution_rejects_an_extensionless_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "IS_WINDOWS", True)
    (tmp_path / "claude").write_text("#!/bin/sh\n")
    with pytest.raises(CommandNotFound):
        resolve_command("claude", env={"PATH": str(tmp_path), "PATHEXT": ".EXE;.CMD"})


def test_posix_resolution_uses_the_extensionless_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proc, "IS_WINDOWS", False)
    directory = shim_dir(tmp_path)
    (directory / "claude").chmod(0o755)
    resolved = resolve_command("claude", ("-p",), env={"PATH": str(directory)}, cwd=tmp_path)
    assert resolved == ResolvedCommand(argv=(str(directory / "claude"), "-p"))
