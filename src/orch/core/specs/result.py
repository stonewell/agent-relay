"""`result.spec.yaml` (design sections 5.3 and 8.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import NodeId, NodeStatus, Sha, SpecModel, VersionedSpec


class OpenQuestion(SpecModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: Literal["free", "choice"] = "free"
    options: list[str] | None = None
    default: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def _choice_rules(self) -> OpenQuestion:
        if self.kind == "choice":
            if not self.options:
                raise ValueError(f"choice question {self.id!r} needs options")
            if self.default is not None and self.default not in self.options:
                raise ValueError(f"default of question {self.id!r} is not one of its options")
        elif self.options is not None:
            raise ValueError(f"free question {self.id!r} must not have options")
        return self


class Changes(SpecModel):
    branch: str | None = None
    input_commit: Sha | None = None
    output_commit: Sha | None = None
    commits: list[Sha] = []
    files: list[str] = []


class Artifact(SpecModel):
    path: str
    type: str | None = None


class VerifyDetail(SpecModel):
    run: str
    exit: int


class Verification(SpecModel):
    mode: Literal["default", "none"] = "default"
    acceptance: Literal["passed", "failed", "skipped", "not_applicable"]
    skip_reason: str | None = None
    details: list[VerifyDetail] = []

    @model_validator(mode="after")
    def _skip_reason(self) -> Verification:
        if self.acceptance == "skipped" and not (self.skip_reason or "").strip():
            raise ValueError("skipped verification must carry a skip_reason")
        return self


class SpawnRequest(SpecModel):
    category: str
    goal: str
    depends_on: list[str] = []


class Usage(SpecModel):
    duration_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


class Session(SpecModel):
    cli_session_id: str | None = None


class ResultSpec(VersionedSpec):
    """Worker output. The orchestrator fills `changes`, `verification`, `usage` and `status`."""

    id: NodeId
    status: NodeStatus
    agent: str | None = None
    model: str | None = None
    summary: str = ""
    changes: Changes | None = None
    artifacts: list[Artifact] = []
    decisions: list[str] = []
    verification: Verification | None = None
    question_revision: int | None = Field(default=None, ge=1)
    open_questions: list[OpenQuestion] = []
    spawn_requests: list[SpawnRequest] = []
    usage: Usage | None = None
    session: Session | None = None

    @model_validator(mode="after")
    def _needs_input_rules(self) -> ResultSpec:
        ids = [q.id for q in self.open_questions]
        if len(ids) != len(set(ids)):
            raise ValueError("open_questions ids must be unique")
        if self.status == "needs_input":
            if not self.open_questions:
                raise ValueError("status needs_input requires at least one open question")
            if self.question_revision is None:
                self.question_revision = 1
        return self
