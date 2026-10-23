"""`run.json` model (design section 6)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .specs.common import NodeId, RunId, Sha, SpecModel, VersionedSpec
from .specs.graph import Graph

RunState = Literal["running", "done", "failed", "blocked", "cancelled", "interrupted"]
NodeState = Literal["pending", "running", "done", "failed", "blocked", "cancelled"]


class NodeRecord(SpecModel):
    dir: str
    state: NodeState = "pending"
    blocked_reason: str | None = None
    attempts: int = Field(default=0, ge=0)
    active_duration_s: float = Field(default=0.0, ge=0)
    input_key: str | None = None
    output_commit: Sha | None = None


class RunRecord(VersionedSpec):
    run_id: RunId
    created_at: str
    state: RunState = "running"
    owner_generation: int = Field(default=0, ge=0)
    config_hash: str | None = None
    graph: Graph | None = None
    nodes: dict[NodeId, NodeRecord] = {}
    final_commit: Sha | None = None
    budget_ledger: dict[str, Any] = {}
