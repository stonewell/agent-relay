"""Shared cumulative-session delivery service (design sections 8.3, 10, 14.1; task 2.27).

Both `relay apply <run>` and the TUI deliver through this module, so they prepare, verify and
install the same tree. It builds on :mod:`relay.core.apply` (the prepared merge, the dirty-edit
guard and the compare-and-swap install) and adds what a multi-round session needs:

* A :class:`DeliveryPolicy` folded from the *approved* graphs of the session's rounds. It only
  accumulates: the write scope is the union of the rounds' scopes, every executable check ever
  mandated stays required, and every integration scope restriction keeps applying. A later,
  narrower round or a later ``verify: none`` cannot remove anything. Nothing here can remove a
  requirement; that needs an explicit operator policy change, which is not implemented yet.
* Revalidation of every carried contribution: each checkpoint or unapplied integration in the
  delivered round's base chain must still match its recorded identity, hashes, lineage and
  ancestry, and a checkpoint must be deliverable (integration checks passed or an operator skip
  with a reason). Any failure blocks delivery (fail closed).
* A prepared-operation digest. Confirmation binds to it, so a changed target ref, session
  revision, policy, checkpoint evidence, ancestry or prepared tree invalidates the confirmation.

A run with no session link keeps the headless behaviour exactly: its own graph's integration
acceptance is verified on the prepared tree, and nothing about sessions is consulted.

Nothing here takes session ownership; the caller (the TUI as owner, or `session_guard` in the CLI)
holds it for the duration, which also keeps the session record stable between preparation and
installation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from . import publish as publication
from .apply import (
    ApplyError,
    ApplyPlan,
    ApplyResult,
    changed_paths,
    commit_ref,
    detached_checkout,
    install_worktree,
    is_ancestor,
    prepare_apply,
    resolve_integration_commit,
)
from .capture import CaptureResult, Change, ChangeCounts
from .checkpoints import (
    integration_status,
    is_deliverable,
    load_checkpoint,
    round_base,
    validate_checkpoint,
)
from .gitbase import read_ref
from .sessions import SessionError, SessionOwner, SessionStore, digest_of, linked_session
from .specs.checkpoint import CheckpointManifest, RetainedBase
from .specs.common import SpecModel
from .specs.graph import Graph
from .specs.task import Acceptance, Commands, RunCheck, ScopeCheck, TaskCore
from .store import RunStore, StoreError
from .verify import (
    VerificationError,
    VerifyResult,
    effective_scope,
    executable_commands,
    scope_violations,
    verify,
)

__all__ = [
    "Carried",
    "ConfirmationInvalid",
    "DeliveryBlocked",
    "DeliveryError",
    "DeliveryOutcome",
    "DeliveryPolicy",
    "DeliveryRequest",
    "PolicySkip",
    "PreparedDelivery",
    "Verdict",
    "accumulate_policy",
    "current_policy",
    "derive_policy",
    "describe_delivery",
    "install_delivery",
    "prepare_checkpoint_delivery",
    "prepare_run_delivery",
    "refresh_delivery",
    "sync_delivery_policy",
    "verify_delivery",
]

VerificationOutcome = Literal["passed", "failed", "skipped", "not_run"]
_DEFAULT_COMMAND_KEYS = ("build", "test", "lint")
_MAX_CHAIN = 64  # a base chain longer than this is a loop or a corrupt record


class DeliveryError(ApplyError):
    """A session delivery cannot be prepared or installed."""


class DeliveryBlocked(DeliveryError):
    """A carried contribution or the session policy failed revalidation; nothing changed."""

    def __init__(self, problems: list[str]) -> None:
        shown = "; ".join(problems[:6])
        more = f" (and {len(problems) - 6} more)" if len(problems) > 6 else ""
        super().__init__(f"delivery is blocked: {shown}{more}")
        self.problems = list(problems)


class ConfirmationInvalid(DeliveryError):
    """The confirmation does not match the operation as it is now; nothing was changed."""


# --- policy -----------------------------------------------------------------------------------


class PolicySkip(SpecModel):
    """A round that explicitly opted out of command checks (`verify: none` with a reason)."""

    run_id: str
    reason: str


class DeliveryPolicy(SpecModel):
    """Cumulative delivery policy of a session, folded from its approved rounds (section 10).

    ``write_scope`` is the union of the approved write nodes' effective scopes;
    ``write_unrestricted`` is set once any approved write node had no scope (the whole
    repository). Each entry of ``scope_checks`` is one round's integration scope restriction and
    every entry must hold. ``run_checks`` are all executable integration checks ever mandated.
    """

    baseline_commit: str
    rounds: list[str] = []
    write_scope: list[str] = []
    write_unrestricted: bool = False
    scope_checks: list[list[str]] = []
    run_checks: list[RunCheck] = []
    skips: list[PolicySkip] = []

    @property
    def digest(self) -> str:
        return digest_of(self.model_dump(mode="json"))


def _no_step(_name: str) -> None:
    return None


def _check_key(check: RunCheck) -> str:
    return json.dumps(check.model_dump(mode="json"), sort_keys=True)


def _round_run_checks(graph: Graph) -> list[RunCheck]:
    """The executable integration checks one graph requires (design section 10 item 1)."""
    explicit = [entry for entry in graph.integration_acceptance if isinstance(entry, RunCheck)]
    if explicit:
        return explicit
    commands: Commands | None = graph.commands
    if commands is None:
        return []
    found: list[RunCheck] = []
    for key in _DEFAULT_COMMAND_KEYS:
        value = getattr(commands, key)
        if value:
            found.append(RunCheck(run=value))
    return found


def accumulate_policy(
    prior: DeliveryPolicy | None, *, run_id: str, graph: Graph, baseline: str
) -> DeliveryPolicy:
    """Fold one approved round into the policy. The result is always a superset of ``prior``.

    The baseline is the first round's and never changes. Scopes and checks are only added; a
    round that is narrower, or that opts out of command checks, adds nothing and removes
    nothing (an opt-out is only recorded as a skip).
    """
    scope: set[str] = set(prior.write_scope) if prior else set()
    unrestricted = prior.write_unrestricted if prior else False
    for node in graph.nodes:
        if node.access != "write":
            continue
        node_scope = effective_scope(node)
        if node_scope is None:
            unrestricted = True
        else:
            scope.update(node_scope)

    scope_checks = [list(entry) for entry in prior.scope_checks] if prior else []
    restriction = sorted(
        {
            pattern
            for entry in graph.integration_acceptance
            if isinstance(entry, ScopeCheck)
            for pattern in entry.diff_scope
        }
    )
    if restriction and restriction not in scope_checks:
        scope_checks.append(restriction)

    checks = list(prior.run_checks) if prior else []
    skips = list(prior.skips) if prior else []
    if graph.verify == "none":
        reason = graph.verification_skip_reason or "no reason recorded"
        skips.append(PolicySkip(run_id=run_id, reason=reason))
    else:
        known = {_check_key(check) for check in checks}
        for check in _round_run_checks(graph):
            if _check_key(check) not in known:
                known.add(_check_key(check))
                checks.append(check)

    return DeliveryPolicy(
        baseline_commit=prior.baseline_commit if prior else baseline,
        rounds=[*(prior.rounds if prior else []), run_id],
        write_scope=sorted(scope),
        write_unrestricted=unrestricted,
        scope_checks=scope_checks,
        run_checks=checks,
        skips=skips,
    )


def derive_policy(
    repo_root: Path, store: SessionStore, *, count: int | None = None
) -> DeliveryPolicy:
    """Fold the first ``count`` rounds (default: all) from their immutable approved plans."""
    rounds = store.read().rounds
    if count is not None:
        rounds = rounds[:count]
    if not rounds:
        raise DeliveryBlocked(["the session has no approved round"])
    policy: DeliveryPolicy | None = None
    for item in rounds:
        try:
            graph = Graph.model_validate(store.plan(item.plan_revision)["graph"])
            baseline = round_base(RunStore.open(repo_root, item.run_id)) if policy is None else ""
        except (SessionError, StoreError, OSError, ValueError, KeyError) as exc:
            raise DeliveryBlocked([f"round {item.run_id} cannot be read: {exc}"]) from exc
        policy = accumulate_policy(policy, run_id=item.run_id, graph=graph, baseline=baseline)
    assert policy is not None
    return policy


def current_policy(repo_root: Path, store: SessionStore, *, through_run: str) -> DeliveryPolicy:
    """The policy in force for a delivery of ``through_run``, checked against the session record.

    Only rounds up to and including ``through_run`` apply: later rounds cannot be part of its
    tree. A recorded policy must equal the fold of the rounds it names; a record that was
    edited (a removed check or an added scope) therefore blocks delivery.
    """
    record = store.read()
    ids = [item.run_id for item in record.rounds]
    if through_run not in ids:
        raise DeliveryBlocked([f"run {through_run} is not a round of session {record.session_id}"])
    derived = derive_policy(repo_root, store, count=ids.index(through_run) + 1)
    if record.delivery_policy is not None:
        try:
            recorded = DeliveryPolicy.model_validate(record.delivery_policy)
        except ValueError as exc:
            raise DeliveryBlocked([f"the recorded delivery policy is invalid: {exc}"]) from exc
        count = len(recorded.rounds)
        expected = (
            derive_policy(repo_root, store, count=count).model_dump(mode="json")
            if count and recorded.rounds == ids[:count]
            else None
        )
        if expected != recorded.model_dump(mode="json"):
            raise DeliveryBlocked(
                ["the recorded delivery policy disagrees with the approved rounds"]
            )
    return derived


def sync_delivery_policy(owner: SessionOwner) -> DeliveryPolicy | None:
    """Record the policy folded from every round so far; None while it cannot be derived."""
    try:
        policy = derive_policy(owner.store.repo_root, owner.store)
    except DeliveryError:
        return None
    owner.set_delivery_policy(policy.model_dump(mode="json"))
    return policy


# --- preparation ------------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryRequest:
    """What to deliver and where; enough to prepare the same operation again."""

    kind: Literal["run", "checkpoint"]
    ref: str  # run id, or checkpoint id
    session_id: str | None
    target: str | None


@dataclass(frozen=True)
class Carried:
    """One validated contribution carried into the delivered tree from an earlier round."""

    kind: Literal["checkpoint", "integration"]
    ref: str  # checkpoint id, or the integration's source run id
    commit: str
    fingerprint: str  # digest of the manifest and its latest evidence, or of the run result


@dataclass(frozen=True)
class PreparedDelivery:
    """A prepared, verified-on-demand delivery whose digest confirmation binds to.

    ``policy`` is None for a headless run, which verifies ``task`` (its own graph's
    integration policy) instead. ``cumulative_paths`` is the session's delta from its original
    baseline to the delivered commit, which the cumulative scope applies to.
    """

    request: DeliveryRequest
    plan: ApplyPlan
    session_id: str | None
    policy: DeliveryPolicy | None
    task: TaskCore | None
    carried: tuple[Carried, ...]
    cumulative_paths: tuple[str, ...]
    session_revision: int | None
    digest: str


def _digest(
    request: DeliveryRequest,
    plan: ApplyPlan,
    policy: DeliveryPolicy | None,
    task: TaskCore | None,
    carried: tuple[Carried, ...],
    cumulative: tuple[str, ...],
    revision: int | None,
) -> str:
    return digest_of(
        {
            "request": asdict(request),
            "plan": asdict(plan),
            "policy": policy.digest if policy is not None else None,
            "task": task.model_dump(mode="json") if task is not None else None,
            "carried": [asdict(item) for item in carried],
            "cumulative": list(cumulative),
            "session_revision": revision,
        }
    )


def prepare_run_delivery(
    repo_root: Path, run_id: str, *, target: str | None = None
) -> PreparedDelivery:
    """Prepare delivering a run; a session-linked run resolves its session's policy (section 10)."""
    session_id = linked_session(repo_root, run_id)
    request = DeliveryRequest("run", run_id, session_id, target)
    if session_id is None:
        return _prepare_headless(repo_root, request)
    return _prepare_session(repo_root, request)


