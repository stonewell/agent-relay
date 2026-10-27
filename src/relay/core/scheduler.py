"""Bounded foreground DAG scheduling (design section 8.2)."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .specs.graph import Graph, GraphNode

if TYPE_CHECKING:
    from .events import EventLog
    from .store import RunStore

State = Literal["pending", "running", "done", "failed", "blocked", "cancelled"]


@dataclass(frozen=True)
class Outcome:
    state: State
    exit_code: int = 0
    reason: str | None = None


class SchedulerControl:
    """Thread-safe mailbox between a session owner and one scheduler loop (design 8.7).

    The owner can requeue an answered node, pause or resume dispatch, keep the loop alive
    while operator answers may still arrive (`hold`), and observe how many workers run. Only
    the scheduler mutates node states; this object just carries intent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requeue: list[str] = []
        self._admitting = True
        self._holding = False
        self.running = 0
        self.store: RunStore | None = None
        self.events: EventLog | None = None
        self.cancel = threading.Event()
        self.cancelled_nodes: set[str] = set()

    def bind(
        self,
        store: RunStore,
        events: EventLog,
        cancel: threading.Event,
        cancelled_nodes: set[str],
    ) -> None:
        """Called by the run once its shared writer exists."""
        self.store, self.events, self.cancel, self.cancelled_nodes = (
            store,
            events,
            cancel,
            cancelled_nodes,
        )

    @property
    def bound(self) -> bool:
        return self.store is not None

    @property
    def admitting(self) -> bool:
        return self._admitting

    @property
    def holding(self) -> bool:
        return self._holding

    def pause(self) -> None:
        self._admitting = False

    def resume(self) -> None:
        self._admitting = True

    def hold(self, value: bool) -> None:
        self._holding = value

    def requeue(self, key: str) -> None:
        with self._lock:
            if key not in self._requeue:
                self._requeue.append(key)

    @property
    def has_commands(self) -> bool:
        with self._lock:
            return bool(self._requeue)

    def take_requeue(self) -> list[str]:
        with self._lock:
            taken, self._requeue = self._requeue, []
            return taken


async def schedule(
    graph: Graph,
    *,
    concurrency: int,
    capacities: dict[str, int],
    choose: Callable[[GraphNode, dict[str, int]], str | None],
    execute: Callable[[GraphNode, str, threading.Event], Coroutine[Any, Any, Outcome]],
    transition: Callable[[str, Outcome], None],
    overlaps: Callable[[GraphNode, GraphNode], bool] = lambda a, b: False,
    cancellation: threading.Event | None = None,
    cancelled_nodes: set[str] | None = None,
    initial: dict[str, State] | None = None,
    control: SchedulerControl | None = None,
) -> dict[str, Outcome]:
    """One coordinator owns dispatch; workers report outcomes and cooperatively stop.

    ``choose`` returns None when an eligible agent is busy. Rejected routing is handled by
    ``execute`` with an empty agent, so dispatch cannot deadlock on unavailable workers.
    """
    if concurrency < 1 or any(value < 1 for value in capacities.values()):
        raise ValueError("scheduler capacities must be positive")
    nodes = {node.id: node for node in graph.nodes}
    results = {key: Outcome((initial or {}).get(key, "pending")) for key in nodes}
    running: dict[asyncio.Task[Outcome], tuple[str, str, threading.Event]] = {}
    stopping = False

    def set_state(key: str, outcome: Outcome) -> None:
        results[key] = outcome
        transition(key, outcome)

    def stop() -> None:
        nonlocal stopping
        stopping = True
        for _, _, event in running.values():
            event.set()
        for key in nodes:
            if results[key].state == "pending":
                set_state(key, Outcome("cancelled", 4, "run cancelled"))

    def reopen(keys: list[str]) -> None:
        """Requeue answered nodes, then dependents blocked solely by them (section 8.7)."""
        for key in keys:
            current = results.get(key)
            if (
                current is not None
                and current.state == "blocked"
                and (current.reason or "").startswith("needs_input")
            ):
                set_state(key, Outcome("pending"))
        changed = True
        while changed:
            changed = False
            for key, node in nodes.items():
                current = results[key]
                if (
                    current.state == "blocked"
                    and current.reason == "dependency unsuccessful"
                    and all(
                        results[d].state in {"done", "pending", "running"} for d in node.depends_on
                    )
                ):
                    set_state(key, Outcome("pending"))
                    changed = True

    try:
        while True:
            if control is not None:
                reopen(control.take_requeue())
                control.running = len(running)
            if cancellation is not None and cancellation.is_set():
                stop()
            for key in cancelled_nodes or ():
                if key in results and results[key].state == "pending":
                    set_state(key, Outcome("cancelled", 4, "node cancelled"))
                for running_key, _, event in running.values():
                    if key == running_key:
                        event.set()
            changed = True
            while changed:
                changed = False
                for key, node in nodes.items():
                    if results[key].state == "pending" and any(
                        results[d].state in {"failed", "blocked", "cancelled"}
                        for d in node.depends_on
                    ):
                        set_state(key, Outcome("blocked", 6, "dependency unsuccessful"))
                        changed = True
            if not stopping and (control is None or control.admitting):
                ready = sorted(
                    (
                        node
                        for key, node in nodes.items()
                        if results[key].state == "pending"
                        and all(results[d].state == "done" for d in node.depends_on)
                    ),
                    key=lambda n: (-n.priority, n.id),
                )
                for node in ready:
                    if len(running) >= concurrency:
                        break
                    if any(overlaps(node, nodes[key]) for key, _, _ in running.values()):
                        continue
                    load = dict(Counter(agent for _, agent, _ in running.values()))
                    agent = choose(node, load)
                    if agent is None:
                        continue
                    if agent and load.get(agent, 0) >= capacities[agent]:
                        continue
                    event = threading.Event()
                    set_state(node.id, Outcome("running"))
                    future = asyncio.create_task(execute(node, agent, event))
                    running[future] = (node.id, agent, event)
            if not running:
                waiting = any(result.state == "pending" for result in results.values())
                if (
                    control is not None
                    and not stopping
                    and (
                        control.holding
                        or control.has_commands
                        or (waiting and not control.admitting)
                    )
                ):
                    await asyncio.sleep(0.05)  # an owner may still answer, resume or finish
                    continue
                if waiting:
                    raise RuntimeError("scheduler has ready nodes but no dispatchable worker")
                break
            if control is not None:
                control.running = len(running)
            completed, _ = await asyncio.wait(
                running, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
            )
            for future in completed:
                key, _, _ = running.pop(future)
                if control is not None:
                    control.running = len(running)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = Outcome("failed", 1, str(exc))
                set_state(key, outcome)
                if graph.policy.on_failure == "fail-fast" and outcome.state == "failed":
                    stop()
        return results
    except BaseException:
        stop()
        # A cancelled coroutine must not leave a worker thread/process behind.
        await asyncio.gather(*running, return_exceptions=True)
        raise
