"""Durable sessions and exclusive session ownership (design 5.6, 6, 8.7; task 2.21).

A session survives several approved execution rounds. This module owns its on-disk record
(`.agents/sessions/<session-id>/`), the native lifetime lock that makes one process its owner,
and the idempotent approval-to-run intent that admits at most one run per approval.

Rules enforced here:

* Only the lock holder (:class:`SessionOwner`) writes session state, and only it sweeps
  leftover temp files (right after acquiring the lock); other processes may read durable
  records through :class:`SessionStore` but cannot approve, answer or dispatch.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .events import EventLog
from .gitexclude import ExcludeError, ensure_exclude_block
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
    "RoundRecord",
    "SessionBusy",
    "SessionConflict",
    "SessionError",
    "SessionOwner",
    "SessionOwnershipUnavailable",
    "SessionRecord",
    "SessionStore",
    "RunDeletion",
    "SessionDeletion",
    "SessionDeletionPlan",
    "delete_session",
    "linked_session",
    "plan_session_deletion",
    "protected_run_ids",
    "round_state",
    "session_guard",
]

SESSIONS_DIR = "sessions"
SESSION_JSON = "session.json"
SESSION_EVENTS = "events.jsonl"
MESSAGES = "messages.jsonl"
DELETING = "deleting"
SESSION_REFS = "refs/relay-sessions"


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


class RoundRecord(SpecModel):
    """One approved execution round: a linked run, its plan revision and its pinned base."""

    run_id: RunId
    plan_revision: int = Field(ge=1)
    state: Literal["active", "superseded"] = "active"
    base: dict[str, Any] | None = None
    checkpoint_id: str | None = None
    """The checkpoint that superseded this round."""


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
    rounds: list[RoundRecord] = []
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


_RECORD_DIRS = frozenset({"turns", "calls", "questions", "context"})


def _record_dir(subdir: str) -> str:
    if subdir not in _RECORD_DIRS:
        raise SessionError(f"{subdir!r} is not a session record directory")
    return subdir


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
        with contextlib.suppress(ExcludeError):  # no repository means nothing to exclude
            ensure_exclude_block(repo_root)  # transcripts must never reach `git add -A`
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
        """A read handle on an existing session. Opening never changes the session directory.

        Leftover temp files are swept only by :meth:`SessionOwner.acquire` once the session
        lock is held; an observer that swept would race the live owner's atomic writes.
        """
        store = cls(repo_root, session_id)
        if not (store.dir / SESSION_JSON).is_file():
            raise SessionError(f"session {session_id} not found under {sessions_dir(repo_root)}")
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

    def read_record(self, subdir: str, name: str) -> dict[str, Any] | None:
        """A durable JSON record under `<session>/<subdir>/<name>.json`, or None."""
        path = self._record_path(subdir, name)
        if not path.is_file():
            return None
        data: dict[str, Any] = json.loads(read_text(path))
        return data

    def list_records(self, subdir: str) -> list[dict[str, Any]]:
        directory = self.dir / _record_dir(subdir)
        if not directory.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            found.append(json.loads(read_text(path)))
        return found

    def _record_path(self, subdir: str, name: str) -> Path:
        if not name or any(c in name for c in "/\\:") or ".." in name:
            raise SessionError(f"{name!r} is not a record name")
        return self.dir / _record_dir(subdir) / f"{name}.json"

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
    def acquire(
        cls,
        repo_root: Path,
        session_id: str,
        *,
        finishing_deletion: bool = False,
        recover: bool = True,
    ) -> SessionOwner:
        """Take the session lock. `recover=False` inspects without changing any record."""
        # Sweeping temp files before holding the lock would race a live owner's atomic writes,
        # so leftovers are swept only once ownership is held (below).
        store = SessionStore(repo_root, session_id)
        if not (store.dir / SESSION_JSON).is_file():
            raise SessionError(f"session {session_id} not found under {sessions_dir(repo_root)}")
        if (store.dir / DELETING).exists() and not finishing_deletion:
            # Recovery reconciles an interrupted deletion before admitting new work (section 6).
            raise SessionError(
                f"deletion of session {session_id} was interrupted; confirm deletion again to "
                "finish it"
            )
        locks = locks_dir(repo_root)
        probe = probe_lock_support(locks)
        if not probe.supported:
            raise SessionOwnershipUnavailable(probe.reason)
        lock = LifetimeLock(session_lock_path(repo_root, session_id))
        try:
            lock.acquire()
        except LockHeld as exc:
            raise SessionBusy(f"session {session_id} is owned by another process") from exc
        sweep_temp_files(store.dir)
        owner = cls(store, lock)
        if recover and not finishing_deletion:
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

    def _close_events(self) -> None:
        if self._events is not None:
            self._events.close()
            self._events = None

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
        base: dict[str, Any] | None = None,
        proposer: dict[str, Any] | None = None,
    ) -> int:
        """Persist an immutable plan revision and make it current; returns its number.

        `base` names the explicit retained base (checkpoint or integration) the round starts
        from; it is part of the digest the operator approves. None means the repository HEAD.
        `proposer` is the exact coordinator profile that proposed the plan; when given it is
        part of the digest too, so a changed coordinator cannot inherit the approval.
        """
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
                "base": base,
                "proposer": proposer,
                "digest": digest_of(
                    {"graph": graph, "base": base}
                    if proposer is None
                    else {"graph": graph, "base": base, "proposer": proposer}
                ),
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
                plan = self.store.plan(intent.plan_revision)

                def link(r: SessionRecord) -> None:
                    assert r.intent is not None
                    r.intent.state = "linked"
                    if intent.run_id not in r.linked_runs:
                        r.linked_runs.append(intent.run_id)
                        r.rounds.append(
                            RoundRecord(
                                run_id=intent.run_id,
                                plan_revision=intent.plan_revision,
                                base=plan.get("base"),
                            )
                        )

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

    def write_record(self, subdir: str, name: str, data: dict[str, Any]) -> None:
        """Atomically (over)write one of this owner's mutable records (turns, calls, questions)."""
        with self._mutex:
            self._require()
            path = self.store._record_path(subdir, name)  # pyright: ignore[reportPrivateUsage]
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps(data, indent=2) + "\n")

    # --- checkpoint and revise ------------------------------------------------------------

    def supersede_round(self, run_id: str, checkpoint_id: str) -> None:
        """Mark a round superseded by a sealed checkpoint and reopen the gate (section 8.7).

        The superseded run cannot resume through either front-end until it is explicitly
        restored. Admission reopens here, in the same durable update, so a revised plan can be
        approved; nothing dispatches until that approval.
        """
        with self._mutex:

            def change(r: SessionRecord) -> None:
                matches = [x for x in r.rounds if x.run_id == run_id]
                if not matches:
                    raise SessionConflict(f"run {run_id} is not a round of this session")
                matches[0].state = "superseded"
                matches[0].checkpoint_id = checkpoint_id
                if checkpoint_id not in r.checkpoints:
                    r.checkpoints.append(checkpoint_id)
                r.admission = "open"

            self._update(change)
            self.event("plan.superseded", {"run_id": run_id, "checkpoint_id": checkpoint_id})
            self.event("admission.resumed", {})

    def restore_round(self, run_id: str) -> None:
        """Explicitly restore a superseded round after revalidation (never implicit on reopen)."""
        with self._mutex:
            record = self.store.read()
            matches = [x for x in record.rounds if x.run_id == run_id]
            if not matches or matches[0].state != "superseded":
                raise SessionConflict(f"run {run_id} is not a superseded round")
            if any(x.state == "active" and x.run_id != run_id for x in record.rounds):
                raise SessionConflict("another round is active; at most one graph runs per session")
            self._update(lambda r: self._reactivate(r, run_id))
            self.event("admission.paused", {"reason": "restore"})
            self.event("admission.resumed", {"reason": "restore", "run_id": run_id})

    @staticmethod
    def _reactivate(record: SessionRecord, run_id: str) -> None:
        for item in record.rounds:
            if item.run_id == run_id:
                item.state = "active"
                item.checkpoint_id = None
        record.admission = "open"

    def register_checkpoint(self, checkpoint_id: str) -> None:
        with self._mutex:
            if checkpoint_id not in self.store.read().checkpoints:
                self._update(lambda r: r.checkpoints.append(checkpoint_id))

    def set_delivery_policy(self, policy: dict[str, Any]) -> None:
        """Record the cumulative delivery policy (section 10); identical values are a no-op.

        The caller derives it from the approved rounds (`relay.core.delivery`); delivery
        re-derives it and refuses a record that disagrees, so this is evidence, not authority.
        """
        with self._mutex:
            if self.store.read().delivery_policy != policy:
                self._update(lambda r: setattr(r, "delivery_policy", policy))
                self.event("delivery.policy", {"rounds": list(policy.get("rounds", []))})

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
    """The session a run is linked to, or None. Read-only: never sweeps or writes."""
    run_json = runs_dir(repo_root) / run_id / RUN_JSON
    if not run_json.is_file():
        return None
    try:
        link = RunStore(repo_root, run_id).read_run().session
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


