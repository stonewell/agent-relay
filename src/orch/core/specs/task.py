"""`task.spec.yaml` (design section 5.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from .common import (
    COST_FEATURE_PHASE,
    Access,
    Category,
    NodeId,
    PermissionLevel,
    RunId,
    ScopePath,
    Sha,
    SpecModel,
    VersionedSpec,
    normalize_repo_path,
    require_phase,
)


class RunCheck(SpecModel):
    """Executable acceptance check, run by the Verifier."""

    model_config = ConfigDict(extra="forbid")

    run: str = Field(min_length=1)
    expect_exit: int = 0
    expect_output: str | None = None
    timeout_s: int | None = Field(default=None, gt=0)


class ScopeCheck(SpecModel):
    """Scope check only; never counts as an executable check (section 10)."""

    model_config = ConfigDict(extra="forbid")

    diff_scope: list[ScopePath] = Field(min_length=1)


Acceptance = RunCheck | ScopeCheck


class RepoInfo(SpecModel):
    root: str = "."
    base_branch: str | None = None
    input_commit: Sha | None = None
    language: str | None = None


class ContextRef(SpecModel):
    """Reference to an upstream result field or artifact (`context.from`)."""

    node: str
    field: str | None = None
    artifact: str | None = None

    @model_validator(mode="after")
    def _one_target(self) -> ContextRef:
        if (self.field is None) == (self.artifact is None):
            raise ValueError("exactly one of 'field' or 'artifact' is required")
        return self


class Context(SpecModel):
    files: list[str] = []
    symbols: list[str] = []
    from_: list[ContextRef] = Field(default=[], alias="from")

    @model_validator(mode="after")
    def _safe_files(self) -> Context:
        self.files = [normalize_repo_path(f) for f in self.files]
        return self


class Commands(SpecModel):
    build: str | None = None
    test: str | None = None
    lint: str | None = None


class Routing(SpecModel):
    """Orchestrator-owned; ids match the route log (section 4.4)."""

    agent: str | None = None
    model: str | None = None
    effort: str | None = None
    agent_rule: str | None = None
    model_rule: str | None = None


class Budget(SpecModel):
    timeout_s: int | None = Field(default=None, gt=0)
    max_duration_s: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _phase_gate(self) -> Budget:
        if self.max_cost_usd is not None:
            require_phase("budget.max_cost_usd", COST_FEATURE_PHASE)
        return self


class Output(SpecModel):
    result: str = "result.spec.yaml"
    artifacts_dir: str = "artifacts/"


class VerifyMixin(SpecModel):
    """`verify` and its explicit opt-out rule (section 10, item 2)."""

    verify: Literal["default", "none"] = "default"
    verification_skip_reason: str | None = None

    @model_validator(mode="after")
    def _skip_needs_reason(self) -> VerifyMixin:
        if self.verify == "none" and not (self.verification_skip_reason or "").strip():
            raise ValueError("verify: none requires a nonempty verification_skip_reason")
        return self


class TaskCore(VerifyMixin):
    """Fields shared by a standalone task spec and a graph node."""

    category: Category
    goal: str = Field(min_length=1)
    title: str | None = None
    language: str | None = None
    context: Context = Context()
    constraints: list[str] = []
    commands: Commands | None = None
    acceptance: list[Acceptance] = []
    depends_on: list[NodeId] = []
    owns: list[ScopePath] = []
    access: Access | None = None
    permissions: PermissionLevel | None = None
    budget: Budget = Budget()


class TaskSpec(VersionedSpec, TaskCore):
    run_id: RunId
    id: NodeId
    repo: RepoInfo = RepoInfo()
    routing: Routing = Routing()
    output: Output = Output()
