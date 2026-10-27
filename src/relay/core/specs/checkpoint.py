"""Checkpoint manifests and retained bases (design 5.6, 6, 8.7; task 2.25).

A checkpoint seals the verified, dependency-closed output of a settled round under a retained
session ref. Its manifest is immutable evidence: every contributing source ref and commit,
result and artifact hashes, verification outcomes, what was excluded and why, and, when
contributions could not be combined, the conflicting paths. Failed or cancelled worker edits are
never contributions; they remain diagnostic worktrees only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import SpecModel, VersionedSpec

BaseKind = Literal["head", "checkpoint", "integration"]


class RetainedBase(SpecModel):
    """An explicit, pinned starting point for a round (never a moving branch)."""

    kind: BaseKind = "head"
    commit: str | None = None
    """Full commit id; required except for `head`, which resolves the current HEAD."""
    checkpoint_id: str | None = None
    source_run: str | None = None
    lineage: list[str] = []
    """Checkpoint ids this base descends from, oldest first."""


class Contribution(SpecModel):
    node_id: str
    attempt: int = Field(ge=1)
    output_commit: str
    source_ref: str
    result_sha256: str
    artifacts: dict[str, str] = {}
    """Relative artifact path to sha256."""
    verification: Literal["passed", "skipped", "not_applicable"]
    skip_reason: str | None = None


class Exclusion(SpecModel):
    node_id: str
    state: str
    reason: str


class ConflictRecord(SpecModel):
    sources: list[str]
    """Node ids whose outputs could not be combined."""
    paths: list[str]


class CheckpointManifest(VersionedSpec):
    checkpoint_id: str
    session_id: str
    created_at: str
    source_run: str
    reason: Literal["revise", "finished", "cancelled", "manual"]
    base_commit: str
    state: Literal["sealed", "conflicted", "empty"]
    contributions: list[Contribution] = []
    excluded: list[Exclusion] = []
    combined_commit: str | None = None
    combined_ref: str | None = None
    parents: list[str] = []
    """The contributing output commits the combined commit descends from."""
    conflicts: list[ConflictRecord] = []
    lineage: list[str] = []


class IntegrationEvidence(SpecModel):
    """Append-only evidence about a checkpoint's required integration checks."""

    checkpoint_id: str
    recorded_at: str
    status: Literal["passed", "failed", "skipped"]
    detail: str = ""
    operator_skip: bool = False