def prepare_checkpoint_delivery(
    repo_root: Path, session_id: str, checkpoint_id: str, *, target: str | None = None
) -> PreparedDelivery:
    """Prepare delivering a sealed checkpoint's combined commit under the session policy."""
    return _prepare_session(
        repo_root, DeliveryRequest("checkpoint", checkpoint_id, session_id, target)
    )


def refresh_delivery(repo_root: Path, prepared: PreparedDelivery) -> PreparedDelivery:
    """Prepare the same request again, revalidating everything from the current state."""
    request = prepared.request
    if request.session_id is None:
        return _prepare_headless(repo_root, request)
    return _prepare_session(repo_root, request)


def integration_task(graph: Graph | None) -> TaskCore | None:
    """The integration verification policy recorded with a headless run, or None.

    ``integration_acceptance`` and ``commands`` are graph-level in the design; a run without a
    recorded graph has no authorized integration commands to run at apply time.
    """
    if graph is None:
        return None
    acceptance: list[Acceptance] = list(graph.integration_acceptance)
    commands: Commands | None = graph.commands
    if not acceptance and commands is None and graph.verify != "none":
        return None
    return TaskCore(
        category="implement",
        goal="integration acceptance",
        acceptance=acceptance,
        commands=commands,
        verify=graph.verify,
        verification_skip_reason=graph.verification_skip_reason,
    )


