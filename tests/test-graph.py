"""Graph intake invariants (design section 8.1)."""

from typing import Any

import pytest
from pydantic import ValidationError

from relay.core.specs.graph import Graph


def node(name: str, **kwargs: Any) -> dict[str, Any]:
    return {"id": name, "category": "implement", "goal": "Work", **kwargs}


@pytest.mark.parametrize(
    ("nodes", "message"),
    [
        ([node("a"), node("A")], "duplicate"),
        ([node("base")], "reserved"),
        ([node("n.lock")], "reserved"),
        ([node("n..x")], "reserved"),
        ([node("n.")], "reserved"),
        ([node("a", depends_on=["missing"])], "unknown dependency"),
        ([node("a", depends_on=["a"])], "self dependency"),
        ([node("a", depends_on=["b"]), node("b", depends_on=["a"])], "cycle"),
        ([node("a"), node("b", depends_on=["a", "a"])], "duplicate dependencies"),
        ([node("a", context={"from": [{"node": "missing", "field": "summary"}]})], "transitive"),
        ([node("a", owns=[".agents/runs/**"])], "reserved"),
        ([node("a", acceptance=[{"diff_scope": [".git/**"]}])], "reserved"),
        ([node("a", category="unknown")], "category"),
    ],
)
def test_reject_invalid_graph(nodes: list[dict[str, Any]], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Graph.model_validate({"nodes": nodes})


def test_depth_and_transitive_context() -> None:
    nodes = [
        node("c", depends_on=["b"], context={"from": [{"node": "a", "field": "summary"}]}),
        node("b", depends_on=["a"]),
        node("a", category="review"),
    ]
    graph = Graph.model_validate({"nodes": nodes, "policy": {"max_depth": 2}})
    assert [n.id for n in graph.ordered_nodes()] == ["a", "b", "c"]
    assert graph.nodes[2].access == "read"
    assert graph.nodes[0].access == "write"
    with pytest.raises(ValidationError, match="max_depth"):
        Graph.model_validate({"nodes": nodes, "policy": {"max_depth": 1}})


def test_priority_and_explicit_access() -> None:
    graph = Graph.model_validate(
        {"nodes": [node("a"), node("b", priority=3, access="read", routing={"agent": "claude"})]}
    )
    assert [n.id for n in graph.ordered_nodes()] == ["b", "a"]
    assert graph.nodes[1].routing.agent == "claude"
    assert graph.nodes[1].access == "read"
