"""Immutable checkpoints and explicit retained bases (design 5.6, 6, 8.7; task 2.25).

`seal_checkpoint` turns a settled round into durable, session-owned evidence: only successful,
validated output commits and their dependency closure contribute; each carries immutable result
and artifact hashes and its verification outcome. Compatible contributions are combined with the
existing merge primitives, recording ancestry. Failed or cancelled worker edits stay diagnostic
worktrees and never become a base. If combination conflicts, the separate source commits and a
conflict manifest are kept and nothing claims a merged base exists.

A checkpoint is not deliverable until its required integration checks have passed or an operator
explicitly recorded a skip (`record_integration`); that evidence is append-only and separate so
the sealed manifest itself never changes.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from . import publish as publication
from .events import read_events
from .gitbase import read_ref
from .sessions import SessionError, SessionOwner, SessionStore
from .specs import ResultSpec, load_spec
from .specs.checkpoint import (
    CheckpointManifest,
    ConflictRecord,
    Contribution,
    Exclusion,
    IntegrationEvidence,
    RetainedBase,
)
from .store import RunStore, atomic_write, iso, read_text

__all__ = [
    "CheckpointError",
    "checkpoint_ref",
    "source_ref",
    "integration_status",
    "is_deliverable",
    "load_checkpoint",
    "record_integration",
    "resolve_retained_base",
    "round_base",
    "seal_checkpoint",
    "validate_checkpoint",
]

SESSION_REFS = "refs/relay-sessions"
CheckpointState = Literal["sealed", "conflicted", "empty"]
_ZERO = "0" * 40


class CheckpointError(SessionError):
    """A checkpoint cannot be sealed, validated or used as a base."""


def checkpoint_ref(session_id: str, checkpoint_id: str) -> str:
    return f"{SESSION_REFS}/{session_id}/checkpoints/{checkpoint_id}"


def source_ref(session_id: str, checkpoint_id: str, node_id: str) -> str:
    """Retains each contributing output: a ref cannot also be a directory of refs."""
    return f"{SESSION_REFS}/{session_id}/checkpoint-sources/{checkpoint_id}/{node_id}"


def _checkpoint_dir(store: SessionStore) -> Path:
    return store.dir / "checkpoints"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_ref(repo: Path, ref: str, commit: str) -> None:
    """Create-only session ref (compare-and-swap); an identical existing ref is idempotent."""
    existing = read_ref(repo, ref)
    if existing == commit:
        return
    zero = _ZERO if len(commit) == 40 else "0" * len(commit)
    if existing is not None or not publication._compare_and_swap(repo, ref, commit, zero):  # pyright: ignore[reportPrivateUsage]
        raise CheckpointError(f"immutable checkpoint ref already exists or raced: {ref}")


def round_base(store: RunStore) -> str:
    """The commit a round started from (its retained base, else the pinned HEAD)."""
    record = store.read_run()
    if record.base is not None and record.base.commit:
        return record.base.commit
    for event in read_events(store.events_path):
        if event.type == "run.started":
            return str(event.data["base_commit"])
    raise CheckpointError(f"run {store.run_id} has no recorded base commit")


# --- sealing -----------------------------------------------------------------------------------


def seal_checkpoint(
    owner: SessionOwner,
    store: RunStore,
    *,
    reason: Literal["revise", "finished", "cancelled", "manual"] = "revise",
    verify: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> CheckpointManifest:
    """Seal the verified, dependency-closed output of a settled round (section 8.7).

    The run must not be executing. `verify`, when given, runs the round's integration checks on
    the combined commit and its outcome is recorded as evidence; without it the checkpoint is
    sealed with integration checks *not run* and is therefore not deliverable yet.
    """
    repo = owner.store.repo_root
    record = store.read_run()
    if record.state == "running":
        raise CheckpointError(f"run {store.run_id} is still running; settle it first")
    if record.session is None or record.session.session_id != owner.store.session_id:
        raise CheckpointError(f"run {store.run_id} does not belong to this session")
    session = owner.store.read()
    cid = f"c{len(session.checkpoints) + 1}"
    owner.event("checkpoint.started", {"checkpoint_id": cid, "run_id": store.run_id})
    base = round_base(store)
    graph = record.graph
    depends = {n.id: list(n.depends_on) for n in graph.nodes} if graph is not None else {}

    eligible: dict[str, Contribution] = {}
    excluded: list[Exclusion] = []
    for node_id in sorted(record.nodes):
        node = record.nodes[node_id]
        reason_not = _ineligible(
            repo, store, node_id, node.state, node.output_commit, node.attempts
        )
        if reason_not is not None:
            excluded.append(Exclusion(node_id=node_id, state=node.state, reason=reason_not))
            continue
        eligible[node_id] = _contribution(
            repo, store, node_id, node.attempts, str(node.output_commit)
        )
    # Dependency closure: a contribution needs every dependency's contribution.
    changed = True
    while changed:
        changed = False
        for node_id in sorted(eligible):
            missing = [d for d in depends.get(node_id, []) if d not in eligible]
            if missing:
                del eligible[node_id]
                excluded.append(
                    Exclusion(
                        node_id=node_id,
                        state="done",
                        reason=f"dependency {', '.join(missing)} is not part of the checkpoint",
                    )
                )
                changed = True
    contributions = [eligible[key] for key in sorted(eligible)]

    state, combined, parents, conflicts = _combine(repo, base, contributions)
    manifest = CheckpointManifest(
        checkpoint_id=cid,
        session_id=owner.store.session_id,
        created_at=iso(now or datetime.now(UTC)),
        source_run=store.run_id,
        reason=reason,
        base_commit=base,
        state=state,
        contributions=contributions,
        excluded=sorted(excluded, key=lambda e: e.node_id),
        combined_commit=combined,
        parents=parents,
        conflicts=conflicts,
        lineage=_lineage(record.base),
    )
    # Retained refs first (create-only), manifest last: a crash leaves only idempotent refs.
    for item in contributions:
        _write_ref(
            repo,
            source_ref(owner.store.session_id, cid, item.node_id),
            item.output_commit,
        )
    if combined is not None:
        ref = checkpoint_ref(owner.store.session_id, cid)
        _write_ref(repo, ref, combined)
        manifest.combined_ref = ref
    directory = _checkpoint_dir(owner.store)
    directory.mkdir(exist_ok=True)
    target = directory / f"{cid}.json"
    if target.exists():
        raise CheckpointError(f"checkpoint {cid} already exists")
    atomic_write(target, manifest.model_dump_json(indent=2) + "\n")
    owner.register_checkpoint(cid)
    owner.event(
        "checkpoint.conflicted" if state == "conflicted" else "checkpoint.sealed",
        {"checkpoint_id": cid, "run_id": store.run_id, "state": state},
    )
    if combined is not None and verify is not None:
        try:
            verify(combined)
        except Exception as exc:  # recorded evidence, not a seal failure
            record_integration(owner, cid, "failed", str(exc))
        else:
            record_integration(owner, cid, "passed", "integration checks passed")
    return manifest


def _lineage(base: RetainedBase | None) -> list[str]:
    if base is None or base.kind != "checkpoint" or not base.checkpoint_id:
        return []
    return [*base.lineage, base.checkpoint_id]


def _ineligible(
    repo: Path,
    store: RunStore,
    node_id: str,
    state: str,
    output: str | None,
    attempts: int,
) -> str | None:
    if state != "done":
        if attempts == 0:
            return f"node never ran ({state})"
        return f"node is {state}; its edits remain diagnostic only"
    if output is None or attempts < 1:
        return "no verified output commit"
    ref = publication.node_ref(store.run_id, node_id)
    if read_ref(repo, ref) != output:
        return "output ref does not match the recorded output commit"
    path = store.node_dir(node_id) / "result.spec.yaml"
    if not path.is_file():
        return "no result spec"
    result = load_spec(ResultSpec, path)
    if result.status != "done":
        return f"result status is {result.status}"
    if result.verification is None or result.verification.acceptance == "failed":
        return "no passing verification evidence"
    return None


def _contribution(
    repo: Path, store: RunStore, node_id: str, attempts: int, output: str
) -> Contribution:
    directory = store.node_dir(node_id)
    result = load_spec(ResultSpec, directory / "result.spec.yaml")
    assert result.verification is not None
    acceptance = result.verification.acceptance
    assert acceptance in {"passed", "skipped", "not_applicable"}
    artifacts: dict[str, str] = {}
    root = directory / "artifacts"
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                artifacts[path.relative_to(directory).as_posix()] = _sha256_file(path)
    return Contribution(
        node_id=node_id,
        attempt=attempts,
        output_commit=output,
        source_ref=publication.node_ref(store.run_id, node_id),
        result_sha256=_sha256_file(directory / "result.spec.yaml"),
        artifacts=artifacts,
        verification=acceptance,  # type: ignore[arg-type]
        skip_reason=result.verification.skip_reason,
    )


def _combine(
    repo: Path, base: str, contributions: list[Contribution]
) -> tuple[CheckpointState, str | None, list[str], list[ConflictRecord]]:
    """Combine compatible outputs onto `base`; on conflict keep sources and record the paths."""
    if not contributions:
        return "empty", None, [], []
    outputs = [(c.node_id, c.output_commit) for c in contributions]
    # A downstream output already contains its ancestors, so re-merging them would be wrong.
    maximal = [
        (key, commit)
        for index, (key, commit) in enumerate(outputs)
        if not any(
            index != other_index
            and (commit != other or index < other_index)
            and publication._is_ancestor(repo, commit, other)  # pyright: ignore[reportPrivateUsage]
            for other_index, (_, other) in enumerate(outputs)
        )
    ]
    current = base
    merged_from: list[str] = []
    for node_id, incoming in maximal:
        try:
            current, _ = publication._merge_into(  # pyright: ignore[reportPrivateUsage]
                repo, current, incoming, node_id, "checkpoint", None
            )
        except publication.IntegrationConflictError:
            proc = publication._run(  # pyright: ignore[reportPrivateUsage]
                repo, "merge-tree", "--write-tree", "--name-only", current, incoming
            )
            paths = publication._parse_conflict_paths(proc.stdout)  # pyright: ignore[reportPrivateUsage]
            conflict = ConflictRecord(sources=[*merged_from, node_id], paths=paths)
            return "conflicted", None, [commit for _, commit in maximal], [conflict]
        merged_from.append(node_id)
    return "sealed", current, [commit for _, commit in maximal], []


# --- reading and validating --------------------------------------------------------------------


def load_checkpoint(store: SessionStore, checkpoint_id: str) -> CheckpointManifest:
    path = _checkpoint_dir(store) / f"{checkpoint_id}.json"
    if not path.is_file():
        raise CheckpointError(f"checkpoint {checkpoint_id} not found")
    return CheckpointManifest.model_validate_json(read_text(path))


def validate_checkpoint(repo: Path, store: SessionStore, manifest: CheckpointManifest) -> list[str]:
    """Source identity, hashes, ancestry and availability; an empty list means usable as a base."""
    errors: list[str] = []
    for item in manifest.contributions:
        if read_ref(repo, item.source_ref) is None and (
            read_ref(repo, source_ref(manifest.session_id, manifest.checkpoint_id, item.node_id))
            != item.output_commit
        ):
            errors.append(f"{item.node_id}: output commit is no longer retained")
        try:
            run = RunStore.open(repo, manifest.source_run)
            directory = run.node_dir(item.node_id)
            if _sha256_file(directory / "result.spec.yaml") != item.result_sha256:
                errors.append(f"{item.node_id}: result changed since the checkpoint")
            for relative, digest in item.artifacts.items():
                path = directory / relative
                if not path.is_file() or _sha256_file(path) != digest:
                    errors.append(f"{item.node_id}: artifact {relative} changed or is missing")
        except (OSError, ValueError) as exc:
            errors.append(f"{item.node_id}: source run unavailable ({exc})")
    if manifest.state == "sealed":
        ref = manifest.combined_ref
        if manifest.combined_commit is None or ref is None:
            errors.append("sealed checkpoint has no combined commit")
        elif read_ref(repo, ref) != manifest.combined_commit:
            errors.append("combined commit ref is missing or was moved")
        else:
            for item in manifest.contributions:
                if not publication._is_ancestor(repo, item.output_commit, manifest.combined_commit):  # pyright: ignore[reportPrivateUsage]
                    errors.append(f"{item.node_id}: not an ancestor of the combined commit")
    return errors


def resolve_retained_base(
    owner: SessionOwner, plan_base: dict[str, object] | None
) -> RetainedBase | None:
    """Validate a plan's explicit base; None means the ordinary HEAD pin of a headless run."""
    if not plan_base or plan_base.get("kind", "head") == "head":
        return None
    base = RetainedBase.model_validate(plan_base)
    repo = owner.store.repo_root
    if base.kind == "checkpoint":
        if not base.checkpoint_id:
            raise CheckpointError("a checkpoint base needs a checkpoint id")
        manifest = load_checkpoint(owner.store, base.checkpoint_id)
        if manifest.state != "sealed" or manifest.combined_commit is None:
            raise CheckpointError(
                f"checkpoint {manifest.checkpoint_id} is {manifest.state}: it has no combined "
                "base; propose a bounded repair round naming its source commits instead"
            )
        problems = validate_checkpoint(repo, owner.store, manifest)
        if problems:
            raise CheckpointError("; ".join(problems))
        return RetainedBase(
            kind="checkpoint",
            commit=manifest.combined_commit,
            checkpoint_id=manifest.checkpoint_id,
            source_run=manifest.source_run,
            lineage=manifest.lineage,
        )
    if not base.source_run:
        raise CheckpointError("an integration base needs the source run")
    source = RunStore.open(repo, base.source_run)
    record = source.read_run()
    if record.state != "done" or not record.final_commit:
        raise CheckpointError(f"run {base.source_run} has no successful integration")
    if read_ref(repo, publication.integration_branch(base.source_run)) != record.final_commit:
        raise CheckpointError("the integration branch moved since the run finished")
    return RetainedBase(kind="integration", commit=record.final_commit, source_run=base.source_run)


