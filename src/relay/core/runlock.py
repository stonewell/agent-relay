"""Interim run ownership lock (design section 6, phase 1).

Phase 1 has no supervisor: the foreground ``relay run`` process is the run's single writer. It
takes ownership by creating ``.agents/locks/<repo-key>-<run-id>.lock`` with exclusive-create
semantics (``open(..., "x")``, i.e. ``O_CREAT | O_EXCL``) and writes its PID plus process-start
identity into that file. On normal exit it removes the file; a crash leaves the file behind,
which is the stale-lock case.

The lock *is* the file: there is no separate ``run.lock`` identity record and no native
``LifetimeLock`` in this phase (the native lifetime lock is the phase-2 replacement, and
``run.lock`` is phase-2 supervisor diagnostic metadata). A contender that finds the file reads
the recorded PID and process-start identity:

* A live owner means the run is in progress; the contender is refused.
* A dead owner (gone, or PID reused by a different process) is *stale*: the run is marked
  ``interrupted`` and the lock is taken over only when the caller explicitly passes a
  retry/resume intent. Without that intent the stale lock is reported, never silently replaced.
* An owner whose identity cannot be read -- an empty or partially written file (the
  create-then-write window), or an unreadable record -- is never taken over.

Messages never mention absolute paths, host names or user names.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

from .platform.procid import IdentityState, ProcessIdentity, check_identity, current_identity
from .sessions import SessionBusy, SessionError, SessionOwner, linked_session
from .store import RunStore, StoreError, locks_dir, read_text


class OwnerState(Enum):
    """What the recorded owner of a run lock tells us."""

    ABSENT = "absent"  # no lock file: the run is not locked
    ALIVE = "alive"  # the recorded owner is still running
    STALE = "stale"  # the recorded owner is gone (dead, or PID reused)
    UNKNOWN = "unknown"  # an owner is recorded but cannot be checked


@dataclass(frozen=True)
class Owner:
    """The recorded owner of a run lock and what it tells us."""

    identity: ProcessIdentity | None
    state: OwnerState


class RunLockError(RuntimeError):
    """Base for run-lock contention errors."""


class RunLockSessionBusy(RunLockError):
    """The run belongs to a session whose owner is another process (design section 6)."""

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.identity = None
        super().__init__(f"run {run_id} is linked to a session: {reason}")


class RunLockHeld(RunLockError):
    """The run is in progress: a live owner holds the lock."""

    def __init__(self, run_id: str, identity: ProcessIdentity | None = None) -> None:
        self.run_id = run_id
        self.identity = identity
        owner = f" (owner pid {identity.pid})" if identity is not None else ""
        super().__init__(f"run {run_id} is in progress{owner}; it cannot be taken over")


class RunLockStale(RunLockError):
    """The recorded owner is gone; taking over requires explicit retry/resume intent."""

    def __init__(self, run_id: str, identity: ProcessIdentity | None = None) -> None:
        self.run_id = run_id
        self.identity = identity
        owner = f" (owner pid {identity.pid})" if identity is not None else ""
        super().__init__(
            f"run {run_id} has a stale lock{owner}; the previous owner is no longer running"
        )


class RunLockUnknown(RunLockError):
    """The recorded owner cannot be checked, so the lock is never taken over."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.identity = None
        super().__init__(
            f"cannot determine whether run {run_id} is still active; refusing to take it over"
        )


# --- paths ------------------------------------------------------------------------------------


def repo_key(repo_root: Path) -> str:
    """A stable, path-safe identity of the repository (design section 6 ``<repo-key>``).

    The lock directory already lives inside the repository, so the key only namespaces the
    lock file; it is a short hash of the canonical root, never the path itself.
    """
    digest = hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()
    return digest[:12]


def lock_path(repo_root: Path, run_id: str) -> Path:
    return locks_dir(repo_root) / f"{repo_key(repo_root)}-{run_id}.lock"


# --- owner inspection -------------------------------------------------------------------------


def _identity_payload(identity: ProcessIdentity) -> str:
    return json.dumps({"pid": identity.pid, "start": identity.start}) + "\n"


def _parse_identity(text: str) -> ProcessIdentity | None:
    """Parse a lock record; ``None`` when it is empty, partial or otherwise unreadable."""
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, Any], raw)
    pid = data.get("pid")
    start = data.get("start")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if start is not None and not isinstance(start, int):
        return None
    return ProcessIdentity(pid=pid, start=start)


def inspect_owner(repo_root: Path, run_id: str) -> Owner:
    """Inspect the recorded owner of a run lock without taking it."""
    try:
        text = read_text(lock_path(repo_root, run_id))
    except FileNotFoundError:
        return Owner(None, OwnerState.ABSENT)
    identity = _parse_identity(text)
    if identity is None:
        return Owner(None, OwnerState.UNKNOWN)  # empty, partial or unreadable record
    state = check_identity(identity)
    if state is IdentityState.ALIVE:
        return Owner(identity, OwnerState.ALIVE)
    if state is IdentityState.DEAD:
        return Owner(identity, OwnerState.STALE)
    return Owner(identity, OwnerState.UNKNOWN)


