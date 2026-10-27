"""Public worker progress: normalization, sanitization and bounded delivery (design 7.4, 11; 2.26).

Progress is a separate, optional, *lossy* channel. It never changes state or authorizes
anything: unknown event types are inspectable diagnostics and cannot trigger control actions.
It is fed from the pipe-draining thread, so every operation here is non-blocking: a slow or
disconnected subscriber loses transient frames behind an explicit gap marker, but cannot stall
pipe draining, cancellation, owner commands or final result parsing.

Everything shown is untrusted text: terminal escapes and control characters are stripped,
secrets are redacted and nothing is interpreted as markup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast, runtime_checkable

from .route_log import redact_secrets
from .store import iso

__all__ = [
    "ClaudeStreamParser",
    "CodexEventParser",
    "ProgressChannel",
    "ProgressParser",
    "ProgressRecord",
    "ProgressSubscription",
    "ProgressTap",
    "Update",
    "split_frames",
    "sanitize_text",
]

Kind = Literal["text", "tool", "phase", "gap", "diagnostic"]
_TRANSIENT: frozenset[str] = frozenset({"text", "tool", "diagnostic"})

DEFAULT_QUEUE_RECORDS = 256
DEFAULT_FRAME_BYTES = 8192
_TOOL_SUMMARY_CHARS = 160

_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_OTHER = re.compile(r"\x1b[@-Z\\-_]|\x1b[()][0-9A-Za-z]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def sanitize_text(text: str) -> str:
    """Plain, secret-free text safe to render in a terminal (design section 11)."""
    cleaned = _ANSI_OSC.sub("", text)
    cleaned = _ANSI_CSI.sub("", cleaned)
    cleaned = _ANSI_OTHER.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _CONTROL.sub("", cleaned)
    return redact_secrets(cleaned)


def split_frames(text: str, max_bytes: int = DEFAULT_FRAME_BYTES) -> list[str]:
    """Split `text` into frames of at most `max_bytes` UTF-8 bytes without cutting a character."""
    if max_bytes < 4:
        raise ValueError("frame size must allow one UTF-8 character")
    frames: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > max_bytes:
            frames.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current or not frames:
        frames.append("".join(current))
    return frames


@dataclass(frozen=True)
class ProgressRecord:
    seq: int
    ts: str
    kind: Kind
    text: str
    run_id: str
    node_id: str
    attempt: int
    call_id: str | None = None
    dropped: int = 0
    """For `gap` records: how many transient frames were dropped or coalesced before this one."""

    def same_source(self, other: ProgressRecord) -> bool:
        return (self.run_id, self.node_id, self.attempt, self.call_id) == (
            other.run_id,
            other.node_id,
            other.attempt,
            other.call_id,
        )


class ProgressSubscription:
    """One consumer's bounded queue. Offering never blocks and never raises."""

    def __init__(self, capacity: int, frame_bytes: int) -> None:
        self._capacity = capacity
        self._frame_bytes = frame_bytes
        self._lock = threading.Lock()
        self._items: deque[ProgressRecord] = deque()
        self._dropped = 0
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._event = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._items.clear()
        self._notify()

    def _notify(self) -> None:
        loop, event = self._loop, self._event
        if loop is not None and event is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):  # loop shutting down: the consumer is gone
                loop.call_soon_threadsafe(event.set)

    def offer(self, record: ProgressRecord) -> None:
        with self._lock:
            if self._closed:
                return
            items = self._items
            # Under pressure, coalesce consecutive text from one source into a larger frame.
            if (
                record.kind == "text"
                and items
                and len(items) >= self._capacity // 2
                and items[-1].kind == "text"
                and items[-1].same_source(record)
                and len(items[-1].text.encode()) + len(record.text.encode()) <= self._frame_bytes
            ):
                items[-1] = replace(items[-1], text=items[-1].text + record.text)
                self._dropped += 1
            else:
                if len(items) >= self._capacity:
                    self._evict(items)
                items.append(record)
        self._notify()

    def _evict(self, items: deque[ProgressRecord]) -> None:
        for index, existing in enumerate(items):
            if existing.kind in _TRANSIENT:
                del items[index]
                break
        else:
            items.popleft()  # only lifecycle phases are queued: the oldest must go
        self._dropped += 1

    def drain(self) -> list[ProgressRecord]:
        """Everything queued, preceded by an explicit gap marker when frames were lost."""
        with self._lock:
            out = list(self._items)
            self._items.clear()
            dropped, self._dropped = self._dropped, 0
        if dropped:
            first = out[0] if out else None
            gap = ProgressRecord(
                seq=first.seq if first else 0,
                ts=iso(datetime.now(UTC)),
                kind="gap",
                text=f"{dropped} progress frame(s) were dropped or merged",
                run_id=first.run_id if first else "",
                node_id=first.node_id if first else "",
                attempt=first.attempt if first else 0,
                dropped=dropped,
            )
            out.insert(0, gap)
        return out

    async def next_batch(self, timeout_s: float | None = None) -> list[ProgressRecord]:
        """Wait (without blocking producers) for records; an empty list means timeout/closed."""
        if self._event is None:
            self.bind(asyncio.get_running_loop())
        assert self._event is not None
        batch = self.drain()
        if batch or self._closed:
            return batch
        self._event.clear()
        batch = self.drain()
        if batch:
            return batch
        try:
            await asyncio.wait_for(self._event.wait(), timeout_s)
        except TimeoutError:
            return []
        return self.drain()