def _prepare_headless(repo_root: Path, request: DeliveryRequest) -> PreparedDelivery:
    plan = prepare_apply(repo_root, request.ref, target=request.target)
    try:
        record = RunStore.open(repo_root, request.ref).read_run()
    except (StoreError, OSError, ValueError) as exc:
        raise ApplyError(f"cannot read run {request.ref}: {exc}") from exc
    task = integration_task(record.graph)
    digest = _digest(request, plan, None, task, (), (), None)
    return PreparedDelivery(request, plan, None, None, task, (), (), None, digest)


def _fingerprint_checkpoint(store: SessionStore, manifest: CheckpointManifest) -> str:
    evidence = integration_status(store, manifest.checkpoint_id)
    return digest_of(
        {
            "manifest": manifest.model_dump(mode="json"),
            "evidence": evidence.model_dump(mode="json") if evidence is not None else None,
        }
    )


def _round_output_problems(repo: Path, run: RunStore, final: str) -> list[str]:
    """Every completed node's published output must still exist and sit under the integration."""
    problems: list[str] = []
    for node_id, node in sorted(run.read_run().nodes.items()):
        if node.state != "done" or node.output_commit is None:
            continue
        if read_ref(repo, publication.node_ref(run.run_id, node_id)) != node.output_commit:
            problems.append(f"run {run.run_id} node {node_id}: output ref no longer matches")
        elif not is_ancestor(repo, node.output_commit, final):
            problems.append(f"run {run.run_id} node {node_id}: output is not in the integration")
    return problems


