"""Durable sessions and exclusive session ownership (design 5.6, 6, 8.7; task 2.21).

A session survives several approved execution rounds. This module owns its on-disk record
(`.agents/sessions/<session-id>/`), the native lifetime lock that makes one process its owner,
and the idempotent approval-to-run intent that admits at most one run per approval.

Rules enforced here:

* Only the lock holder (:class:`SessionOwner`) writes session state; other processes may read
  durable records through :class:`SessionStore` but cannot approve, answer or dispatch.
* The lock object lives in `.agents/locks/`, outside the purgeable session directory, and is
  never replaced or unlinked, so its existence also reserves the session id for good.
* Approval binds an exact plan revision and digest and carries an idempotency key and the
  expected session revision. The run id is reserved *inside* the intent before the run is
  created, so a crash or retry between the two attaches to the same run instead of allocating
  a second one.
* Where native locking is unsupported, ownership is unavailable (`SessionOwnershipUnavailable`);
  legacy headless run ownership is untouched.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import threading
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .events import EventLog
from .platform.locks import LifetimeLock, LockHeld, probe_lock_support
from .runrecord import SessionLink
from .specs.common import RunId, SpecModel, VersionedSpec
from .store import (
    AGENTS_DIR,
    RUN_JSON,
    RunStore,
    StoreError,
    atomic_write,
    iso,
    locks_dir,
    read_text,
    runs_dir,
    sweep_temp_files,
)

__all__ = [
    "LaunchIntent",
    "SessionBusy",
    "SessionConflict",
    "SessionError",
    "SessionOwner",
    "SessionOwnershipUnavailable",
    "SessionRecord",
    "SessionStore",
    "linked_session",
    "protected_run_ids",
    "session_guard",
]

SESSIONS_DIR = "sessions"
SESSION_JSON = "session.json"
SESSION_EVENTS = "events.jsonl"
MESSAGES = "messages.jsonl"


class SessionError(RuntimeError):
    """Base class for session store and ownership failures."""


class SessionBusy(SessionError):
    """Another process owns the session (`LockHeld`)."""


class SessionOwnershipUnavailable(SessionError):
    """Native session locking does not work here; interactive ownership is disabled."""


class SessionConflict(SessionError):
    """A stale revision, mismatched digest or superseded plan; nothing was changed."""


class LaunchIntent(SpecModel):
    intent_id: str
    plan_revision: int = Field(ge=1)
    approval_digest: str
    approval_id: str
    session_revision: int = Field(ge=1)  # session revision at approval time
    run_id: RunId  # reserved before the run exists
    state: Literal["pending", "linked"] = "pending"


class SessionRecord(VersionedSpec):
    session_id: str
    created_at: str
    repo: str  # canonical repository identity
    revision: int = Field(default=1, ge=1)
    state: Literal["idle", "active", "interrupted"] = "idle"
    admission: Literal["open", "closed"] = "open"
    coordinator: dict[str, Any] = {}
    plan_revision: int = Field(default=0, ge=0)
    linked_runs: list[RunId] = []
    checkpoints: list[str] = []
    delivery_policy: dict[str, Any] | None = None
    intent: LaunchIntent | None = None


# --- paths ------------------------------------------------------------------------------------


def sessions_dir(repo_root: Path) -> Path:
    return repo_root / AGENTS_DIR / SESSIONS_DIR


def session_lock_path(repo_root: Path, session_id: str) -> Path:
    return locks_dir(repo_root) / f"session-{session_id}.lock"


def _valid_id(session_id: str) -> str:
    if not session_id or any(c not in "0123456789abcdefs-" for c in session_id.lower()):
        raise SessionError(f"{session_id!r} is not a session id")
    return session_id


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest_of(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# --- store (read access and creation) ----------------------------------------------------------


class SessionStore:
    """The durable records of one session. Reading needs no ownership; writing does."""

    def __init__(self, repo_root: Path, session_id: str) -> None:
        self.repo_root = repo_root
        self.session_id = _valid_id(session_id)
        self.dir = sessions_dir(repo_root) / session_id

    @classmethod
    def create(
        cls,
        repo_root: Path,
        *,
        coordinator: dict[str, Any] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> SessionStore:
        """Allocate a session id exclusively; ids whose lock object exists are never reused."""
        parent = sessions_dir(repo_root)
        parent.mkdir(parents=True, exist_ok=True)
        locks_dir(repo_root).mkdir(parents=True, exist_ok=True)
        moment = now()
        for attempt in range(40):
            digits = 4 if attempt < 10 else 6
            session_id = f"{moment.astimezone(UTC):%Y-%m-%d}-s{secrets.token_hex(digits // 2)}"
            lock = session_lock_path(repo_root, session_id)
            try:
                # Reserve the id permanently: the lock object is created once, never removed.
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            os.close(fd)
            try:
                (parent / session_id).mkdir()
            except FileExistsError:
                continue
            store = cls(repo_root, session_id)
            store._write(
                SessionRecord(
                    session_id=session_id,
                    created_at=iso(moment),
                    repo=str(repo_root.resolve()),
                    coordinator=coordinator or {},
                )
            )
            return store
        raise SessionError("could not allocate a unique session id")

    @classmethod
    def open(cls, repo_root: Path, session_id: str) -> SessionStore:
        store = cls(repo_root, session_id)
        if not (store.dir / SESSION_JSON).is_file():
            raise SessionError(f"session {session_id} not found under {sessions_dir(repo_root)}")
        sweep_temp_files(store.dir)
        return store

    @staticmethod
    def list_sessions(repo_root: Path) -> list[str]:
        parent = sessions_dir(repo_root)
        if not parent.is_dir():
            return []
        return sorted(p.name for p in parent.iterdir() if (p / SESSION_JSON).is_file())

    # --- records --------------------------------------------------------------------------

    def read(self) -> SessionRecord:
        return SessionRecord.model_validate_json(read_text(self.dir / SESSION_JSON))

    def _write(self, record: SessionRecord) -> None:
        atomic_write(self.dir / SESSION_JSON, record.model_dump_json(indent=2) + "\n")

    @property
    def events_path(self) -> Path:
        return self.dir / SESSION_EVENTS

    def messages(self) -> list[dict[str, Any]]:
        path = self.dir / MESSAGES
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in read_text(path).splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def plan(self, revision: int) -> dict[str, Any]:
        return json.loads(read_text(self.dir / "plans" / f"{revision}.json"))

    def approvals(self) -> list[dict[str, Any]]:
        directory = self.dir / "approvals"
        if not directory.is_dir():
            return []
        return [json.loads(read_text(p)) for p in sorted(directory.glob("*.json"))]

    def acquire_owner(self) -> SessionOwner:
        return SessionOwner.acquire(self.repo_root, self.session_id)


# --- ownership ----------------------------------------------------------------------------------


class SessionOwner:
    """The single writer of a session, valid while its native lifetime lock is held."""

    def __init__(self, store: SessionStore, lock: LifetimeLock) -> None:
        self.store = store
        self._lock = lock
        self._mutex = threading.RLock()
        self._events: EventLog | None = None

    @classmethod
    def acquire(cls, repo_root: Path, session_id: str) -> SessionOwner:
        store = SessionStore.open(repo_root, session_id)
        locks = locks_dir(repo_root)
        probe = probe_lock_support(locks)
        if not probe.supported:
            raise SessionOwnershipUnavailable(probe.reason)
        lock = LifetimeLock(session_lock_path(repo_root, session_id))
        try:
            lock.acquire()
        except LockHeld as exc:
            raise SessionBusy(f"session {session_id} is owned by another process") from exc
        owner = cls(store, lock)
        owner._recover()
        return owner

    # --- lifecycle ------------------------------------------------------------------------

    @property
    def held(self) -> bool:
        return self._lock.held

    def close(self) -> None:
        with self._mutex:
            if self._lock.held and self.store.read().state == "active":
                self._update(lambda r: setattr(r, "state", "idle"))
            if self._events is not None:
                self._events.close()
                self._events = None
            self._lock.release()

    def __enter__(self) -> SessionOwner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _require(self) -> None:
        if not self._lock.held:
            raise SessionError("session ownership was released")

    def event(self, type_: str, data: dict[str, Any] | None = None) -> None:
        """Append one session event (ids, revisions, hashes only: never prompt text)."""
        self._require()
        if self._events is None:
            self._events = EventLog(self.store.events_path)
        self._events.append(type_, data or {})

    @property
    def record(self) -> SessionRecord:
        return self.store.read()

    def _update(self, change: Callable[[SessionRecord], None]) -> SessionRecord:
        self._require()
        record = self.store.read()
        change(record)
        record.revision += 1
        self.store._write(record)  # pyright: ignore[reportPrivateUsage]
        return record

    def _check_revision(self, expected: int | None) -> SessionRecord:
        record = self.store.read()
        if expected is not None and record.revision != expected:
            raise SessionConflict(
                f"stale session revision {expected}; current revision is {record.revision}"
            )
        return record

    # --- messages and plans ---------------------------------------------------------------

    def append_message(self, author: str, content: str, *, provenance: str) -> dict[str, Any]:
        """Append-only message with a stable id and content hash (section 5.6)."""
        with self._mutex:
            self._require()
            existing = self.store.messages()
            message = {
                "message_id": f"m{len(existing) + 1}",
                "author": author,
                "provenance": provenance,
                "content": content,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "ts": iso(datetime.now(UTC)),
            }
            path = self.store.dir / MESSAGES
            with open(path, "ab") as handle:
                handle.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            return message

    def save_plan(
        self,
        graph: dict[str, Any],
        *,
        explanation: str,
        parent: int | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """Persist an immutable plan revision and make it current; returns its number."""
        with self._mutex:
            record = self._check_revision(expected_revision)
            if parent is not None and parent != record.plan_revision:
                raise SessionConflict(f"parent revision {parent} is not the current plan")
            number = record.plan_revision + 1
            body = {
                "revision": number,
                "parent": record.plan_revision or None,
                "explanation": explanation,
                "graph": graph,
                "digest": digest_of(graph),
            }
            directory = self.store.dir / "plans"
            directory.mkdir(exist_ok=True)
            atomic_write(directory / f"{number}.json", json.dumps(body, indent=2) + "\n")
            self._update(lambda r: setattr(r, "plan_revision", number))
            self.event("plan.proposed", {"plan_revision": number, "digest": body["digest"]})
            return number

    # --- approval and launch --------------------------------------------------------------

    def approve(
        self,
        plan_revision: int,
        digest: str,
        *,
        idempotency_key: str,
        expected_session_revision: int,
    ) -> LaunchIntent:
        """Bind an operator approval to one exact revision and digest and reserve its run.

        Idempotent per key: replaying the same request returns the same intent. A different
        request under a used key, a stale session revision, a superseded plan or a digest that
        does not match the stored plan raises :class:`SessionConflict`.
        """
        with self._mutex:
            record = self.store.read()
            for prior in self.store.approvals():
                if prior["idempotency_key"] == idempotency_key:
                    same = (prior["plan_revision"], prior["digest"]) == (plan_revision, digest)
                    if not same or record.intent is None:
                        raise SessionConflict(f"idempotency key {idempotency_key!r} reused")
                    return record.intent
            if record.revision != expected_session_revision:
                raise SessionConflict(
                    f"stale session revision {expected_session_revision}; "
                    f"current revision is {record.revision}"
                )
            if plan_revision != record.plan_revision:
                raise SessionConflict(f"plan revision {plan_revision} is superseded")
            if record.admission != "open":
                raise SessionConflict("admission is closed")
            if record.intent is not None and record.intent.state == "pending":
                raise SessionConflict("another approved launch is still pending")
            if self.store.plan(plan_revision)["digest"] != digest:
                raise SessionConflict("approval digest does not match the stored plan")
            approval_id = f"a{len(self.store.approvals()) + 1}"
            directory = self.store.dir / "approvals"
            directory.mkdir(exist_ok=True)
            atomic_write(
                directory / f"{approval_id}.json",
                json.dumps(
                    {
                        "approval_id": approval_id,
                        "plan_revision": plan_revision,
                        "digest": digest,
                        "idempotency_key": idempotency_key,
                        "expected_session_revision": expected_session_revision,
                        "approved_at": iso(datetime.now(UTC)),
                    },
                    indent=2,
                )
                + "\n",
            )
            intent = LaunchIntent(
                intent_id=f"i{approval_id}",
                plan_revision=plan_revision,
                approval_digest=digest,
                approval_id=approval_id,
                session_revision=record.revision,
                run_id=self._reserve_run_id(),
            )
            self._update(lambda r: setattr(r, "intent", intent))
            self.event(
                "plan.approved", {"plan_revision": plan_revision, "approval_id": approval_id}
            )
            self.event("launch.prepared", {"intent_id": intent.intent_id, "run_id": intent.run_id})
            return intent

    def _reserve_run_id(self) -> RunId:
        runs = runs_dir(self.store.repo_root)
        locks = locks_dir(self.store.repo_root)
        moment = datetime.now(UTC)
        for attempt in range(40):
            digits = 4 if attempt < 10 else 6
            candidate = f"{moment:%Y-%m-%d}-{secrets.token_hex(digits // 2)}"
            if (runs / candidate).exists() or (
                locks.is_dir() and any(locks.glob(f"*-{candidate}.lock"))
            ):
                continue
            return candidate
        raise SessionError("could not reserve a unique run id")

    def start_run(self, *, config_hash: str | None = None) -> RunStore:
        """Create (or attach to) the one run of the pending intent; idempotent.

        Verifies the durable gate, the plan revision and the intent before touching the run
        store. A retry or crash after run creation attaches to the recorded run.
        """
        with self._mutex:
            record = self.store.read()
            intent = record.intent
            if intent is None:
                raise SessionConflict("no approved launch intent")
            if record.admission != "open":
                raise SessionConflict("admission is closed")
            if intent.plan_revision != record.plan_revision:
                raise SessionConflict("the approved plan revision was superseded")
            store = self._attach_or_create(intent, config_hash)
            if intent.state != "linked":

                def link(r: SessionRecord) -> None:
                    assert r.intent is not None
                    r.intent.state = "linked"
                    if intent.run_id not in r.linked_runs:
                        r.linked_runs.append(intent.run_id)

                self._update(link)
                self.event(
                    "launch.linked", {"intent_id": intent.intent_id, "run_id": intent.run_id}
                )
            return store

    def _attach_or_create(self, intent: LaunchIntent, config_hash: str | None) -> RunStore:
        link = SessionLink(
            session_id=self.store.session_id,
            plan_revision=intent.plan_revision,
            intent_id=intent.intent_id,
        )
        run_dir = runs_dir(self.store.repo_root) / intent.run_id
        if run_dir.exists():
            store = RunStore.open(self.store.repo_root, intent.run_id)
            try:
                existing = store.read_run().session
            except (OSError, ValueError) as exc:
                raise StoreError(f"run {intent.run_id} is unreadable: {exc}") from exc
            if existing != link:
                raise SessionConflict(f"run {intent.run_id} exists but is not this launch")
            return store
        store = RunStore.create(self.store.repo_root, config_hash=config_hash, run_id=intent.run_id)
        store.update_run(lambda r: setattr(r, "session", link))
        return store

    # --- owner commands -------------------------------------------------------------------

    def record_command(self, command_id: str, content: dict[str, Any]) -> bool:
        """Durably record an owner command once; True if new, False for an identical repeat.

        A repeated id with different content raises :class:`SessionConflict` (section 8.7).
        """
        if not command_id or any(c in command_id for c in "/\\:") or ".." in command_id:
            raise SessionConflict(f"{command_id!r} is not a valid command id")
        with self._mutex:
            self._require()
            directory = self.store.dir / "commands"
            directory.mkdir(exist_ok=True)
            path = directory / f"{command_id}.json"
            if path.is_file():
                if json.loads(read_text(path))["content"] != content:
                    raise SessionConflict(f"command id {command_id!r} reused with other content")
                return False
            record = {"command_id": command_id, "content": content}
            atomic_write(path, json.dumps(record) + "\n")
            return True

    # --- gate and recovery ----------------------------------------------------------------

    def close_admission(self) -> None:
        with self._mutex:
            self._update(lambda r: setattr(r, "admission", "closed"))
            self.event("admission.paused", {})

    def open_admission(self) -> None:
        with self._mutex:
            self._update(lambda r: setattr(r, "admission", "open"))
            self.event("admission.resumed", {})

    def _recover(self) -> None:
        """After owner death: link a created run to its intent; mark the session interrupted.

        Reopening never dispatches or repeats a coordinator call. Proof that old managed
        execution terminated stays with run recovery (section 6) before any admission.
        """
        record = self.store.read()
        intent = record.intent
        if intent is not None and intent.state == "pending":
            run_json = runs_dir(self.store.repo_root) / intent.run_id / RUN_JSON
            if run_json.is_file():
                with contextlib.suppress(SessionConflict, StoreError, OSError, ValueError):
                    self.start_run()
        state = self.store.read().state
        if state == "active":  # the previous owner never closed cleanly
            self._update(lambda r: setattr(r, "state", "interrupted"))
            self.event("session.interrupted", {})
        elif state == "idle":
            self._update(lambda r: setattr(r, "state", "active"))

    def mark_interrupted(self) -> None:
        """Record that a round was stopped before completion (section 8.7)."""
        with self._mutex:
            self._update(lambda r: setattr(r, "state", "interrupted"))
            self.event("session.interrupted", {})

    def mark_active(self) -> None:
        """The operator explicitly continues a recovered session."""
        with self._mutex:
            self._update(lambda r: setattr(r, "state", "active"))


# --- linked-run checks and retention -------------------------------------------------------------


def linked_session(repo_root: Path, run_id: str) -> str | None:
    run_json = runs_dir(repo_root) / run_id / RUN_JSON
    if not run_json.is_file():
        return None
    try:
        link = RunStore.open(repo_root, run_id).read_run().session
    except (StoreError, OSError, ValueError):
        return None
    return link.session_id if link else None


@contextlib.contextmanager
def session_guard(
    repo_root: Path, run_id: str, *, held: SessionOwner | None = None
) -> Generator[None]:
    """Honour session ownership around a mutating command on `run_id` (section 6).

    A run with no session link is unaffected (legacy headless behaviour). For a linked run the
    caller must be the session's owner (`held`) or be able to take the session lock for the
    duration of the command; otherwise :class:`SessionBusy` is raised and nothing changes.
    Session ownership is acquired before run ownership, so use this before `RunLock.acquire`.
    """
    session_id = linked_session(repo_root, run_id)
    if session_id is None or (held is not None and held.store.session_id == session_id):
        yield
        return
    with SessionOwner.acquire(repo_root, session_id):
        yield


def protected_run_ids(repo_root: Path) -> set[str]:
    """Runs referenced by any session; `clean` must not purge them (session retention)."""
    protected: set[str] = set()
    for session_id in SessionStore.list_sessions(repo_root):
        with contextlib.suppress(SessionError, OSError, ValueError):
            record = SessionStore.open(repo_root, session_id).read()
            protected.update(record.linked_runs)
            if record.intent is not None:
                protected.add(record.intent.run_id)
    return protected
