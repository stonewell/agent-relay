"""Process identity: a PID plus a process-start marker (design sections 6 and 15).

A PID alone is ambiguous after reuse: once a process exits, its PID can be recycled by an
unrelated process. Recording a start marker alongside the PID lets a later check distinguish
"the same process is still running" from "the PID now belongs to someone else". The interim
run lock records such an identity so a contender never mistakes a recycled PID for a live
owner, and never takes over while the owner cannot be checked.

The start marker is opaque and host-specific; it only has to be comparable between the moment
it was recorded and the moment it is checked. On Linux it is the ``starttime`` field of
``/proc/<pid>/stat``; on Windows it is the process creation time from ``GetProcessTimes``. On
platforms where no marker can be read (for example macOS, or a PID owned by another user) the
marker is ``None`` and checks report :attr:`IdentityState.UNKNOWN`, so callers stay
conservative and never take over on an unknown owner.

This module imports nothing from the rest of the package, and its platform-specific imports
live inside the ``sys.platform == "win32"`` branch so it imports on every OS.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class IdentityState(Enum):
    """What a recorded identity can tell us about the process it names."""

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, order=True)
class ProcessIdentity:
    """Who owns something: a process id plus a start marker that survives PID reuse."""

    pid: int
    start: int | None  # None when the start marker could not be read


def current_identity() -> ProcessIdentity:
    """Identity of the calling process."""
    pid = os.getpid()
    state, marker = _probe(pid)
    return ProcessIdentity(pid=pid, start=marker if state is IdentityState.ALIVE else None)


def check_identity(identity: ProcessIdentity) -> IdentityState:
    """Whether ``identity`` still names a live process.

    ``DEAD`` means the PID no longer exists, or exists with a different start marker (so it was
    reused by another process). ``UNKNOWN`` means the process exists but its marker could not
    be read; a caller must treat that as un-take-overable. Only ``ALIVE`` confirms the same
    process is still running.
    """
    if identity.pid <= 0:
        return IdentityState.DEAD
    state, marker = _probe(identity.pid)
    if state is IdentityState.DEAD or state is IdentityState.UNKNOWN:
        return state
    if identity.start is None:
        return IdentityState.UNKNOWN  # recorded no marker, so identity cannot be confirmed
    if marker != identity.start:
        return IdentityState.DEAD  # PID was reused by a different process
    return IdentityState.ALIVE


# --- platform probes -------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes as _ct
    from ctypes import wintypes as _w

    _kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _ERROR_INVALID_PARAMETER = 87

    class _FILETIME(_ct.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes protocol
            ("dwLowDateTime", _w.DWORD),
            ("dwHighDateTime", _w.DWORD),
        ]

    _kernel32.OpenProcess.argtypes = [_w.DWORD, _w.BOOL, _w.DWORD]
    _kernel32.OpenProcess.restype = _w.HANDLE
    _kernel32.GetProcessTimes.argtypes = [
        _w.HANDLE,
        _ct.POINTER(_FILETIME),
        _ct.POINTER(_FILETIME),
        _ct.POINTER(_FILETIME),
        _ct.POINTER(_FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = _w.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [_w.HANDLE, _ct.POINTER(_w.DWORD)]
    _kernel32.GetExitCodeProcess.restype = _w.BOOL
    _kernel32.CloseHandle.argtypes = [_w.HANDLE]
    _kernel32.CloseHandle.restype = _w.BOOL

    def _probe(pid: int) -> tuple[IdentityState, int | None]:
        if pid <= 0:
            return IdentityState.DEAD, None
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            code = _ct.get_last_error()
            if code == _ERROR_INVALID_PARAMETER:
                return IdentityState.DEAD, None
            return IdentityState.UNKNOWN, None  # access denied: exists, but marker unreadable
        try:
            exit_code = _w.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, _ct.byref(exit_code)):
                return IdentityState.UNKNOWN, None
            if exit_code.value != 259:  # STILL_ACTIVE
                return IdentityState.DEAD, None
            created = _FILETIME()
            exited = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            if not _kernel32.GetProcessTimes(
                handle,
                _ct.byref(created),
                _ct.byref(exited),
                _ct.byref(kernel),
                _ct.byref(user),
            ):
                return IdentityState.UNKNOWN, None
            marker = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return IdentityState.ALIVE, marker
        finally:
            _kernel32.CloseHandle(handle)

else:

    def _probe(pid: int) -> tuple[IdentityState, int | None]:
        """POSIX probe: existence via ``kill(pid, 0)``; start marker from ``/proc`` on Linux."""
        if pid <= 0:
            return IdentityState.DEAD, None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return IdentityState.DEAD, None
        except (PermissionError, OSError):
            return IdentityState.UNKNOWN, None
        # The start marker is Linux's `/proc/<pid>/stat`; elsewhere this read fails and the
        # identity stays UNKNOWN (conservative: never take over without a marker).
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()
        except (OSError, IndexError):
            return IdentityState.UNKNOWN, None
        if len(fields) < 20:  # need at least up to starttime (field 22 -> index 19)
            return IdentityState.UNKNOWN, None
        if fields[0] == "Z":  # a zombie is a dead owner even though its PID is not reused yet
            return IdentityState.DEAD, None
        try:
            start = int(fields[19])
        except ValueError:
            return IdentityState.UNKNOWN, None
        return IdentityState.ALIVE, start