def _carried_checkpoint(
    repo: Path,
    store: SessionStore,
    session_id: str,
    base: RetainedBase,
    source: str,
    problems: list[str],
) -> tuple[Carried | None, RetainedBase | None]:
    cid = base.checkpoint_id
    if not cid:
        problems.append("a checkpoint base names no checkpoint")
        return None, None
    try:
        manifest = load_checkpoint(store, cid)
    except (SessionError, OSError, ValueError) as exc:
        problems.append(f"carried checkpoint {cid} cannot be read: {exc}")
        return None, None
    label = f"carried checkpoint {cid}"
    if manifest.session_id != session_id:
        problems.append(f"{label} belongs to another session")
    if manifest.state != "sealed" or manifest.combined_commit is None:
        problems.append(f"{label} is {manifest.state} and has no combined commit")
        return None, None
    if base.commit != manifest.combined_commit:
        problems.append(f"{label}: the round's pinned base is not the checkpoint's combined commit")
    if not is_deliverable(store, cid):
        problems.append(f"{label} is not deliverable: record passing integration checks or a skip")
    problems.extend(f"{label}: {item}" for item in validate_checkpoint(repo, store, manifest))
    if not is_ancestor(repo, manifest.combined_commit, source):
        problems.append(f"{label} is not an ancestor of the delivered commit")
    next_base: RetainedBase | None = None
    try:
        next_base = RunStore.open(repo, manifest.source_run).read_run().base
    except (StoreError, OSError, ValueError) as exc:
        problems.append(f"{label}: source run {manifest.source_run} unavailable ({exc})")
    expected = (
        [*next_base.lineage, str(next_base.checkpoint_id)]
        if next_base is not None and next_base.kind == "checkpoint"
        else []
    )
    if manifest.lineage != expected:
        problems.append(f"{label}: recorded lineage no longer matches its source run")
    return Carried(
        "checkpoint", cid, manifest.combined_commit, _fingerprint_checkpoint(store, manifest)
    ), next_base