# --- delivery evidence -------------------------------------------------------------------------


def record_integration(
    owner: SessionOwner,
    checkpoint_id: str,
    status: str,
    detail: str = "",
    *,
    operator_skip: bool = False,
) -> IntegrationEvidence:
    """Append evidence about the checkpoint's required integration checks (never edits it)."""
    if status not in {"passed", "failed", "skipped"}:
        raise CheckpointError(f"unknown integration status {status!r}")
    if status == "skipped" and not (operator_skip and detail.strip()):
        raise CheckpointError("a skip must be an explicit operator action with a reason")
    load_checkpoint(owner.store, checkpoint_id)
    evidence = IntegrationEvidence(
        checkpoint_id=checkpoint_id,
        recorded_at=iso(datetime.now(UTC)),
        status=status,  # type: ignore[arg-type]
        detail=detail,
        operator_skip=operator_skip,
    )
    path = _checkpoint_dir(owner.store) / f"{checkpoint_id}.integration.jsonl"
    with open(path, "ab") as handle:
        handle.write(evidence.model_dump_json().encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    return evidence


def integration_status(store: SessionStore, checkpoint_id: str) -> IntegrationEvidence | None:
    path = _checkpoint_dir(store) / f"{checkpoint_id}.integration.jsonl"
    if not path.is_file():
        return None
    lines = [line for line in read_text(path).splitlines() if line.strip()]
    return IntegrationEvidence.model_validate(json.loads(lines[-1])) if lines else None


def is_deliverable(store: SessionStore, checkpoint_id: str) -> bool:
    """Sealed, combined, and integration checks passed or explicitly skipped by the operator."""
    manifest = load_checkpoint(store, checkpoint_id)
    if manifest.state != "sealed":
        return False
    evidence = integration_status(store, checkpoint_id)
    return evidence is not None and evidence.status in {"passed", "skipped"}
