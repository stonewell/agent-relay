"""Bounded foreground DAG scheduling (design section 8.2)."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from .specs.graph import Graph, GraphNode

State = Literal["pending", "running", "done", "failed", "blocked", "cancelled"]


@dataclass(frozen=True)
class Outcome:
    state: State
    exit_code: int = 0
    reason: str | None = None


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

    try:
        while True:
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
            if not stopping:
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
                if any(result.state == "pending" for result in results.values()):
                    raise RuntimeError("scheduler has ready nodes but no dispatchable worker")
                break
            completed, _ = await asyncio.wait(
                running, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
            )
            for future in completed:
                key, _, _ = running.pop(future)
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
