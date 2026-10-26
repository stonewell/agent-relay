"""Deterministic starter pipelines; no model call (design section 17)."""

from typing import cast

from .specs import Graph, GraphNode
from .specs.common import Category
from .specs.task import Context, RunCheck

TEMPLATES = ("bugfix", "feature", "refactor", "upgrade")


def plan(request: str, *, template: str = "feature", checks: tuple[str, ...] = ()) -> Graph:
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}")
    category = {
        "bugfix": "bugfix",
        "feature": "implement",
        "refactor": "refactor",
        "upgrade": "migrate",
    }[template]
    return Graph.model_validate(
        {
            "policy": {"max_depth": 3},
            "integration_acceptance": [RunCheck(run=check) for check in checks],
            "nodes": [
                GraphNode(
                    id="research",
                    category="codebase-research",
                    goal=f"Inspect the code and propose a concrete plan: {request}",
                ),
                GraphNode(
                    id="implement",
                    category=cast(Category, category),
                    goal=request,
                    depends_on=["research"],
                    context=Context.model_validate(
                        {"from": [{"node": "research", "field": "summary"}]}
                    ),
                    acceptance=[RunCheck(run=check) for check in checks],
                ),
                GraphNode(
                    id="tests",
                    category="test",
                    goal=f"Add regression coverage for: {request}",
                    depends_on=["implement"],
                    context=Context.model_validate(
                        {"from": [{"node": "implement", "field": "summary"}]}
                    ),
                    acceptance=[RunCheck(run=check) for check in checks],
                ),
                GraphNode(
                    id="review",
                    category="review",
                    goal=f"Review the implementation and tests: {request}",
                    depends_on=["tests"],
                ),
            ],
        }
    )
