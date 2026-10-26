"""Coordinator response contract (design section 5.6, phase 2c).

A coordinator turn returns exactly one action from a closed union. The models forbid unknown
fields: unlike worker specs, nothing a coordinator adds outside the contract is preserved or
acted on. Validation here is structural only; the application service independently checks
references, graph structure, capability eligibility and limits (section 5.6), so a valid
response is never authority to execute anything.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .common import SPEC_VERSION, VersionedSpec, intersects_reserved, normalize_repo_path
from .graph import Graph
from .result import OpenQuestion

__all__ = [
    "ArtifactSelection",
    "AskUser",
    "CoordinatorAction",
    "CoordinatorResponse",
    "EvidenceRef",
    "Finding",
    "Finish",
    "ProposePlan",
    "RepoSelection",
    "RequestContext",
    "Review",
]

MAX_QUESTIONS = 8
MAX_SELECTIONS = 20
MAX_FINDINGS = 50
_TEXT_MAX = 8000
_SHA256 = r"^[0-9a-f]{64}$"


class Closed(BaseModel):
    """Base for coordinator models: unknown fields are rejected (the union is closed)."""

    model_config = ConfigDict(extra="forbid")


class LineRange(Closed):
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> LineRange:
        if self.end < self.start:
            raise ValueError("range end must not precede its start")
        return self


class RepoSelection(Closed):
    """A repository object read from the pinned base, never from the host filesystem."""

    kind: Literal["repo"] = "repo"
    path: str = Field(min_length=1, max_length=1024)
    lines: LineRange | None = None

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        path = normalize_repo_path(value)
        if any(ch in path for ch in "*?[]"):
            raise ValueError(f"{value!r} must name one path, not a glob")
        if path.endswith("/"):
            raise ValueError(f"{value!r} must name a file, not a directory")
        if intersects_reserved(path):
            raise ValueError(f"{value!r} is a reserved runtime path")
        return path


class ArtifactSelection(Closed):
    """A hash-pinned artifact of a sealed source run (section 5.6 cross-round imports)."""

    kind: Literal["artifact"] = "artifact"
    run_id: str = Field(min_length=1, max_length=64)
    node_id: str = Field(min_length=1, max_length=128)
    attempt: int = Field(ge=1)
    artifact: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=_SHA256)
    lines: LineRange | None = None

    @field_validator("artifact")
    @classmethod
    def _relative_artifact(cls, value: str) -> str:
        return normalize_repo_path(value)


ContextSelection = Annotated[RepoSelection | ArtifactSelection, Field(discriminator="kind")]


class EvidenceRef(Closed):
    node_id: str = Field(min_length=1, max_length=128)
    kind: Literal["result", "artifact", "verification", "diff"]
    ref: str = Field(min_length=1, max_length=512)


class Finding(Closed):
    severity: Literal["info", "warning", "blocker"]
    text: str = Field(min_length=1, max_length=_TEXT_MAX)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=20)


class AskUser(Closed):
    action: Literal["ask_user"] = "ask_user"
    questions: list[OpenQuestion] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def _unique_ids(self) -> AskUser:
        ids = [question.id for question in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self


class RequestContext(Closed):
    action: Literal["request_context"] = "request_context"
    reason: str = Field(min_length=1, max_length=_TEXT_MAX)
    selections: list[ContextSelection] = Field(min_length=1, max_length=MAX_SELECTIONS)


class ProposePlan(Closed):
    """A complete graph proposal; follow-up work is another proposal, never a spawn request."""

    action: Literal["propose_plan"] = "propose_plan"
    explanation: str = Field(min_length=1, max_length=_TEXT_MAX)
    change_summary: str | None = Field(default=None, max_length=_TEXT_MAX)
    graph: Graph


class Review(Closed):
    action: Literal["review"] = "review"
    summary: str = Field(min_length=1, max_length=_TEXT_MAX)
    findings: list[Finding] = Field(default=[], max_length=MAX_FINDINGS)
    remaining_questions: list[OpenQuestion] = Field(default=[], max_length=MAX_QUESTIONS)


class Finish(Closed):
    action: Literal["finish"] = "finish"
    summary: str = Field(min_length=1, max_length=_TEXT_MAX)
    delivery_recommendation: Literal["apply", "revise", "discard", "none"] = "none"


CoordinatorAction = Annotated[
    AskUser | RequestContext | ProposePlan | Review | Finish, Field(discriminator="action")
]


class CoordinatorResponse(VersionedSpec):
    """The single object a coordinator call returns (the schema root must be an object)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    spec_version: int = SPEC_VERSION
    response: CoordinatorAction
