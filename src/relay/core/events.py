"""`events.jsonl`: canonical append-only event log (design sections 4.4 and 11)."""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .specs.route import RouteEntry
from .store import iso

ENVELOPE = frozenset({"seq", "ts", "type", "owner_generation", "hashes"})


class EventError(ValueError):
    """An event does not match the catalog."""


class EventLogCorrupt(RuntimeError):
    """The log has an unparsable record that is not a torn trailing line, or broken `seq`."""


@dataclass(frozen=True)
class EventType:
    """Catalog entry: payload fields of one event type (section 11)."""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    open: bool = False  # the design does not enumerate all fields yet: extra fields allowed


def _t(required: str = "", optional: str = "", *, open: bool = False) -> EventType:  # noqa: A002
    return EventType(frozenset(required.split()), frozenset(optional.split()), open)


CATALOG: dict[str, EventType] = {
    # Run and supervisor
    "run.started": _t("config_hash base_commit allow_dirty", "sensitive_paths run_id"),
    "run.finished": _t("state", "integration_commit"),
    "supervisor.started": _t("generation"),
    "supervisor.recovered": _t("generation reconciled_nodes"),
    "supervisor.lock_failed": _t(open=True),
    # Node lifecycle
    "node.state": _t("node_id from to", "reason"),
    "attempt.phase": _t("node_id attempt phase"),
    "node.started": _t("node_id permissions enforcement_profile input_commit input_key cmd"),
    "node.finished": _t("node_id status", "output_commit exit duration usage"),
    "node.reused": _t("node_id", open=True),
    "node.invalidated": _t("node_id", open=True),
    # Routing
    "node.routed": _t("node_id route_seq decision"),
    "route.log.write_failed": _t(open=True),
    # Input (answer content lives in input_answers.yaml, never here)
    "input.requested": _t("node_id question_revision"),
    "input.recorded": _t("node_id question_revision submission_id provenance", "content_hash"),
    "input.rejected": _t("reason", "node_id"),
    # Verification, merge, delivery
    "verify.result": _t("node_id", open=True),
    "merge.result": _t(open=True),
    "integration.published": _t(open=True),
    "apply.started": _t("op_id target commit"),
    "apply.progress": _t("op_id step"),
    "apply.finished": _t("op_id status", "error"),
    "apply.recovered": _t("op_id action"),
    # Budget, spawn, other
    "budget.reserved": _t(open=True),
    "budget.settled": _t(open=True),
    "budget.exhausted": _t(open=True),
    "budget.overrun": _t(open=True),
    "spawn.accepted": _t(open=True),
    "spawn.rejected": _t(open=True),
    "permission.denied": _t("node_id requested effective"),
    "warning": _t("message", open=True),
    # Interactive sessions (phase 2c): ids, revisions, hashes and states only (section 11)
    "session.created": _t(open=True),
    "session.interrupted": _t(open=True),
    "turn.started": _t(open=True),
    "turn.finished": _t(open=True),
    "coordinator.started": _t(open=True),
    "coordinator.finished": _t(open=True),
    "context.selected": _t(open=True),
    "plan.proposed": _t("plan_revision", open=True),
    "plan.approved": _t("plan_revision", open=True),
    "plan.superseded": _t(open=True),
    "launch.prepared": _t("intent_id run_id", open=True),
    "launch.linked": _t("intent_id run_id", open=True),
    "admission.paused": _t(open=True),
    "admission.resumed": _t(open=True),
    "checkpoint.started": _t(open=True),
    "checkpoint.sealed": _t(open=True),
    "checkpoint.conflicted": _t(open=True),
}

