"""Process supervision and command resolution (design sections 7.1, 7.2, 15).

The adapter layer resolves a CLI command to a concrete argv with :func:`resolve_command`
(PATH and PATHEXT lookup, plus Windows ``.cmd``/``.bat`` shim wrapping), then
:func:`run_process` spawns that argv with the resolved environment and stdin, captures
stdout/stderr, enforces a timeout, and kills the whole process tree. No shell is ever used.

Tree kill: on POSIX the child is started in its own session, so ``killpg`` reaches every
process in the tree (``SIGTERM``, a short grace period, then ``SIGKILL``); on Windows the
child is assigned to a kill-on-close Job Object through ``ctypes`` and the tree is killed
with ``TerminateJobObject`` (design section 15).

This module imports nothing from the rest of the package, and its platform-specific imports
are kept inside the ``sys.platform == "win32"`` branch so it imports on both OSes.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
_CMD_EXTENSIONS = frozenset({".cmd", ".bat"})
_TERM_GRACE = 0.2  # seconds between SIGTERM and SIGKILL on POSIX

# `os.killpg` does not exist on Windows; fall back to killing the direct child.
_killpg = getattr(os, "killpg", None)


class CommandNotFound(FileNotFoundError):
    """A command could not be resolved to an executable (design section 7.2)."""


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of one supervised process, in the form adapters parse (design section 7.1)."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False


def _split_pathext(pathext: str) -> list[str]:
    """Normalise a ``PATHEXT`` value into a dotted-extension list (Windows)."""
    extensions: list[str] = []
    for entry in pathext.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.startswith("."):
            entry = "." + entry
        extensions.append(entry)
    return extensions


def _has_extension(name: str) -> bool:
    return os.path.splitext(name)[1] != ""


def _iter_candidates(
    cmd: str, path_value: str, extensions: Sequence[str], explicit: bool
) -> Iterator[str]:
    if explicit:
        yield cmd
        if extensions and not _has_extension(cmd):
            yield from (cmd + ext for ext in extensions)
        return
    for directory in path_value.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, cmd)
        yield candidate
        if extensions and not _has_extension(cmd):
            yield from (candidate + ext for ext in extensions)


def _wrap_cmd_shim(shim: str, args: Sequence[str], env: Mapping[str, str]) -> list[str]:
    """Wrap a ``.cmd``/``.bat`` shim so it runs through ``cmd.exe`` (design section 7.2).

    ``subprocess.list2cmdline`` quotes each argument for the Windows command line, and the
    whole line is wrapped in one extra pair of quotes that ``cmd.exe /s`` strips, preserving
    the inner quoting.
    """
    comspec = env.get("COMSPEC", "cmd.exe")
    command_line = subprocess.list2cmdline([shim, *args])
    return [comspec, "/d", "/s", "/c", f'"{command_line}"']


def resolve_command(
    cmd: str,
    args: Sequence[str] = (),
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | str | None = None,
) -> list[str]:
    """Resolve `cmd` to a concrete executable and return the full argv (never via a shell).

    - A command containing a path separator is an explicit path; it must be a file and, on
      POSIX, executable.
    - Otherwise the command is looked up on ``PATH`` and, on Windows, ``PATHEXT``.
    - On Windows a resolved ``.cmd``/``.bat`` file is wrapped through ``cmd.exe /d /s /c``.

    Raises :class:`CommandNotFound` when nothing resolves. `args` are appended verbatim to
    the resolved executable (or folded into the shim command line).
    """
    if not cmd:
        raise CommandNotFound("command is empty")
    effective = dict(os.environ) if env is None else dict(env)
    path_value = effective.get("PATH", "")
    base = Path(cwd) if cwd is not None else Path.cwd()
    extensions: tuple[str, ...] = ()
    if IS_WINDOWS:
        extensions = tuple(_split_pathext(effective.get("PATHEXT", _DEFAULT_PATHEXT)))
    explicit = os.sep in cmd or (os.altsep is not None and os.altsep in cmd)

    tried: list[str] = []
    for candidate in _iter_candidates(cmd, path_value, extensions, explicit):
        path = Path(candidate)
        if not path.is_absolute():
            path = base / path
        tried.append(str(path))
        if not path.is_file():
            continue
        if not IS_WINDOWS and not os.access(path, os.X_OK):
            raise CommandNotFound(f"{path} is not executable")
        resolved = str(path)
        if IS_WINDOWS and os.path.splitext(resolved)[1].lower() in _CMD_EXTENSIONS:
            return _wrap_cmd_shim(resolved, args, effective)
        return [resolved, *args]

    tried_note = f" (tried: {', '.join(tried)})" if tried else ""
    raise CommandNotFound(f"command {cmd!r} not found{tried_note}")


# --- Windows Job Object tree kill (lazy ctypes import) -----------------------------------------

if sys.platform == "win32":
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    _kernel32 = _ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(_ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes protocol
            ("ReadOperationCount", _ctypes.c_ulonglong),
            ("WriteOperationCount", _ctypes.c_ulonglong),
            ("OtherOperationCount", _ctypes.c_ulonglong),
            ("ReadTransferCount", _ctypes.c_ulonglong),
            ("WriteTransferCount", _ctypes.c_ulonglong),
            ("OtherTransferCount", _ctypes.c_ulonglong),
        ]

    class _JOB_BASIC_LIMIT(_ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes protocol
            ("PerProcessUserTimeLimit", _ctypes.c_longlong),
            ("PerJobUserTimeLimit", _ctypes.c_longlong),
            ("LimitFlags", _wintypes.DWORD),
            ("MinimumWorkingSetSize", _ctypes.c_size_t),
            ("MaximumWorkingSetSize", _ctypes.c_size_t),
            ("ActiveProcessLimit", _wintypes.DWORD),
            ("Affinity", _ctypes.c_size_t),
            ("PriorityClass", _wintypes.DWORD),
            ("SchedulingClass", _wintypes.DWORD),
        ]

    class _JOB_EXTENDED_LIMIT(_ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes protocol
            ("BasicLimitInformation", _JOB_BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", _ctypes.c_size_t),
            ("JobMemoryLimit", _ctypes.c_size_t),
            ("PeakProcessMemoryUsed", _ctypes.c_size_t),
            ("PeakJobMemoryUsed", _ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [_ctypes.c_void_p, _wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = _wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        _wintypes.HANDLE,
        _ctypes.c_int,
        _ctypes.c_void_p,
        _wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = _wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [_wintypes.HANDLE, _wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = _wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [_wintypes.DWORD, _wintypes.BOOL, _wintypes.DWORD]
    _kernel32.OpenProcess.restype = _wintypes.HANDLE
    _kernel32.TerminateJobObject.argtypes = [_wintypes.HANDLE, _wintypes.UINT]
    _kernel32.TerminateJobObject.restype = _wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [_wintypes.HANDLE]
    _kernel32.CloseHandle.restype = _wintypes.BOOL

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    def _assign_to_job(pid: int) -> int | None:
        """Put `pid` in a new kill-on-close Job Object; returns the handle, or None on failure."""
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _JOB_EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            _ctypes.byref(info),
            _ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(handle)
            return None
        child = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not child:
            _kernel32.CloseHandle(handle)
            return None
        assigned = _kernel32.AssignProcessToJobObject(handle, child)
        _kernel32.CloseHandle(child)
        if not assigned:
            _kernel32.CloseHandle(handle)
            return None
        return int(handle)

    def _terminate_job(handle: int) -> None:
        _kernel32.TerminateJobObject(handle, 1)

    def _close_job(handle: int | None) -> None:
        if handle is not None:
            _kernel32.CloseHandle(handle)

else:

    def _assign_to_job(pid: int) -> int | None:
        return None

    def _terminate_job(handle: int) -> None:
        return None

    def _close_job(handle: int | None) -> None:
        return None


def _wait_exit(popen: subprocess.Popen[str], grace: float) -> None:
    deadline = time.monotonic() + grace
    while popen.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)


def _posix_killer(popen: subprocess.Popen[str]) -> Callable[[], None]:
    def kill_tree() -> None:
        if popen.returncode is not None:
            return
        if _killpg is None:
            with contextlib.suppress(OSError):
                popen.kill()
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            _killpg(popen.pid, signal.SIGTERM)
        _wait_exit(popen, _TERM_GRACE)
        if popen.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                _killpg(popen.pid, signal.SIGKILL)

    return kill_tree


def _windows_killer(popen: subprocess.Popen[str], job: int | None) -> Callable[[], None]:
    def kill_tree() -> None:
        if popen.returncode is not None:
            return
        if job is not None:
            _terminate_job(job)
        else:
            # Job assignment failed (e.g. nested-job limits): kill the direct child only.
            with contextlib.suppress(OSError):
                popen.terminate()

    return kill_tree


class SupervisedProcess:
    """A spawned process plus the handle that kills its whole tree (design section 7.1).

    Implements the ``WorkerHandle`` protocol used by ``AgentAdapter.cancel``.
    """

    def __init__(
        self,
        popen: subprocess.Popen[str],
        kill_tree: Callable[[], None],
        close: Callable[[], None],
    ) -> None:
        self._popen = popen
        self._kill_tree = kill_tree
        self._close = close
        self._started = time.monotonic()

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def poll(self) -> int | None:
        return self._popen.poll()

    def kill_tree(self) -> None:
        """Terminate the whole tree (TERM then KILL on POSIX; TerminateJobObject on Windows)."""
        self._kill_tree()

    def run(self, stdin: str | None = None, timeout_s: float | None = None) -> ProcessResult:
        """Drive the child to completion, capturing output and enforcing `timeout_s`."""
        timed_out = False
        stdout = ""
        stderr = ""
        try:
            try:
                stdout, stderr = self._popen.communicate(input=stdin, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill_tree()
                stdout, stderr = self._popen.communicate()
        finally:
            self._close()
        return ProcessResult(
            exit_code=None if timed_out else self.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
            duration_s=time.monotonic() - self._started,
            timed_out=timed_out,
        )


def _spawn(argv: Sequence[str], *, cwd: Path | str, env: Mapping[str, str]) -> SupervisedProcess:
    args = [str(arg) for arg in argv]
    environment = {str(key): str(value) for key, value in env.items()}
    if IS_WINDOWS:
        popen = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        job = _assign_to_job(popen.pid)
        return SupervisedProcess(popen, _windows_killer(popen, job), lambda: _close_job(job))
    popen = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    return SupervisedProcess(popen, _posix_killer(popen), lambda: None)


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    env: Mapping[str, str],
    stdin: str | None = None,
    timeout_s: float | None = None,
) -> ProcessResult:
    """Spawn `argv` (already resolved, no shell), capture output, enforce `timeout_s`.

    On timeout the whole process tree is killed. `stdin`, when given, is the prompt text piped
    to the child's stdin and then closed; otherwise stdin is closed immediately so the child
    cannot hang waiting for input.
    """
    return _spawn(argv, cwd=cwd, env=env).run(stdin, timeout_s)