def round_state(repo_root: Path, run_id: str) -> str | None:
    """`active`, `superseded` or None for a run that is not a round of any session (read-only)."""
    session_id = linked_session(repo_root, run_id)
    if session_id is None:
        return None
    with contextlib.suppress(SessionError, OSError, ValueError):
        for item in SessionStore(repo_root, session_id).read().rounds:
            if item.run_id == run_id:
                return item.state
    return None


def protected_run_ids(repo_root: Path) -> set[str]:
    """Runs referenced by any session; `clean` must not purge them (session retention).

    Read-only: live owners' sessions are read without sweeping their temp files.
    """
    protected: set[str] = set()
    for session_id in SessionStore.list_sessions(repo_root):
        with contextlib.suppress(SessionError, OSError, ValueError):
            record = SessionStore(repo_root, session_id).read()
            protected.update(record.linked_runs)
            if record.intent is not None:
                protected.add(record.intent.run_id)
    return protected


# --- confirmed deletion ---------------------------------------------------------------------------


def _unconfirmed_execution(repo_root: Path, store: SessionStore) -> list[str]:
    """Why deleting now could lose track of execution (empty when it is safe)."""
    record = store.read()
    problems: list[str] = []
    if record.state == "active":
        problems.append("the session was not closed cleanly; open it once so it is recovered")
    if record.intent is not None and record.intent.state == "pending":
        problems.append(f"approved launch {record.intent.intent_id} has not been reconciled")
    for turn in store.list_records("turns"):
        if turn.get("status") in ("active", "blocked"):
            problems.append(
                f"coordinator turn {turn.get('turn_id')} is {turn.get('status')}: its coordinator "
                "process is not confirmed stopped"
            )
    for call in store.list_records("calls"):
        if call.get("status") == "started":
            problems.append(f"coordinator call {call.get('call_id')} was never confirmed finished")
    for run_id in record.linked_runs:
        run_store = RunStore(repo_root, run_id)  # read-only: no lock, no sweeping
        if not run_store.run_json.is_file():
            continue
        try:
            run = run_store.read_run()
        except (StoreError, OSError, ValueError) as exc:
            problems.append(f"run {run_id} is unreadable: {exc}")
            continue
        if run.state == "running":
            problems.append(f"run {run_id} is still running or was never recovered")
        for node_id, node in run.nodes.items():
            if node.worker is not None or node.worker_pending or node.recovery_pending:
                problems.append(f"run {run_id} node {node_id} has an unconfirmed worker")
    return problems


