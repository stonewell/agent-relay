"""Admission and child lifetime for the foreground completion service.

All entry points belong to one asyncio loop. Permits cover the HTTP response lifetime:
the route must call ``handle.drain()`` after sending or abandoning its response.
"""

from __future__ import annotations

import asyncio
import math
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from relay.adapters.completion import (
    CompletionCapable,
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionResult,
    CompletionTerminationError,
    aggregate_usage,
)

from .render import render_repair

DRAIN_GRACE_S = 15.0


@dataclass(frozen=True)
class GatewayLimits:
    concurrency: int = 4
    timeout_s: float = 1800
    max_request_bytes: int = 8 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("concurrency", "max_request_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.timeout_s, bool) or not math.isfinite(self.timeout_s):
            raise ValueError("timeout_s must be positive and finite")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")


class CompletionHandle:
    def __init__(self, runtime: GatewayRuntime, agent: str) -> None:
        self._runtime = runtime
        self._agent = agent
        self._cancel = threading.Event()
        self._released = False
        self._quarantined = False
        self.result: asyncio.Task[CompletionResult]

    def cancel(self) -> None:
        self._cancel.set()

    async def drain(self) -> None:
        """Cancel pending work and release only after real drainage; cleanup never masks errors."""
        self.cancel()
        while not self.result.done():
            try:
                await asyncio.shield(self.result)
            except asyncio.CancelledError:
                # Repeated ASGI cancellation must not abandon the child future.
                continue
            except Exception:
                break
        if not self.result.cancelled():
            self.result.exception()  # consume a failed task even after the client disconnected
        if not self._released and not self._quarantined:
            self._released = True
            self._runtime._release(self)  # pyright: ignore[reportPrivateUsage]


class GatewayRuntime:
    def __init__(self, limits: GatewayLimits, agent_limits: Mapping[str, int]) -> None:
        if any(isinstance(v, bool) or v < 1 for v in agent_limits.values()):
            raise ValueError("agent concurrency must be positive")
        self.limits = limits
        self._agent_limits = dict(agent_limits)
        self._counts: dict[str, int] = {}
        self._handles: set[CompletionHandle] = set()
        self._closed = False
        self._healthy = True
        self._workers: set[asyncio.Task[CompletionResult]] = set()

    @property
    def active(self) -> int:
        return len(self._handles)

    @property
    def healthy(self) -> bool:
        return self._healthy

    def quarantine(self) -> None:
        """Stop admission after child termination outside a permit could not be confirmed."""
        self._healthy = False

    def start(
        self,
        backend: CompletionCapable,
        request: CompletionRequest,
        *,
        validate: Callable[[CompletionResult], CompletionResult] | None = None,
        timeout_s: float | None = None,
        max_output_bytes: int | None = None,
    ) -> CompletionHandle:
        # No await between checking capacity and reserving both counters.
        if self._closed:
            # Shutdown aborts request waiters; discovery must not mistake it for a missing CLI.
            raise CompletionError(
                "Gateway is shutting down", status=503, code="request_cancelled"
            )
        if not self._healthy:
            raise CompletionError(
                "Gateway is not accepting requests", status=503, code="unavailable"
            )
        count = self._counts.get(backend.id, 0)
        cap = self._agent_limits.get(backend.id, self.limits.concurrency)
        if self.active >= self.limits.concurrency or count >= cap:
            raise CompletionError(
                "Gateway capacity is full", status=429, code="rate_limit_exceeded"
            )
        limits = replace(
            self.limits,
            timeout_s=min(
                self.limits.timeout_s,
                self.limits.timeout_s if timeout_s is None else timeout_s,
            ),
            max_output_bytes=min(
                self.limits.max_output_bytes,
                self.limits.max_output_bytes if max_output_bytes is None else max_output_bytes,
            ),
        )
        handle = CompletionHandle(self, backend.id)
        self._counts[backend.id] = count + 1
        self._handles.add(handle)
        handle.result = asyncio.create_task(
            self._execute(handle, backend, request, validate, limits)
        )
        return handle

    async def complete_when_available(
        self,
        backend: CompletionCapable,
        request: CompletionRequest,
        *,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        """Run background discovery under the same admission and drainage as HTTP work."""
        while True:
            if cancel.is_set():
                raise CompletionError("Completion cancelled", status=503, code="request_cancelled")
            try:
                handle = self.start(
                    backend, request, timeout_s=timeout_s, max_output_bytes=max_output_bytes
                )
                break
            except CompletionError as exc:
                if exc.status != 429:
                    raise
                # Saturation defers a probe; it is not an observation about the alias.
                await asyncio.sleep(0.05)
        try:
            while not handle.result.done():
                if cancel.is_set():
                    handle.cancel()
                await asyncio.wait({handle.result}, timeout=0.05)
            return handle.result.result()
        finally:
            await handle.drain()

    def _release(self, handle: CompletionHandle) -> None:
        self._handles.discard(handle)
        agent = handle._agent  # pyright: ignore[reportPrivateUsage]
        self._counts[agent] -= 1

    async def shutdown(self) -> None:
        self._closed = True
        handles = tuple(self._handles)
        for handle in handles:
            handle.cancel()
        if handles:
            # Each handle drains its thread; cancellation of shutdown cannot lose ownership.
            drainage = asyncio.gather(*(handle.drain() for handle in handles))
            while not drainage.done():
                try:
                    await asyncio.shield(drainage)
                except asyncio.CancelledError:
                    continue

    async def _call(
        self,
        handle: CompletionHandle,
        backend: CompletionCapable,
        request: CompletionRequest,
        cwd: Path,
        deadline: float,
        validate: Callable[[CompletionResult], CompletionResult] | None,
        max_output_bytes: int,
    ) -> CompletionResult:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompletionError("Completion deadline expired", status=504, code="timeout")
        cancellation = handle._cancel  # pyright: ignore[reportPrivateUsage]
        if cancellation.is_set():
            raise CompletionError("Completion cancelled", status=503, code="request_cancelled")

        def invoke_and_validate() -> CompletionResult:
            result = backend.complete(
                request,
                cwd=cwd,
                cancel=cancellation,
                timeout_s=remaining,
                max_output_bytes=max_output_bytes,
            )
            if cancellation.is_set():
                raise CompletionError("Completion cancelled", status=503, code="request_cancelled")
            # A result that finished in time is validated even if the deadline passes meanwhile.
            # Validation is bounded separately and belongs to this same owned worker. It
            # must never block the ASGI loop or escape the request deadline/drain accounting;
            # the loop below still times out a worker that has not finished by the deadline.
            return validate(result) if validate is not None else result

        worker = asyncio.create_task(asyncio.to_thread(invoke_and_validate))
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        interrupted = False
        try:
            # Polling observes disconnect without cancelling the to_thread task itself.
            while not worker.done() and not cancellation.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.wait({worker}, timeout=min(0.05, remaining))
        except asyncio.CancelledError:
            interrupted = True
            cancellation.set()
        # A worker that finished in the same tick the deadline passed keeps its result.
        expired = not worker.done() and time.monotonic() >= deadline
        if not worker.done():
            cancellation.set()
            drain_until = time.monotonic() + DRAIN_GRACE_S
            while not worker.done() and time.monotonic() < drain_until:
                try:
                    await asyncio.wait({worker}, timeout=max(0, drain_until - time.monotonic()))
                except asyncio.CancelledError:
                    interrupted = True
            if not worker.done():
                worker.add_done_callback(
                    lambda task: task.exception() if not task.cancelled() else None
                )
                raise CompletionTerminationError()
        if expired or interrupted or cancellation.is_set():
            # Consume the worker exception, but preserve termination failures over cancellation.
            failure = worker.exception()
            if isinstance(failure, CompletionTerminationError):
                raise failure
            if expired:
                raise CompletionError("Completion deadline expired", status=504, code="timeout")
            raise CompletionError("Completion cancelled", status=503, code="request_cancelled")
        return worker.result()

    async def _execute(
        self,
        handle: CompletionHandle,
        backend: CompletionCapable,
        request: CompletionRequest,
        validate: Callable[[CompletionResult], CompletionResult] | None,
        limits: GatewayLimits,
    ) -> CompletionResult:
        directory: Path | None = None
        deadline = time.monotonic() + limits.timeout_s
        try:
            directory = Path(tempfile.mkdtemp(prefix="relay-completion-"))
            first_invalid: CompletionInvalid | None = None
            current = request
            for attempt in range(2):
                try:
                    result = await self._call(
                        handle,
                        backend,
                        current,
                        directory,
                        deadline,
                        validate,
                        limits.max_output_bytes,
                    )
                    if first_invalid is not None:
                        result = replace(
                            result, usage=aggregate_usage(first_invalid.usage, result.usage)
                        )
                    return result
                except CompletionInvalid as exc:
                    if request.output_schema is None or attempt:
                        raise
                    first_invalid = exc
                    current = replace(
                        request, prompt=render_repair(request.prompt, exc.raw, str(exc))
                    )
            raise AssertionError("completion repair loop exhausted")
        except CompletionTerminationError:
            self._healthy = False
            handle._quarantined = True  # pyright: ignore[reportPrivateUsage]
            raise
        finally:
            if directory is not None and not handle._quarantined:  # pyright: ignore[reportPrivateUsage]
                try:
                    shutil.rmtree(directory)
                except OSError:
                    # Retain private residue, stop admission rather than silently lose cleanup.
                    self._healthy = False