def _carried_integration(
    repo: Path,
    session_id: str,
    rounds: dict[str, str],
    base: RetainedBase,
    source: str,
    problems: list[str],
) -> tuple[Carried | None, RetainedBase | None]:
    run_id = base.source_run
    if not run_id:
        problems.append("an integration base names no source run")
        return None, None
    label = f"carried integration of run {run_id}"
    if run_id not in rounds:
        problems.append(f"{label} is not a round of this session")
        return None, None
    try:
        store = RunStore.open(repo, run_id)
        record = store.read_run()
    except (StoreError, OSError, ValueError) as exc:
        problems.append(f"{label} is unreadable ({exc})")
        return None, None
    final = record.final_commit
    if record.state != "done" or not final:
        problems.append(f"{label} did not finish successfully")
        return None, None
    if base.commit != final:
        problems.append(f"{label}: the round's pinned base is not its integration commit")
    if read_ref(repo, publication.integration_branch(run_id)) != final:
        problems.append(f"{label}: the integration branch moved since the run finished")
    if not is_ancestor(repo, final, source):
        problems.append(f"{label} is not an ancestor of the delivered commit")
    problems.extend(_round_output_problems(repo, store, final))
    return Carried(
        "integration", run_id, final, digest_of({"run": run_id, "commit": final})
    ), record.base


def _walk_bases(
    repo: Path, store: SessionStore, session_id: str, base: RetainedBase | None, source: str
) -> tuple[list[Carried], list[str]]:
    """Validate every checkpoint and integration the delivered tree was built on."""
    rounds = {item.run_id: item.state for item in store.read().rounds}
    carried: list[Carried] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    while base is not None and base.kind != "head":
        key = (base.kind, str(base.checkpoint_id or base.source_run))
        if key in seen or len(seen) >= _MAX_CHAIN:
            problems.append("the base chain loops or is implausibly long")
            break
        seen.add(key)
        if base.kind == "checkpoint":
            item, base = _carried_checkpoint(repo, store, session_id, base, source, problems)
        else:
            item, base = _carried_integration(repo, session_id, rounds, base, source, problems)
        if item is None:
            break
        carried.append(item)
    return carried, problems