RoundDelivery = Literal["delivered", "undelivered", "unknown"]


@dataclass(frozen=True)
class RunDeletion:
    """One run that deleting the session removes, and whether its work was delivered."""

    run_id: str
    state: str
    delivery: RoundDelivery
    detail: str
    refs: tuple[str, ...]
    worktrees: int
    scratch: bool
    blocker: str | None
    blocked_refs: tuple[str, ...] = ()
    """Run branches a worktree outside the deletion has checked out (deletion is refused)."""


@dataclass(frozen=True)
class SessionDeletionPlan:
    session_id: str
    runs: tuple[RunDeletion, ...]
    session_refs: tuple[str, ...]
    problems: tuple[str, ...]
    """Reasons deletion is refused now (live or unverifiable execution, shared runs)."""

    @property
    def undelivered(self) -> tuple[RunDeletion, ...]:
        return tuple(r for r in self.runs if r.delivery != "delivered")


@dataclass(frozen=True)
class SessionDeletion:
    session_id: str
    runs: tuple[str, ...]
    refs: tuple[str, ...]


def _git_lines(repo_root: Path, *args: str) -> list[str] | None:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.stdout.splitlines() if proc.returncode == 0 else None


def _round_delivery(
    repo_root: Path, store: SessionStore, item: RoundRecord
) -> tuple[str, RoundDelivery, str]:
    """(run state, delivery, detail): delivered means a user branch already contains it."""
    run_store = RunStore(repo_root, item.run_id)  # no sweep
    if not run_store.run_json.is_file():
        return "missing", "undelivered", "the run record is missing"
    try:
        run = run_store.read_run()
    except (StoreError, OSError, ValueError) as exc:
        return "unreadable", "unknown", f"the run record is unreadable: {exc}"
    commit: str | None = None
    if run.state == "done" and run.final_commit:
        commit = run.final_commit
    elif item.checkpoint_id:
        path = store.dir / "checkpoints" / f"{item.checkpoint_id}.json"
        with contextlib.suppress(OSError, ValueError):
            commit = json.loads(read_text(path)).get("combined_commit")
    if not commit:
        return run.state, "undelivered", f"{run.state}, no delivered result"
    branches = _git_lines(
        repo_root, "for-each-ref", "--contains", commit, "--format=%(refname)", "refs/heads/"
    )
    if branches is None:
        return run.state, "unknown", "whether it was applied cannot be checked"
    users = [b for b in branches if not b.startswith("refs/heads/relay/")]
    if users:
        names = ", ".join(b.removeprefix("refs/heads/") for b in users[:3])
        return run.state, "delivered", f"contained in {names}"
    return run.state, "undelivered", f"{run.state}, not applied to any branch"


