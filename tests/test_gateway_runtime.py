"""Request ownership, cancellation, response lifetime and repair bounds."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from relay.adapters.completion import (
    CompletionError,
    CompletionInvalid,
    CompletionRequest,
    CompletionResult,
    CompletionSupport,
    CompletionTerminationError,
    CompletionUsage,
)
from relay.gateway import runtime as module
from relay.gateway.render import render_repair
from relay.gateway.runtime import GatewayLimits, GatewayRuntime


class Backend:
    id = "fake"

    def __init__(self, results: list[CompletionResult | Exception] | None = None) -> None:
        self.results = results or [CompletionResult(content="ok")]
        self.calls: list[CompletionRequest] = []
        self.paths: list[Path] = []
        self.timeouts: list[float] = []
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.hang = False
        self.delay = 0.0

    def inspect(self, *, timeout_s: float = 10) -> CompletionSupport | None:
        return None

    def complete(
        self,
        request: CompletionRequest,
        *,
        cwd: Path,
        cancel: threading.Event,
        timeout_s: float,
        max_output_bytes: int,
    ) -> CompletionResult:
        self.calls.append(request)
        self.paths.append(cwd)
        self.timeouts.append(timeout_s)
        (cwd / "scratch").write_text("private", encoding="utf-8")
        self.started.set()
        if self.hang:
            assert cancel.wait(3)
            time.sleep(0.05)
            self.stopped.set()
            raise CompletionError("cancelled")
        time.sleep(self.delay)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def test_capacity_is_atomic_shared_by_aliases_and_held_until_response_drain() -> None:
    async def scenario() -> None:
        runtime = GatewayRuntime(GatewayLimits(concurrency=2), {"fake": 1})
        backend = Backend()
        handle = runtime.start(backend, CompletionRequest("hello", "alias1"))
        with pytest.raises(CompletionError) as caught:
            runtime.start(backend, CompletionRequest("hello", "alias2"))
        assert caught.value.status == 429
        assert (await handle.result).content == "ok"
        assert runtime.active == 1  # still holds admission while HTTP sends to a slow client
        assert not backend.paths[0].exists()
        await handle.drain()
        await handle.drain()
        assert runtime.active == 0
        await runtime.shutdown()
        with pytest.raises(CompletionError) as caught:
            runtime.start(backend, CompletionRequest("hello"))
        assert caught.value.status == 503

    asyncio.run(scenario())


def test_disconnect_and_repeated_task_cancellation_drain_worker_before_permit_release() -> None:
    async def scenario() -> None:
        runtime = GatewayRuntime(GatewayLimits(concurrency=1), {})
        backend = Backend()
        backend.hang = True
        handle = runtime.start(backend, CompletionRequest("hello"))
        while not backend.started.is_set():
            await asyncio.sleep(0.01)
        handle.cancel()
        drain = asyncio.create_task(handle.drain())
        await asyncio.sleep(0.01)
        drain.cancel()
        await asyncio.sleep(0)
        drain.cancel()
        assert runtime.active == 1
        await drain
        assert backend.stopped.is_set()
        assert runtime.active == 0
        assert not backend.paths[0].exists()

    asyncio.run(scenario())


def test_repair_has_one_shared_deadline_and_aggregate_usage() -> None:
    async def scenario() -> None:
        usage = CompletionUsage(5, 2, 1, 0)
        backend = Backend(
            [
                CompletionInvalid("bad envelope", raw="bad", usage=usage),
                CompletionResult(envelope={"content": "ok", "tool_calls": []}, usage=usage),
            ]
        )
        backend.delay = 0.04
        runtime = GatewayRuntime(GatewayLimits(timeout_s=2), {})
        handle = runtime.start(backend, CompletionRequest("original", output_schema={}))
        result = await handle.result
        assert result.usage == CompletionUsage(10, 4, 2, 0)
        assert len(backend.calls) == 2
        assert backend.paths[0] == backend.paths[1]
        assert backend.timeouts[1] < backend.timeouts[0]
        assert backend.calls[1].prompt.startswith("original")
        assert "bad envelope" in backend.calls[1].prompt
        # The shared renderer keeps the failed output and error as one JSON data record.
        assert backend.calls[1].prompt == render_repair("original", "bad", "bad envelope")
        await handle.drain()

    asyncio.run(scenario())


def test_validator_failure_repairs_but_plain_and_provider_errors_do_not() -> None:
    async def scenario() -> None:
        backend = Backend()
        runtime = GatewayRuntime(GatewayLimits(), {})

        def invalid(result: CompletionResult) -> CompletionResult:
            raise CompletionInvalid("tool schema", raw=result.raw)

        handle = runtime.start(backend, CompletionRequest("x", output_schema={}), validate=invalid)
        with pytest.raises(CompletionInvalid):
            await handle.result
        assert len(backend.calls) == 2
        await handle.drain()
        for error in [CompletionInvalid("plain malformed"), CompletionError("provider failure")]:
            backend = Backend([error])
            handle = runtime.start(backend, CompletionRequest("x"))
            with pytest.raises(CompletionError):
                await handle.result
            assert len(backend.calls) == 1
            await handle.drain()

    asyncio.run(scenario())


def test_timeout_drains_and_shutdown_cancels_all_requests() -> None:
    async def scenario() -> None:
        runtime = GatewayRuntime(GatewayLimits(timeout_s=0.05), {})
        backend = Backend()
        backend.hang = True
        handle = runtime.start(backend, CompletionRequest("hello"))
        with pytest.raises(CompletionError) as caught:
            await handle.result
        assert caught.value.status == 504 and backend.stopped.is_set()
        await handle.drain()
        runtime = GatewayRuntime(GatewayLimits(), {})
        handle = runtime.start(backend, CompletionRequest("hello"))
        await asyncio.sleep(0.02)
        await runtime.shutdown()
        assert handle.result.done() and runtime.active == 0

    asyncio.run(scenario())


def test_unconfirmed_termination_quarantines_capacity_and_retains_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "DRAIN_GRACE_S", 0.03)

    async def scenario() -> None:
        backend = Backend([CompletionTerminationError()])
        runtime = GatewayRuntime(GatewayLimits(), {})
        handle = runtime.start(backend, CompletionRequest("x"))
        with pytest.raises(CompletionTerminationError):
            await handle.result
        await handle.drain()
        assert not runtime.healthy and runtime.active == 1
        assert backend.paths[0].exists()
        with pytest.raises(CompletionError) as caught:
            runtime.start(backend, CompletionRequest("y"))
        assert caught.value.status == 503
        # This fake has no live process; manually remove its deliberately retained residue.
        (backend.paths[0] / "scratch").unlink()
        backend.paths[0].rmdir()

    asyncio.run(scenario())


def test_validation_runs_off_loop_and_consumes_the_request_deadline() -> None:
    async def scenario() -> None:
        loop_thread = threading.get_ident()
        validation_started = threading.Event()

        def slow_validation(result: CompletionResult) -> CompletionResult:
            assert threading.get_ident() != loop_thread
            validation_started.set()
            time.sleep(0.12)
            return result

        backend = Backend()
        runtime = GatewayRuntime(GatewayLimits(timeout_s=0.04), {})
        handle = runtime.start(backend, CompletionRequest("x"), validate=slow_validation)
        while not validation_started.is_set():
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        assert not handle.result.done()  # event loop remained responsive during validation
        with pytest.raises(CompletionError) as caught:
            await handle.result
        assert caught.value.status == 504
        assert not backend.paths[0].exists()
        await handle.drain()
        assert runtime.active == 0

    asyncio.run(scenario())


def test_result_finished_as_deadline_passes_is_kept_not_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The backend finishes exactly as the request clock crosses the deadline; the completed,
    # valid result must win over expiry in the worker and in the polling loop.
    offset = [0.0]

    class Clock:
        @staticmethod
        def monotonic() -> float:
            return time.monotonic() + offset[0]

    monkeypatch.setattr(module, "time", Clock)

    class LateBackend(Backend):
        def complete(
            self,
            request: CompletionRequest,
            *,
            cwd: Path,
            cancel: threading.Event,
            timeout_s: float,
            max_output_bytes: int,
        ) -> CompletionResult:
            result = super().complete(
                request,
                cwd=cwd,
                cancel=cancel,
                timeout_s=timeout_s,
                max_output_bytes=max_output_bytes,
            )
            offset[0] += timeout_s + 1
            return result

    async def scenario() -> None:
        validated: list[CompletionResult] = []

        def validate(result: CompletionResult) -> CompletionResult:
            validated.append(result)
            return result

        runtime = GatewayRuntime(GatewayLimits(timeout_s=5), {})
        handle = runtime.start(LateBackend(), CompletionRequest("x"), validate=validate)
        assert (await handle.result).content == "ok"
        assert len(validated) == 1
        await handle.drain()
        assert runtime.active == 0

    asyncio.run(scenario())
