"""Scheduling concurrency, failure and cancellation contracts."""

import asyncio
import threading

import pytest

from relay.core.scheduler import Outcome, schedule
from relay.core.scopes import patterns_overlap, writers_overlap
from relay.core.specs import Graph, GraphNode
from relay.core.specs.task import ScopeCheck


@pytest.mark.parametrize("cap", [1, 2])
def test_capacity_priority_and_dependencies(cap: int) -> None:
    async def run() -> None:
        graph = Graph(
            nodes=[
                GraphNode(id=n, category="test", goal="test", priority=p)
                for n, p in [("a", 0), ("b", 10), ("c", 2)]
            ]
        )
        active = 0
        maximum = 0
        order: list[str] = []

        async def execute(node: GraphNode, agent: str, event: threading.Event) -> Outcome:
            nonlocal active, maximum
            order.append(node.id)
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return Outcome("done")

        results = await schedule(
            graph,
            concurrency=3,
            capacities={"agent": cap},
            choose=lambda n, load: "agent",
            execute=execute,
            transition=lambda key, outcome: None,
        )
        assert order == ["b", "c", "a"]
        assert maximum == cap
        assert all(outcome.state == "done" for outcome in results.values())

    asyncio.run(run())


def test_fail_fast_cancels_active_attempt() -> None:
    async def run() -> None:
        graph = Graph.model_validate(
            {
                "policy": {"on_failure": "fail-fast"},
                "nodes": [
                    {"id": key, "category": "test", "goal": "test"} for key in ("a", "b", "c")
                ],
            }
        )

        async def execute(node: GraphNode, agent: str, event: threading.Event) -> Outcome:
            if node.id == "a":
                await asyncio.sleep(0.02)
                return Outcome("failed", 1)
            while not event.is_set():
                await asyncio.sleep(0.01)
            return Outcome("cancelled", 4)

        result = await schedule(
            graph,
            concurrency=2,
            capacities={"agent": 2},
            choose=lambda n, load: "agent",
            execute=execute,
            transition=lambda key, outcome: None,
        )
        assert result["a"].state == "failed"
        assert result["b"].state == "cancelled"
        assert result["c"].state == "cancelled"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("src/editor/**", "src/net/**", False),
        ("src/**", "tests/**", False),
        ("**/editor/**", "src/net/**", True),
        ("src/[ab]/**", "src/c/**", True),
        ("src", "src/x.py", True),
        ("SRC/x.py", "src/x.py", True),
        ("src/a*", "src/b*", True),
    ],
)
def test_conservative_scope_intersections(a: str, b: str, expected: bool) -> None:
    assert patterns_overlap(a, b) is expected


def test_acceptance_scope_broadens_reservation() -> None:
    left = GraphNode(
        id="a",
        category="test",
        goal="a",
        owns=["src/a/**"],
        acceptance=[ScopeCheck(diff_scope=["src/**"])],
    )
    right = GraphNode(id="b", category="test", goal="b", owns=["src/b/**"])
    assert writers_overlap(left, right)
    left.acceptance = []
    assert not writers_overlap(left, right)
    right.owns = []
    assert writers_overlap(left, right)
    right.access = "read"
    assert not writers_overlap(left, right)