def _session_runs(repo_root: Path, store: SessionStore) -> list[str]:
    """The runs linked to this session (rounds, a reserved launch, and a deletion's list)."""
    record = store.read()
    ids = list(record.linked_runs)
    if record.intent is not None and record.intent.run_id not in ids:
        ids.append(record.intent.run_id)
    marker = store.dir / DELETING
    if marker.is_file():
        with contextlib.suppress(OSError, ValueError, AttributeError):
            for run_id in json.loads(read_text(marker)).get("runs", []):
                if str(run_id) not in ids:
                    ids.append(str(run_id))
    return [run_id for run_id in ids if (runs_dir(repo_root) / run_id).exists()]


def _shared_with_others(repo_root: Path, session_id: str, run_ids: list[str]) -> list[str]:
    problems: list[str] = []
    for other in SessionStore.list_sessions(repo_root):
        if other == session_id:
            continue
        with contextlib.suppress(SessionError, OSError, ValueError):
            record = SessionStore(repo_root, other).read()
            referenced = set(record.linked_runs)
            if record.intent is not None:
                referenced.add(record.intent.run_id)
            for run_id in run_ids:
                if run_id in referenced:
                    problems.append(f"run {run_id} is also referenced by session {other}")
    return problems


def plan_session_deletion(
    repo_root: Path, session_id: str, *, worktree_root: str | None = None
) -> SessionDeletionPlan:
    """Exactly what deleting the session removes; read-only (no lock, no sweeping)."""
    from .run_purge import blocked_message, owner_blocker, plan_run_purge

    store = SessionStore(repo_root, session_id)
    if not (store.dir / SESSION_JSON).is_file():
        raise SessionError(f"session {session_id} not found under {sessions_dir(repo_root)}")
    root = worktree_root or _worktree_root(repo_root)
    record = store.read()
    rounds = {item.run_id: item for item in record.rounds}
    run_ids = _session_runs(repo_root, store)
    problems: list[str] = []
    if not (store.dir / DELETING).exists():
        problems += _unconfirmed_execution(repo_root, store)
    problems += _shared_with_others(repo_root, session_id, run_ids)
    runs: list[RunDeletion] = []
    for run_id in run_ids:
        item = rounds.get(run_id) or RoundRecord(run_id=run_id, plan_revision=1)
        state, delivery, detail = _round_delivery(repo_root, store, item)
        blocker = owner_blocker(repo_root, run_id)
        if blocker is not None:
            problems.append(blocker)
        purge = plan_run_purge(repo_root, run_id, root)
        problems += [blocked_message(ref, path) for ref, path in purge.blocked_refs.items()]
        runs.append(
            RunDeletion(
                run_id, state, delivery, detail, tuple(purge.refs), len(purge.worktrees),
                purge.scratch is not None, blocker, tuple(purge.blocked_refs),
            )
        )  # fmt: skip
    refs = _git_lines(
        repo_root, "for-each-ref", "--format=%(refname)", f"{SESSION_REFS}/{session_id}/"
    )
    return SessionDeletionPlan(session_id, tuple(runs), tuple(refs or ()), tuple(problems))


def _worktree_root(repo_root: Path) -> str:
    from . import registry

    return registry.load_config(
        project_root=repo_root, user_config=registry.USER_CONFIG
    ).config.paths.worktree_root