# --- interrupted marking ----------------------------------------------------------------------


def mark_interrupted(repo_root: Path, run_id: str) -> bool:
    """Mark a ``running`` run ``interrupted``, crash-safely. Returns whether state changed.

    Terminal states (``done``, ``failed``, ``blocked``, ``cancelled``) are left alone, so a
    completed run is never rewritten as interrupted, and a run already ``interrupted`` is left
    alone (idempotent). If the run record does not exist there is nothing to mark.
    """
    try:
        store = RunStore(repo_root, run_id)
        if not store.run_dir.is_dir():
            return False
    except StoreError:
        return False
    record = store.read_run()
    if record.state != "running":
        return False
    record.state = "interrupted"
    store.write_run(record)
    return True


# --- the lock ---------------------------------------------------------------------------------


def _create_lock(path: Path, identity: ProcessIdentity) -> None:
    """Create the lock file exclusively and write the owner identity into it.

    The file appears (empty) the instant it is created; a contender that reads it in the
    window before this write completes sees an empty file and treats it as ``UNKNOWN``, never
    as a dead owner. If the write fails the empty file is removed so it cannot block takeover.
    """
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_identity_payload(identity))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def _overwrite_lock(path: Path, identity: ProcessIdentity) -> None:
    """Replace an existing (stale) lock file with the new owner identity, atomically.

    The new identity is written to a temp file in the same directory (same filesystem),
    flushed and fsynced, then ``os.replace``d onto the lock path. A reader therefore sees
    either the old owner or the new one, never a torn record. If the write or the replace
    fails the temp file is removed and the original lock is left untouched, so it stays
    readable and the error propagates.
    """
    payload = _identity_payload(identity)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class RunLock:
    """A held run lock: the lock file that carries the owner identity."""

    def __init__(
        self, path: Path, identity: ProcessIdentity, session: SessionOwner | None = None
    ) -> None:
        self._path = path
        self.identity = identity
        self._session = session  # a session lock this command took; released after the run's

    @property
    def held(self) -> bool:
        return self._path.exists()

    @classmethod
    def acquire(
        cls,
        repo_root: Path,
        run_id: str,
        *,
        takeover: bool = False,
        session_owner: SessionOwner | None = None,
    ) -> RunLock:
        """Acquire the run lock, honouring session ownership first (design section 6).

        A run linked to a session needs that session's lock: the caller's own `session_owner`,
        or one taken here for the command and released with the run lock. Session ownership is
        always acquired before run ownership. Unlinked runs behave exactly as before.
        """
        session: SessionOwner | None = None
        try:
            linked = linked_session(repo_root, run_id)
            if linked is not None and (
                session_owner is None or session_owner.store.session_id != linked
            ):
                session = SessionOwner.acquire(repo_root, linked)
        except SessionBusy as exc:
            raise RunLockSessionBusy(run_id, str(exc)) from exc
        except SessionError as exc:
            raise RunLockSessionBusy(run_id, str(exc)) from exc
        try:
            lock = cls._acquire_file(repo_root, run_id, takeover=takeover)
        except BaseException:
            if session is not None:
                session.close()
            raise
        lock._session = session  # pyright: ignore[reportPrivateUsage]
        return lock

    @classmethod
    def _acquire_file(cls, repo_root: Path, run_id: str, *, takeover: bool) -> RunLock:
        """Acquire the run lock file, refusing or taking over per the contention rules above.

        ``takeover`` must be ``True`` (explicit retry/resume intent) before a stale lock is
        taken over; without it a stale lock raises :class:`RunLockStale`. A live owner is
        always refused, and an owner whose identity cannot be checked is never taken over.
        """
        path = lock_path(repo_root, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        identity = current_identity()
        try:
            _create_lock(path, identity)
        except FileExistsError:
            pass  # someone else created it: contend below
        else:
            return cls(path, identity)

        owner = inspect_owner(repo_root, run_id)
        if owner.state is OwnerState.ALIVE:
            raise RunLockHeld(run_id, owner.identity)
        if owner.state is OwnerState.UNKNOWN:
            raise RunLockUnknown(run_id)
        # STALE: the recorded owner is gone; take over only with explicit intent.
        if not takeover:
            raise RunLockStale(run_id, owner.identity)
        mark_interrupted(repo_root, run_id)  # crash-safe; only flips `running`
        _overwrite_lock(path, identity)
        return cls(path, identity)

    def release(self) -> None:
        """Remove the lock file. Idempotent; a crash leaves it for the next contender."""
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        if self._session is not None:
            session, self._session = self._session, None
            session.close()

    def __enter__(self) -> RunLock:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
