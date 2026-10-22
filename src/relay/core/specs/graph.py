"""Task graph (design section 8.1). Structural checks only; full graph validation is task 2.1."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import COST_FEATURE_PHASE, NodeId, RunId, SpecModel, VersionedSpec, require_phase
from .task import Acceptance, Commands, TaskCore, VerifyMixin


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


class Graph(VersionedSpec, VerifyMixin):
    run_id: RunId | Literal["auto"] = "auto"
    policy: Policy = Policy()
    commands: Commands | None = None
    integration_acceptance: list[Acceptance] = []
    nodes: list[GraphNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _structure(self) -> Graph:
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"duplicate node ids: {', '.join(dupes)}")
        if len(ids) > self.policy.max_nodes:
            raise ValueError(f"{len(ids)} nodes exceed policy.max_nodes={self.policy.max_nodes}")
        return self