def _prepare_session(repo_root: Path, request: DeliveryRequest) -> PreparedDelivery:
    session_id = request.session_id
    assert session_id is not None
    try:
        store = SessionStore.open(repo_root, session_id)
        record = store.read()
    except (SessionError, OSError, ValueError) as exc:
        raise DeliveryBlocked([f"session {session_id} cannot be read: {exc}"]) from exc
    problems: list[str] = []
    carried: list[Carried] = []

    if request.kind == "run":
        rounds = {item.run_id: item for item in record.rounds}
        item = rounds.get(request.ref)
        if item is None:
            raise DeliveryBlocked([f"run {request.ref} is not a round of session {session_id}"])
        if item.state != "active":
            raise DeliveryBlocked([f"round {request.ref} was superseded by a checkpoint"])
        source = resolve_integration_commit(repo_root, request.ref)
        try:
            run = RunStore.open(repo_root, request.ref)
            base = run.read_run().base
        except (StoreError, OSError, ValueError) as exc:
            raise ApplyError(f"cannot read run {request.ref}: {exc}") from exc
        problems.extend(_round_output_problems(repo_root, run, source))
        plan_run = through = request.ref
    else:
        try:
            manifest = load_checkpoint(store, request.ref)
        except (SessionError, OSError, ValueError) as exc:
            raise DeliveryBlocked([f"checkpoint {request.ref} cannot be read: {exc}"]) from exc
        if manifest.session_id != session_id:
            raise DeliveryBlocked([f"checkpoint {request.ref} belongs to another session"])
        if manifest.state != "sealed" or manifest.combined_commit is None:
            raise DeliveryBlocked(
                [f"checkpoint {request.ref} is {manifest.state}: it has no combined commit"]
            )
        item_carried, base = _carried_checkpoint(
            repo_root,
            store,
            session_id,
            RetainedBase(
                kind="checkpoint", commit=manifest.combined_commit, checkpoint_id=request.ref
            ),
            manifest.combined_commit,
            problems,
        )
        if item_carried is not None:
            carried.append(item_carried)
        source = manifest.combined_commit
        plan_run = through = manifest.source_run

    policy = current_policy(repo_root, store, through_run=through)
    chain, chain_problems = _walk_bases(repo_root, store, session_id, base, source)
    carried.extend(chain)
    problems.extend(chain_problems)
    if not is_ancestor(repo_root, policy.baseline_commit, source):
        problems.append("the session's original baseline is not an ancestor of the delivered tree")
    if problems:
        raise DeliveryBlocked(problems)

    cumulative = changed_paths(repo_root, policy.baseline_commit, source)
    plan = prepare_apply(
        repo_root,
        plan_run,
        target=request.target,
        source_commit=source,
        baseline_commit=policy.baseline_commit,
    )
    digest = _digest(request, plan, policy, None, tuple(carried), cumulative, record.revision)
    return PreparedDelivery(
        request, plan, session_id, policy, None, tuple(carried), cumulative, record.revision, digest
    )


# --- verification -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """The outcome of running the integration acceptance on the prepared tree."""

    outcome: VerificationOutcome
    detail: str
    checks: int = 0
    result: VerifyResult | None = None


def verify_delivery(
    repo_root: Path,
    prepared: PreparedDelivery,
    *,
    on_step: Callable[[str], None] | None = None,
) -> Verdict:
    """Verify the exact prepared tree (design section 10 item 4); earlier evidence is not reused.

    A headless run verifies its own graph's integration policy. A session delivery checks the
    cumulative policy: the whole baseline-to-delivered delta against the approved write scope
    and every integration scope restriction, then every executable check ever mandated on the
    prepared tree. ``on_step`` receives progress step names for event logs.
    """
    step = on_step or _no_step
    try:
        if prepared.policy is None:
            return _verify_headless(repo_root, prepared, step)
        return _verify_session(repo_root, prepared, prepared.policy, step)
    except VerificationError as exc:
        return Verdict("failed", str(exc))


def _verify_headless(
    repo_root: Path, prepared: PreparedDelivery, step: Callable[[str], None]
) -> Verdict:
    task = prepared.task
    if task is None:
        step("verification-not-recorded")
        return Verdict("not_run", "no integration acceptance commands are recorded for this run")

    changes = tuple(Change(path=path, status="modified") for path in prepared.plan.changed_paths)
    if task.verify == "default" and not executable_commands(task):
        # Scope checks are acceptance entries of their own and apply even when no command is
        # configured (design section 10 item 3); run them before deciding there is nothing else.
        violations = scope_violations(changes, effective_scope(task))
        if violations:
            step("verification-failed")
            return Verdict(
                "failed", "scope: " + ", ".join(violations) + " outside the integration scope", 1
            )
        step("verification-not-configured")
        return Verdict("not_run", "the run records no executable integration checks")
    return _verify_on_tree(repo_root, prepared.plan, task, changes, step)


