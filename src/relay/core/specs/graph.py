"""Validated task graph (design section 8.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import COST_FEATURE_PHASE, NodeId, RunId, SpecModel, VersionedSpec, require_phase
from .task import Acceptance, Commands, Routing, TaskCore, VerifyMixin


class Policy(SpecModel):
    on_failure: Literal["fail-fast", "continue-independent"] = "continue-independent"
    max_depth: int = Field(default=2, ge=0)
    max_nodes: int = Field(default=20, ge=1)
    cost_ceiling_usd: float | None = Field(default=None, ge=0)
    cost_mode: Literal["advisory", "hard"] | None = None

    @model_validator(mode="after")
    def _phase_gate(self) -> Policy:
        if self.cost_ceiling_usd is not None or self.cost_mode is not None:
            require_phase("policy.cost_ceiling_usd and policy.cost_mode", COST_FEATURE_PHASE)
        return self


class GraphNode(TaskCore):
    id: NodeId
    priority: int = 0
    routing: Routing = Routing()

    @model_validator(mode="after")
    def _access(self) -> GraphNode:
        if (
            self.id.lower() in {"base", "integration", "graph-verification"}
            or self.id.lower().startswith("resolve-")
            or self.id.endswith(".")
            or self.id.lower().endswith(".lock")
            or ".." in self.id
        ):
            raise ValueError(f"reserved node id: {self.id}")
        if self.access is None:
            self.access = (
                "read"
                if self.category in {"review", "debug", "architecture", "codebase-research"}
                else "write"
            )
        return self


class Graph(VersionedSpec, VerifyMixin):
    run_id: RunId | Literal["auto"] = "auto"
    policy: Policy = Policy()
    commands: Commands | None = None
    integration_acceptance: list[Acceptance] = []
    nodes: list[GraphNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _structure(self) -> Graph:
        ids = [n.id for n in self.nodes]
        folded = [node_id.lower() for node_id in ids]
        if len(folded) != len(set(folded)):
            dupes = sorted({i for i in ids if folded.count(i.lower()) > 1})
            raise ValueError(f"duplicate node ids: {', '.join(dupes)}")
        if len(ids) > self.policy.max_nodes:
            raise ValueError(f"{len(ids)} nodes exceed policy.max_nodes={self.policy.max_nodes}")
        known = set(ids)
        for node in self.nodes:
            if len(node.depends_on) != len(set(node.depends_on)):
                raise ValueError(f"node {node.id}: duplicate dependencies")
            for dep in node.depends_on:
                if dep not in known:
                    raise ValueError(f"node {node.id}: unknown dependency {dep}")
                if dep == node.id:
                    raise ValueError(f"node {node.id}: self dependency")
        ancestors: dict[str, set[str]] = {}
        depths: dict[str, int] = {}
        pending = {node.id: node for node in self.nodes}
        while pending:
            ready = sorted(
                key for key, node in pending.items() if all(d in depths for d in node.depends_on)
            )
            if not ready:
                raise ValueError(f"dependency cycle involving: {', '.join(sorted(pending))}")
            for key in ready:
                node = pending.pop(key)
                depths[key] = max((depths[d] + 1 for d in node.depends_on), default=0)
                if depths[key] > self.policy.max_depth:
                    raise ValueError(f"node {key} exceeds policy.max_depth={self.policy.max_depth}")
                ancestors[key] = set(node.depends_on)
                for dep in node.depends_on:
                    ancestors[key].update(ancestors[dep])
                for ref in node.context.from_:
                    if ref.node not in ancestors[key]:
                        raise ValueError(
                            f"node {key}: context.from {ref.node} is not a transitive dependency"
                        )
        return self

    def ordered_nodes(self, *, priority: bool = True) -> list[GraphNode]:
        """Stable topological order, using priority then id within each ready set."""
        pending = {node.id: node for node in self.nodes}
        done: set[str] = set()
        ordered: list[GraphNode] = []
        while pending:
            ready = [node for node in pending.values() if set(node.depends_on) <= done]
            node = min(ready, key=lambda n: (-n.priority if priority else 0, n.id))
            ordered.append(node)
            done.add(node.id)
            del pending[node.id]
        return ordered