class ProgressChannel:
    """Fan-out of progress records to bounded subscriber queues (process-local, lossy)."""

    def __init__(
        self,
        *,
        queue_records: int = DEFAULT_QUEUE_RECORDS,
        frame_bytes: int = DEFAULT_FRAME_BYTES,
    ) -> None:
        if queue_records < 2:
            raise ValueError("queue_records must be at least 2")
        self._queue_records = queue_records
        self._frame_bytes = frame_bytes
        self._lock = threading.Lock()
        self._subs: list[ProgressSubscription] = []
        self._seq = 0

    def subscribe(self) -> ProgressSubscription:
        sub = ProgressSubscription(self._queue_records, self._frame_bytes)
        with self._lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: ProgressSubscription) -> None:
        sub.close()
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(
        self,
        kind: Kind,
        text: str,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        call_id: str | None = None,
    ) -> None:
        """Sanitize, frame and fan out. Safe to call from any thread; never blocks."""
        clean = sanitize_text(text)
        if not clean.strip() and kind != "phase":
            return
        frames = split_frames(clean, self._frame_bytes)
        with self._lock:
            subs = list(self._subs)
            first = self._seq + 1
            self._seq += len(frames)
        stamp = iso(datetime.now(UTC))
        for offset, frame in enumerate(frames):
            record = ProgressRecord(
                first + offset, stamp, kind, frame, run_id, node_id, attempt, call_id
            )
            for sub in subs:
                sub.offer(record)


# --- normalization of supported CLI output ------------------------------------------------------


Update = tuple[Kind, str]


@runtime_checkable
class ProgressParser(Protocol):
    """Turns one line of a CLI's public stream into zero or more bounded updates."""

    def feed(self, line: str) -> list[Update]: ...


def _json_object(line: str) -> dict[str, Any] | None:
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _short(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= _TOOL_SUMMARY_CHARS else text[: _TOOL_SUMMARY_CHARS - 1] + "…"


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    """The dict blocks of `event["message"]["content"]`, tolerating any other shape."""
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = cast("dict[str, Any]", message).get("content")
    if not isinstance(content, list):
        return []
    return [cast("dict[str, Any]", b) for b in cast("list[Any]", content) if isinstance(b, dict)]


class ClaudeStreamParser:
    """`claude -p --output-format stream-json --verbose` events (public, documented shape)."""

    def feed(self, line: str) -> list[Update]:
        event = _json_object(line)
        if event is None:
            return []
        kind = event.get("type")
        if kind == "system":
            if event.get("subtype") == "init":
                model = event.get("model")
                return [("phase", f"agent started{f' ({_short(model)})' if model else ''}")]
            return [("diagnostic", f"system event {_short(event.get('subtype', '?'))}")]
        if kind == "assistant":
            updates: list[Update] = []
            for typed in _content_blocks(event):
                if typed.get("type") == "text" and isinstance(typed.get("text"), str):
                    updates.append(("text", typed["text"]))
                elif typed.get("type") == "tool_use":
                    summary = typed.get("input")
                    args = ""
                    if isinstance(summary, dict) and summary:
                        args = f": {_short(next(iter(cast('dict[str, Any]', summary).values())))}"
                    updates.append(("tool", f"{_short(typed.get('name', 'tool'))}{args}"))
            return updates
        if kind == "user":
            return [
                ("tool", "tool result (error)" if block.get("is_error") else "tool result")
                for block in _content_blocks(event)
                if block.get("type") == "tool_result"
            ]
        if kind == "result":
            return [("phase", "agent finished")]
        if kind == "stream_event":
            return []  # partial deltas are not republished; the full message follows
        return [("diagnostic", f"unknown event {_short(kind)}")]


class CodexEventParser:
    """`codex exec --json` JSONL events."""

    def feed(self, line: str) -> list[Update]:
        event = _json_object(line)
        if event is None:
            return []
        kind = event.get("type")
        item: Any = event.get("item")
        item_type = cast("dict[str, Any]", item).get("type") if isinstance(item, dict) else None
        data = cast("dict[str, Any]", item) if isinstance(item, dict) else {}
        if kind == "turn.started":
            return [("phase", "agent started")]
        if kind == "turn.completed":
            return [("phase", "agent finished")]
        if kind in ("turn.failed", "error"):
            return [
                ("diagnostic", f"agent error: {_short(event.get('message') or event.get('error'))}")
            ]
        if kind == "item.completed" and item_type == "agent_message":
            return [("text", str(data.get("text", "")))]
        if kind == "item.started" and item_type == "command_execution":
            return [("tool", f"$ {_short(data.get('command', ''))}")]
        if kind == "item.completed" and item_type == "command_execution":
            return [("tool", f"command exited {data.get('exit_code')}")]
        if kind in ("thread.started", "item.started", "item.completed", "item.updated"):
            return []
        return [("diagnostic", f"unknown event {_short(kind)}")]


class ProgressTap:
    """Binds a parser and a channel to one attempt's identity; every method is non-blocking."""

    def __init__(
        self,
        channel: ProgressChannel,
        parser: ProgressParser | None,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
    ) -> None:
        self._channel = channel
        self._parser = parser
        self._ids = {"run_id": run_id, "node_id": node_id, "attempt": attempt}
        self._failed = False

    @property
    def streaming(self) -> bool:
        return self._parser is not None

    def phase(self, name: str) -> None:
        self._channel.publish("phase", name, **self._ids)  # type: ignore[arg-type]

    def feed(self, line: str) -> None:
        parser = self._parser
        if parser is None:
            return
        try:
            updates = parser.feed(line)
        except Exception:  # a parser bug must never disturb draining or the run
            if not self._failed:
                self._failed = True
                self._channel.publish("diagnostic", "progress parsing failed", **self._ids)  # type: ignore[arg-type]
            return
        for kind, text in updates:
            self._channel.publish(kind, text, **self._ids)  # type: ignore[arg-type]