def delete_session(
    repo_root: Path,
    session_id: str,
    *,
    include_undelivered: bool = False,
    worktree_root: str | None = None,
) -> SessionDeletion:
    """Delete a session and its runs after operator confirmation (section 6).

    Ownership is acquired first (:class:`SessionBusy` if another process owns it), without
    recovery, so a refused deletion changes no record. Refused with :class:`SessionConflict`:
    active or unconfirmed execution (including coordinator turns and calls not proven
    finished), a run whose owner is alive or cannot be checked, a run another session also
    references, and -- unless `include_undelivered` records the operator's explicit
    confirmation -- any round whose work is not contained in a user branch, or whose applied
    state cannot be checked.

    A durable marker listing the runs is written before anything is removed. Each run is purged
    like `relay clean --purge-runs` (worktrees, scratch, then `refs/relay/<run>/...` and
    `refs/heads/relay/<run>/...`, then its record); then the session's checkpoint refs and
    records go, `session.json` last. Deleting again after a crash finishes the job; ownership
    is refused until then. No other ref is touched, so the user's branches never change, and a
    run branch checked out in a worktree the purge keeps (the main one included) refuses the
    deletion with :class:`SessionConflict`. Lock objects are never created, replaced or
    unlinked; the session lock keeps its id reserved.
    """
    import shutil

    from .run_purge import blocked_message, execute_run_purge, owner_blocker, plan_run_purge

    # Nothing is swept or written before the lock is held: that would race a live owner.
    store = SessionStore(repo_root, session_id)
    if not (store.dir / SESSION_JSON).is_file():
        raise SessionError(f"session {session_id} not found under {sessions_dir(repo_root)}")
    finishing = (store.dir / DELETING).exists()
    owner = SessionOwner.acquire(repo_root, session_id, finishing_deletion=finishing, recover=False)
    try:
        root = worktree_root or _worktree_root(repo_root)
        run_ids = _session_runs(repo_root, store)
        problems = [] if finishing else _unconfirmed_execution(repo_root, store)
        problems += _shared_with_others(repo_root, session_id, run_ids)
        problems += [b for b in (owner_blocker(repo_root, r) for r in run_ids) if b is not None]
        purges = {r: plan_run_purge(repo_root, r, root) for r in run_ids}  # read-only
        for purge in purges.values():
            # A run branch the user has checked out (any worktree the purge keeps) is never
            # deleted, so the deletion is refused before the marker rather than left partial.
            problems += [blocked_message(ref, path) for ref, path in purge.blocked_refs.items()]
        if problems:
            raise SessionConflict("cannot delete the session: " + "; ".join(problems))
        if not finishing:
            rounds = {item.run_id: item for item in store.read().rounds}
            pending: list[str] = []
            for run_id in run_ids:
                item = rounds.get(run_id) or RoundRecord(run_id=run_id, plan_revision=1)
                _, delivery, detail = _round_delivery(repo_root, store, item)
                if delivery != "delivered":
                    pending.append(f"{run_id} ({detail})")
            if pending and not include_undelivered:
                raise SessionConflict(
                    "these rounds were not delivered (or their applied state is unknown) and "
                    "would be deleted with the session: "
                    + "; ".join(pending)
                    + ". Confirm deleting them explicitly."
                )
            marker = {"at": iso(datetime.now(UTC)), "runs": run_ids}
            atomic_write(store.dir / DELETING, json.dumps(marker) + "\n")
        for run_id in run_ids:
            execute_run_purge(repo_root, purges[run_id])
        prefix = f"{SESSION_REFS}/{store.session_id}/"
        refs = _git_lines(repo_root, "for-each-ref", "--format=%(refname)", prefix)
        if refs is None:
            raise SessionError("cannot list the session's refs; deletion can be retried")
        removed: list[str] = []
        for ref in refs:
            if ref.startswith(prefix):
                if _git_lines(repo_root, "update-ref", "-d", ref) is None:
                    raise SessionError(f"cannot delete {ref}; deletion can be retried")
                removed.append(ref)
        with owner._mutex:  # pyright: ignore[reportPrivateUsage]
            owner._close_events()  # pyright: ignore[reportPrivateUsage]
            keep = {SESSION_JSON, DELETING}
            for child in list(store.dir.iterdir()):
                if child.name in keep:
                    continue
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            (store.dir / SESSION_JSON).unlink()  # now hidden from listings: nothing else remains
            (store.dir / DELETING).unlink()
            store.dir.rmdir()
        return SessionDeletion(session_id, tuple(run_ids), tuple(removed))
    finally:
        with owner._mutex:  # pyright: ignore[reportPrivateUsage]
            owner._close_events()  # pyright: ignore[reportPrivateUsage]
        owner._lock.release()  # pyright: ignore[reportPrivateUsage]