_FORBIDDEN_PAYLOAD = frozenset({"answers"})


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    type: str
    owner_generation: int
    data: Mapping[str, Any] = field(default_factory=lambda: {})
    hashes: Mapping[str, str] | None = None

    def to_json(self) -> str:
        body: dict[str, Any] = {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "owner_generation": self.owner_generation,
        }
        if self.hashes:
            body["hashes"] = dict(self.hashes)
        body.update(self.data)
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Event:
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("event must be a JSON object")
        body: dict[str, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
        try:
            envelope = {k: body.pop(k) for k in ("seq", "ts", "type", "owner_generation")}
        except KeyError as exc:
            raise ValueError(f"event is missing {exc.args[0]!r}") from None
        hashes = body.pop("hashes", None)
        return cls(
            seq=int(envelope["seq"]),
            ts=str(envelope["ts"]),
            type=str(envelope["type"]),
            owner_generation=int(envelope["owner_generation"]),
            data=body,
            hashes=hashes,
        )


def validate_event(type_: str, data: Mapping[str, Any]) -> None:
    """Check a payload against the catalog (section 11)."""
    entry = CATALOG.get(type_)
    if entry is None:
        raise EventError(f"unknown event type {type_!r}")
    clash = ENVELOPE & data.keys()
    if clash:
        raise EventError(f"{type_}: payload fields {sorted(clash)} collide with the envelope")
    leaked = _FORBIDDEN_PAYLOAD & data.keys()
    if leaked:
        raise EventError(
            f"{type_}: {sorted(leaked)} must not appear in events (answers are private)"
        )
    missing = entry.required - data.keys()
    if missing:
        raise EventError(f"{type_}: missing fields {sorted(missing)}")
    if not entry.open:
        unknown = data.keys() - entry.required - entry.optional
        if unknown:
            raise EventError(f"{type_}: unexpected fields {sorted(unknown)}")
    if type_ == "node.routed":
        try:
            RouteEntry.model_validate(data["decision"])
        except ValueError as exc:
            raise EventError(f"node.routed: invalid decision: {exc}") from exc


def _parse_record(line: str) -> Event | None:
    try:
        return Event.from_json(line)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class ScanResult:
    events: list[Event]
    good_length: int  # bytes of the file that hold complete, valid records
    needs_newline: bool  # last record is valid but lacks its newline
    torn_bytes: int  # bytes of an unusable trailing fragment


def scan(path: Path) -> ScanResult:
    """Read every record. Only a trailing fragment may be bad; anything else is corruption."""
    if not path.exists():
        return ScanResult([], 0, False, 0)
    data = path.read_bytes()
    events: list[Event] = []
    offset = 0
    expected = 1
    while offset < len(data):
        end = data.find(b"\n", offset)
        last = end == -1
        chunk = data[offset:] if last else data[offset:end]
        event = _parse_record(chunk.decode("utf-8", errors="replace")) if chunk.strip() else None
        if event is None:
            if last:
                return ScanResult(events, offset, False, len(data) - offset)
            if not chunk.strip():
                offset = end + 1  # tolerate a blank line
                continue
            raise EventLogCorrupt(f"{path}: unparsable record at byte {offset}")
        if event.seq != expected:
            raise EventLogCorrupt(f"{path}: expected seq {expected}, found {event.seq}")
        expected += 1
        events.append(event)
        if last:
            return ScanResult(events, len(data), True, 0)
        offset = end + 1
    return ScanResult(events, offset, False, 0)


def read_events(path: Path) -> list[Event]:
    """Events of a log, ignoring a torn trailing record without modifying the file."""
    return scan(path).events


def replay(path: Path, apply: Callable[[Event], None], after_seq: int = 0) -> int:
    """Feed events with `seq > after_seq` to `apply` in order; returns the last `seq` seen.

    Consumers record the returned value and pass it back, so projections rebuilt from the
    canonical log apply each event once, however often replay is run (section 4.4).
    """
    last = after_seq
    for event in read_events(path):
        if event.seq > after_seq:
            apply(event)
            last = event.seq
    return last


class EventLog:
    """Single-writer appender for one run's `events.jsonl`."""

    def __init__(
        self,
        path: Path,
        *,
        owner_generation: int = 0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._lock = threading.RLock()
        self.path = path
        self.owner_generation = owner_generation
        self._now = now
        result = scan(path)
        if result.torn_bytes:
            with open(path, "r+b") as handle:  # discard only the torn trailing record
                handle.truncate(result.good_length)
                handle.flush()
                os.fsync(handle.fileno())
        self._next_seq = len(result.events) + 1
        self._handle = open(path, "ab")  # noqa: SIM115 - held for the life of the writer
        if result.needs_newline:
            self._write_line(b"")

    def append(
        self,
        type_: str,
        data: Mapping[str, Any] | None = None,
        *,
        hashes: Mapping[str, str] | None = None,
    ) -> Event:
        """Durably append one event; returns it only after the bytes are on disk."""
        with self._lock:
            payload = dict(data or {})
            validate_event(type_, payload)
            event = Event(
                seq=self._next_seq,
                ts=iso(self._now()),
                type=type_,
                owner_generation=self.owner_generation,
                data=payload,
                hashes=hashes,
            )
            self._write_line(event.to_json().encode("utf-8"))
            self._next_seq += 1
            return event

    def _write_line(self, body: bytes) -> None:
        self._handle.write(body + b"\n")  # one write call per record
        self._handle.flush()
        os.fsync(self._handle.fileno())

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._handle.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[Event]:
        return iter(read_events(self.path))
