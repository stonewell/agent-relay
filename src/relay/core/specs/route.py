"""Route log entry (design section 4.4). `schemas/route-entry.json` is generated from this."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import Category, Complexity, NodeId, PermissionLevel, RunId, SpecModel

RouteEvent = Literal[
    "routed", "rerouted", "fallback", "escalated", "pinned", "previewed", "rejected"
]


class Alternative(SpecModel):
    agent: str
    score: float


class PreviousRoute(SpecModel):
    agent: str | None = None
    model: str | None = None
    reason: str | None = None


class RouteEntry(SpecModel):
    ts: str
    run_id: RunId
    seq: int = Field(ge=0)
    node_id: NodeId
    attempt: int = Field(ge=1)
    event: RouteEvent
    title: str | None = None
    category: Category
    complexity: Complexity
    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    agent_rule: str
    model_rule: str | None = None
    reason: str | None = None
    alternatives: list[Alternative] = []
    from_: PreviousRoute | None = Field(default=None, alias="from")
    permissions: PermissionLevel
    executed: bool
    resumed: bool = False

    @model_validator(mode="after")
    def _event_rules(self) -> RouteEntry:
        if self.event == "rejected":
            if self.agent is not None:
                raise ValueError("a rejected entry has no agent")
            if self.executed:
                raise ValueError("a rejected entry cannot be executed")
        elif self.agent is None:
            raise ValueError(f"event {self.event!r} requires an agent")
        if self.event == "previewed" and self.executed:
            raise ValueError("previewed entries have executed: false")
        if self.event in ("fallback", "escalated", "rerouted") and self.from_ is None:
            raise ValueError(f"event {self.event!r} requires 'from'")
        return self