def _verify_session(
    repo_root: Path,
    prepared: PreparedDelivery,
    policy: DeliveryPolicy,
    step: Callable[[str], None],
) -> Verdict:
    delta = tuple(Change(path=path, status="modified") for path in prepared.cumulative_paths)
    violations: set[str] = set()
    if not policy.write_unrestricted:
        violations.update(scope_violations(delta, tuple(policy.write_scope)))
    for restriction in policy.scope_checks:
        violations.update(scope_violations(delta, tuple(restriction)))
    if violations:
        step("verification-failed")
        return Verdict(
            "failed",
            "scope: " + ", ".join(sorted(violations)) + " outside the cumulative approved scope",
            1,
        )
    if policy.run_checks:
        task = TaskCore(
            category="implement",
            goal="cumulative integration acceptance",
            acceptance=list(policy.run_checks),
            verify="default",
        )
        changes = tuple(
            Change(path=path, status="modified") for path in prepared.plan.changed_paths
        )
        return _verify_on_tree(repo_root, prepared.plan, task, changes, step)
    if policy.skips:
        step("verification-skipped")
        reasons = "; ".join(f"{skip.run_id}: {skip.reason}" for skip in policy.skips)
        return Verdict("skipped", f"verify: none ({reasons})")
    step("verification-not-configured")
    return Verdict("not_run", "no round mandated executable integration checks")


def _verify_on_tree(
    repo_root: Path,
    plan: ApplyPlan,
    task: TaskCore,
    changes: tuple[Change, ...],
    step: Callable[[str], None],
) -> Verdict:
    with detached_checkout(repo_root, plan.prepared_commit) as worktree:
        capture = CaptureResult(
            input_commit=plan.target_tip,
            candidate_tree=plan.prepared_tree,
            candidate_commit=plan.prepared_commit,
            candidate_path=worktree,
            changes=changes,
            counts=ChangeCounts(modified=len(changes)),
        )
        result = verify(capture, task)

    step(
        "verification-skipped" if result.outcome == "skipped" else f"verification-{result.outcome}"
    )
    if result.outcome == "failed":
        failed = [check.name for check in result.checks if check.status in ("failed", "timed_out")]
        return Verdict(
            "failed", ", ".join(failed) or "verification failed", len(result.checks), result
        )
    if result.outcome == "skipped":
        return Verdict(
            "skipped",
            f"verify: none ({result.skip_reason or 'no reason recorded'})",
            len(result.checks),
            result,
        )
    return Verdict("passed", "passed", len(result.checks), result)


# --- installation -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryOutcome:
    """What :func:`install_delivery` did: the operation as re-prepared, and the ref update."""

    prepared: PreparedDelivery
    result: ApplyResult
    installed: bool


def install_delivery(
    repo_root: Path,
    prepared: PreparedDelivery,
    *,
    confirmed_digest: str,
    on_step: Callable[[str], None] | None = None,
) -> DeliveryOutcome:
    """Install a confirmed delivery (design sections 8.3 and 10); the only way it reaches git.

    ``confirmed_digest`` must be the digest the operator confirmed. Everything is prepared and
    revalidated again first: if the digest differs (target ref or tip, session revision,
    policy, checkpoint evidence or ancestry, prepared content) :class:`ConfirmationInvalid` is
    raised and nothing changes. The target ref then moves by compare-and-swap, and only after
    that are the delivered paths restored into the working tree when the target is checked out.
    Steps reported to ``on_step``: ``revalidated``, ``ref-updated``, ``installed``.
    """
    step = on_step or _no_step
    if confirmed_digest != prepared.digest:
        raise ConfirmationInvalid("the confirmation is for a different prepared operation")
    fresh = refresh_delivery(repo_root, prepared)
    if fresh.digest != confirmed_digest:
        raise ConfirmationInvalid(
            "the target, session, policy or prepared tree changed while confirming, so the "
            "previous confirmation no longer applies"
        )
    step("revalidated")
    result = commit_ref(repo_root, fresh.plan)
    step("ref-updated")
    installed = install_worktree(repo_root, fresh.plan)
    step("installed")
    return DeliveryOutcome(fresh, result, installed)


def describe_delivery(prepared: PreparedDelivery) -> dict[str, Any]:
    """A JSON-safe description of the prepared operation for UIs and logs (no secrets)."""
    plan = prepared.plan
    return {
        "digest": prepared.digest,
        "session_id": prepared.session_id,
        "target": plan.target_ref,
        "target_tip": plan.target_tip,
        "prepared_commit": plan.prepared_commit,
        "changed_paths": list(plan.changed_paths),
        "cumulative_paths": list(prepared.cumulative_paths),
        "carried": [asdict(item) for item in prepared.carried],
        "policy_digest": prepared.policy.digest if prepared.policy is not None else None,
    }
