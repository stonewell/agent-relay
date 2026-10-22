"""Lifetime ownership locks (design sections 6 and 15).

An exclusive, non-blocking, process-lifetime lock on a file: `flock(LOCK_EX | LOCK_NB)` on
Unix, `LockFileEx` with exclusive and fail-immediately flags on Windows. The operating system
releases it when the holder dies, however it dies, so a held lock proves a live owner and no
heartbeat or PID check is needed.

Rules this module enforces or documents (checked by `tests/test_locks.py`):

* The handle is created non-inheritable and the lock is never passed to workers. Workers must
  be started by exec (`subprocess`), never by a bare `os.fork()`, which would share the open
  file description and keep a Unix lock alive after the owner dies.
* Windows locks a single byte far beyond the end of the file, so the file stays readable, and
  opens without `FILE_SHARE_DELETE`, so a held lock file cannot be deleted or renamed.
* Use only the standard library: no third-party native binding is required.

This module imports nothing from the rest of the package so it can be tested on its own.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

IS_WINDOWS = sys.platform == "win32"

_LOCK_OFFSET = 1 << 40  # Windows: byte range far past EOF, so readers of the file are unaffected


class LockHeld(OSError):
    """Another process (or handle) holds the lock."""


class LifetimeLock:
    """An exclusive lock on `path`, held until `release()` or until the process exits."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: int | None = None  # file descriptor (Unix) or HANDLE (Windows)

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        """Take the lock without waiting; raises `LockHeld` if someone else has it."""
        if self._handle is not None:
            raise RuntimeError(f"{self.path} is already locked by this object")
        self._handle = _acquire(self.path)

    def release(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            _release(handle)

    def __enter__(self) -> LifetimeLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    def __del__(self) -> None:
        with contextlib.suppress(Exception):  # interpreter shutdown: nothing useful left to do
            self.release()


@dataclass(frozen=True)
class LockProbe:
    supported: bool
    reason: str


def probe_lock_support(directory: Path) -> LockProbe:
    """Check that locking behaves on the filesystem holding `directory` (for `orch doctor`).

    Two independent handles in one process must conflict, and the lock must be reacquirable
    after release. Filesystems that silently ignore locks (some network mounts) fail the first.
    """
    probe = directory / f".orch-lock-probe-{os.getpid()}-{secrets.token_hex(3)}"
    first, second = LifetimeLock(probe), LifetimeLock(probe)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        first.acquire()
        try:
            second.acquire()
        except LockHeld:
            pass
        else:
            return LockProbe(False, f"{directory}: a second handle also got the lock")
        first.release()
        second.acquire()
        return LockProbe(True, f"{directory}: exclusive locks behave")
    except OSError as exc:
        return LockProbe(False, f"{directory}: {exc}")
    finally:
        first.release()
        second.release()
        with contextlib.suppress(OSError):
            probe.unlink()


# --- platform implementations -----------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Overlapped(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes protocol
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _GENERIC_READ_WRITE = 0xC0000000
    _SHARE_READ_WRITE = 0x1 | 0x2  # deliberately not FILE_SHARE_DELETE
    _OPEN_ALWAYS = 4
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33
    _ERROR_SHARING_VIOLATION = 32
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def _acquire(path: Path) -> int:
        # A NULL SECURITY_ATTRIBUTES makes the handle non-inheritable.
        handle = _kernel32.CreateFileW(
            str(path), _GENERIC_READ_WRITE, _SHARE_READ_WRITE, None, _OPEN_ALWAYS, 0x80, None
        )
        if handle is None or handle == _INVALID_HANDLE:
            code = ctypes.get_last_error()
            if code == _ERROR_SHARING_VIOLATION:
                raise LockHeld(f"{path} is open exclusively by another process")
            raise ctypes.WinError(code)
        overlapped = _Overlapped(Offset=_LOCK_OFFSET & 0xFFFFFFFF, OffsetHigh=_LOCK_OFFSET >> 32)
        flags = _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY
        if not _kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            code = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            if code == _ERROR_LOCK_VIOLATION:
                raise LockHeld(f"{path} is locked by another process")
            raise ctypes.WinError(code)
        return int(handle)

    def _release(handle: int) -> None:
        _kernel32.CloseHandle(handle)  # closing the handle drops the lock

else:
    import errno
    import fcntl

    def _acquire(path: Path) -> int:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise LockHeld(f"{path} is locked by another process") from exc
            raise
        os.set_inheritable(fd, False)
        return fd

    def _release(handle: int) -> None:
        os.close(handle)  # closing the descriptor drops the lock
