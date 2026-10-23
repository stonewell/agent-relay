"""Foreground ``relay run`` loop for a single node (design sections 6, 8.2, 9, 10, 14).

One node runs in the foreground, with same-agent retries up to ``max_attempts``.
Every attempt is routed, started through ``start_attempt`` (so an earlier attempt's
``result.spec.yaml`` is archived under ``attempts/NN/``), and only a verified, published attempt
ends the node ``done``. Escalation, fallback and timeout retry policy arrive in phase 2a;
``needs_input`` is task 1.21. Execution has no supervisor or task graph. This module is the single
writer of a run's durable state for the lifetime of the foreground process: it takes the
interim run lock (section 6), records each routing decision, runs adapter invocations,
captures/verifies/publishes the successful output, and releases the lock on every exit path.

Exit codes (design section 14) are plain ints here -- the core never imports ``relay.cli``.
The CLI maps them onto :class:`relay.cli.exitcodes.Exit`; ``relay doctor`` does the same.

* ``0`` ok
* ``1`` execution/store/lock failure
* ``2`` invalid input or configuration (dirty tree, missing executable checks, unsupported
  permission level, or the unsupported foreground input flow)
* ``3`` no eligible agent
* ``5`` verification failed

Read nodes (access ``read``: review, debug, architecture, codebase-research; section 8.1) are
not rejected for missing executable checks, but their verification is recorded as
``not_applicable`` and their output commit equals their input commit (section 8.3).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, TextIO

from relay.adapters.base import (
    AgentAdapter,
    CommandNotFound,
    ProcessResult,
    RunContext,
    run_invocation,
    start_attempt,
)
from relay.adapters.parsing import ResultInvalid
from relay.core.capture import CaptureError, capture_changes
from relay.core.classifier import Classification, classify
from relay.core.events import EventLog, EventLogCorrupt
from relay.core.gitbase import (
    DirtyTreeError,
    EmptyRepositoryError,
    GitBaseError,
    InvalidRefError,
    NotARepositoryError,
    resolve_base,
)
from relay.core.gitexclude import ExcludeError, ensure_exclude_block
from relay.core.model_router import NO_MODEL, select_model
from relay.core.permissions import decide, format_banner, resolve_level
from relay.core.publish import PublicationError, node_ref, publish, publish_integration
from relay.core.registry import Config, snapshot
from relay.core.route_log import RouteLog
from relay.core.router import route_agent
from relay.core.runlock import RunLock, RunLockError
from relay.core.specs.common import Access, NodeStatus, PermissionLevel
from relay.core.specs.graph import Graph, GraphNode
from relay.core.specs.result import Changes, ResultSpec, Verification, VerifyDetail
from relay.core.specs.task import TaskCore, TaskSpec
from relay.core.store import RunStore, StoreError
from relay.core.verify import (
    NoExecutableChecksError,
    VerificationError,
    VerifyResult,
    executable_commands,
    verify,
)
from relay.core.worktree import WorktreeError, WorktreeManager

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID = 2
EXIT_NO_AGENT = 3
EXIT_VERIFICATION_FAILED = 5

DEFAULT_MAX_ATTEMPTS = 1
"""Attempts per node when ``budget.max_attempts`` is unset.

Section 8.2 defines this as a total per-node limit and defaults phase 1 to one attempt.
"""

READ_CATEGORIES = frozenset({"review", "debug", "architecture", "codebase-research"})

# Restrictiveness rank: lower is more restrictive (readonly < workspace < full; section 9.2).
_RESTRICTION: dict[PermissionLevel, int] = {"readonly": 0, "workspace": 1, "full": 2}


@dataclass(frozen=True)
class RunResult:
    """What a foreground run did, as plain data for the CLI to render or map to an exit code."""

    exit_code: int
    run_id: str | None = None
    node_id: str | None = None
    node_status: str | None = None
    output_commit: str | None = None
    integration_commit: str | None = None
    reasons: tuple[str, ...] = ()
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        """A ``--json``-friendly representation (design section 14)."""
        data: dict[str, Any] = {
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "node_status": self.node_status,
            "output_commit": self.output_commit,
            "integration_commit": self.integration_commit,
            "reasons": list(self.reasons),
        }
        if self.error is not None:
            data["error"] = self.error
        return data


def _most_restrictive(*levels: PermissionLevel | None) -> PermissionLevel | None:
    present: list[PermissionLevel] = [level for level in levels if level is not None]
    if not present:
        return None

    def rank(level: PermissionLevel) -> int:
        return _RESTRICTION[level]

    return min(present, key=rank)


def _access(category: str, task_access: Access | None) -> Access:
    if task_access is not None:
        return task_access
    return "read" if category in READ_CATEGORIES else "write"


def _input_key(task: TaskSpec, input_commit: str, config_hash: str) -> str:
    """A slice-A stand-in for the full input key of section 6 (no reuse in this phase)."""
    payload = {
        "goal": task.goal,
        "category": task.category,
        "language": task.language,
        "owns": list(task.owns),
        "context_files": list(task.context.files),
        "acceptance": [entry.model_dump(mode="json") for entry in task.acceptance],
        "commands": task.commands.model_dump(mode="json") if task.commands is not None else None,
        "verify": task.verify,
        "input_commit": input_commit,
        "config_hash": config_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_adapters(config: Config) -> dict[str, AgentAdapter]:
    """Claude and Codex adapters for the configured agents (design section 7.1).

    Imports are local so importing ``relay.core.run`` never drags in adapter modules, and so
    agents without an adapter here (e.g. the built-in ``gemini``, the disabled ``cursor``) are
    simply absent from the registry and reported unavailable by the probe below.
    """
    from relay.adapters.claude import ClaudeAdapter
    from relay.adapters.codex import CodexAdapter

    adapters: dict[str, AgentAdapter] = {}
    if "claude" in config.agents:
        adapters["claude"] = ClaudeAdapter(config.agents["claude"])
    if "codex" in config.agents:
        adapters["codex"] = CodexAdapter(agent=config.agents["codex"])
    return adapters


async def _probe(
    config: Config, adapters: Mapping[str, AgentAdapter]
) -> tuple[dict[str, bool], dict[str, tuple[PermissionLevel, ...]]]:
    """Probe every adapter once; configured agents without an adapter are unavailable."""
    available = {name: False for name in config.agents}
    levels: dict[str, tuple[PermissionLevel, ...]] = {name: ("full",) for name in config.agents}
    for name, adapter in adapters.items():
        if name not in config.agents:
            continue
        probe = await adapter.probe()
        available[name] = probe.available
        levels[name] = probe.levels
    return available, levels


@dataclass(frozen=True)
class _AttemptFailure:
    """A retryable attempt failure; the loop records it and decides whether to retry."""

    reason: str
    exit_code: int
    retryable: bool = True


@dataclass
class _AttemptRunner:
    """Runs one attempt of one node and owns that attempt's capture candidate (section 8.2).

    Split out of :func:`run_foreground` so the retry loop stays readable and the type checker
    can analyze each part. The runner keeps the in-node CLI session id across attempts (section
    6): a retry of the same node resumes the agent's session when the adapter reported one.
    """

    repo_root: Path
    store: RunStore
    events: EventLog
    task: TaskSpec
    node_id: str
    node_dir: Path
    worktree_manager: WorktreeManager
    worktree_path: Path
    base_commit: str
    input_key: str
    access: Access
    permission: PermissionLevel
    timeout_s: float | None
    adapter: AgentAdapter
    agent_name: str
    resume_session: str | None = None

    async def run(self, attempt: int, model: str | None) -> RunResult | _AttemptFailure:
        """Run the agent phase, then capture/verify/publish when the agent reported success."""
        deadline = time.monotonic() + self.timeout_s if self.timeout_s is not None else None
        ctx = RunContext(
            run_id=self.store.run_id,
            node_id=self.node_id,
            attempt=attempt,
            node_dir=self.node_dir,
            worktree=self.worktree_path,
            permission=self.permission,
            resume_session=self.resume_session,
        )
        # `start_attempt` archives the previous attempt's node-dir result before building this
        # attempt's invocation (section 8.2), so parsing sees only the current attempt.
        invocation = start_attempt(self.adapter, self.task, ctx)
        self.events.append(
            "node.started",
            {
                "node_id": self.node_id,
                "permissions": self.permission,
                "enforcement_profile": self.permission,
                "input_commit": self.base_commit,
                "input_key": self.input_key,
                "cmd": invocation.cmd,
            },
        )
        self.events.append(
            "attempt.phase", {"node_id": self.node_id, "attempt": attempt, "phase": "agent"}
        )
        proc = run_invocation(invocation, timeout_s=_remaining_timeout(deadline))
        if proc.timed_out:
            return _AttemptFailure(
                f"attempt {attempt}: the agent timed out", EXIT_FAILURE, retryable=False
            )
        try:
            result = await self.adapter.parse_result(proc, self.task, ctx)
        except ResultInvalid as exc:
            return _AttemptFailure(f"attempt {attempt}: result rejected: {exc}", EXIT_FAILURE)

        # A retry of the same node resumes the agent's session when the adapter reported one
        # (section 6); a fresh session is used when extraction failed.
        if result.session is not None:
            self.resume_session = result.session.cli_session_id

        if result.status == "needs_input":
            _set_node(self.store, self.node_id, "blocked", blocked_reason="needs_input")
            self.store.update_run(lambda record: setattr(record, "state", "blocked"))
            self.events.append("run.finished", {"state": "blocked"})
            return RunResult(
                exit_code=EXIT_INVALID,
                run_id=self.store.run_id,
                node_id=self.node_id,
                node_status="needs_input",
                reasons=("needs_input is not supported in this build (task 1.21)",),
                error="the agent asked for input; answering questions is not implemented yet",
            )

        if result.status in ("blocked", "cancelled"):
            # A terminal non-done status is not retried in this slice.
            _set_node(self.store, self.node_id, "failed")
            self.store.update_run(lambda record: setattr(record, "state", "failed"))
            self.events.append("node.finished", {"node_id": self.node_id, "status": "failed"})
            self.events.append("run.finished", {"state": "failed"})
            return RunResult(
                exit_code=EXIT_FAILURE,
                run_id=self.store.run_id,
                node_id=self.node_id,
                node_status=result.status,
                error=f"agent reported status {result.status!r}",
            )

        if result.status != "done":
            return _AttemptFailure(
                f"attempt {attempt}: agent reported status {result.status!r}", EXIT_FAILURE
            )
        if proc.exit_code not in (0, None):
            return _AttemptFailure(
                f"attempt {attempt}: the agent exited with code {proc.exit_code}", EXIT_FAILURE
            )

        return self._capture_verify_publish(attempt, result, proc, model, deadline=deadline)

    def _capture_verify_publish(
        self,
        attempt: int,
        result: ResultSpec,
        proc: ProcessResult,
        model: str | None,
        *,
        deadline: float | None,
    ) -> RunResult | _AttemptFailure:
        node_id = self.node_id
        candidate_path: Path | None = None
        try:
            self.events.append(
                "attempt.phase", {"node_id": node_id, "attempt": attempt, "phase": "capture"}
            )
            candidate_dir = self.store.path(f"{node_id}/candidate")
            capture = capture_changes(
                self.worktree_path, self.base_commit, candidate_dir=candidate_dir
            )
            candidate_path = capture.candidate_path

            self.events.append(
                "attempt.phase", {"node_id": node_id, "attempt": attempt, "phase": "verify"}
            )
            verification: VerifyResult | None = None
            if self.access != "read":
                try:
                    verification = verify(
                        capture, self.task, overall_timeout_s=_remaining_timeout(deadline)
                    )
                except NoExecutableChecksError as exc:
                    _set_node(self.store, node_id, "failed")
                    self.store.update_run(lambda record: setattr(record, "state", "failed"))
                    self.events.append("run.finished", {"state": "failed"})
                    return RunResult(
                        exit_code=EXIT_INVALID,
                        run_id=self.store.run_id,
                        node_id=node_id,
                        node_status="failed",
                        error=str(exc),
                    )
                self.events.append(
                    "verify.result",
                    {
                        "node_id": node_id,
                        "attempt": attempt,
                        "outcome": verification.outcome,
                        "mode": verification.mode,
                    },
                )
                if not verification.publication_allowed:
                    return _AttemptFailure(
                        f"attempt {attempt}: verification failed ({verification.outcome})",
                        EXIT_VERIFICATION_FAILED,
                        retryable=not verification.timed_out,
                    )

            if deadline is not None and time.monotonic() >= deadline:
                return _AttemptFailure(
                    f"attempt {attempt}: timed out before publication",
                    EXIT_FAILURE,
                    retryable=False,
                )

            self.events.append(
                "attempt.phase", {"node_id": node_id, "attempt": attempt, "phase": "publish"}
            )
            publication = publish(
                self.repo_root,
                self.store.run_id,
                node_id,
                capture,
                verification if verification is not None else _read_only_verification(),
                node_dir=self.node_dir,
                attempt=attempt,
            )
            integration = publish_integration(
                self.repo_root,
                self.store.run_id,
                base_commit=self.base_commit,
                outputs=[(node_id, publication.output_commit)],
            )
            self.events.append(
                "integration.published",
                {
                    "commit": integration.commit,
                    "branch": integration.branch,
                    "nodes": [node_id],
                },
            )

            final_status: NodeStatus = "done"
            result_spec = _finalize_result(
                result,
                task=self.task,
                node_id=node_id,
                agent=self.agent_name,
                model=model,
                status=final_status,
                input_commit=self.base_commit,
                output_commit=publication.output_commit,
                changed_files=[change.path for change in capture.changes],
                verification=verification,
                read_only=self.access == "read",
            )
            self.store.write_spec(
                f"{self.node_dir.relative_to(self.store.run_dir).as_posix()}/result.spec.yaml",
                result_spec,
            )

            _set_node(
                self.store,
                node_id,
                "done",
                attempts=attempt,
                output_commit=publication.output_commit,
            )
            self.events.append(
                "node.finished",
                {
                    "node_id": node_id,
                    "status": "done",
                    "output_commit": publication.output_commit,
                    "exit": proc.exit_code if proc.exit_code is not None else 1,
                    "duration": round(proc.duration_s, 3),
                },
            )
            self.store.update_run(lambda record: setattr(record, "state", "done"))
            self.store.update_run(partial(_set_final_commit, commit=integration.commit))
            self.events.append(
                "run.finished", {"state": "done", "integration_commit": integration.commit}
            )
            return RunResult(
                exit_code=EXIT_OK,
                run_id=self.store.run_id,
                node_id=node_id,
                node_status="done",
                output_commit=publication.output_commit,
                integration_commit=integration.commit,
            )
        finally:
            # The capture candidate is scratch: remove it whether this attempt published, failed
            # verification, or raised, so the next attempt can capture into the same path.
            if candidate_path is not None:
                self.worktree_manager.decommission_candidate(candidate_path)


async def run_foreground(
    repo_root: Path,
    task: TaskSpec,
    *,
    config: Config,
    adapters: Mapping[str, AgentAdapter] | None = None,
    run_id: str | None = None,
    cli_permissions: PermissionLevel | None = None,
    agent_permissions: Mapping[str, PermissionLevel] | None = None,
    model_tier: str | None = None,
    allow_dirty: bool = False,
    exclude: Sequence[str] = (),
    timeout_s: float | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> RunResult:
    """Run one node in the foreground and return a :class:`RunResult`.

    ``task`` is a single-node task spec (its ``run_id`` is replaced by the allocated id, its
    ``id`` names the node). ``config`` is the merged configuration; the run snapshots it and
    records ``config_hash``. ``adapters`` may inject adapters (tests); otherwise Claude and
    Codex are built from ``config``. ``run_id`` pins the id for deterministic tests.

    Expected failures are reported as exit codes, never uncaught exceptions; the interim run
    lock is released in a ``finally`` block on every path.
    """
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err

    def fail(
        code: int,
        error: str,
        *,
        run_id: str | None = None,
        reasons: tuple[str, ...] = (),
    ) -> RunResult:
        print(f"relay run: {error}", file=stderr)
        return RunResult(exit_code=code, run_id=run_id, reasons=reasons, error=error)

    category = task.category
    access = _access(category, task.access)
    node_id = task.id
    try:
        node_ref("run", node_id)
    except PublicationError as exc:
        return fail(EXIT_INVALID, str(exc))

    # Classify (deterministic; the task's explicit category wins over text signals).
    raw_classification = classify(
        task.goal,
        files=task.context.files,
        owns=task.owns,
        symbols=task.context.symbols,
        language=task.language,
        repo_language=task.repo.language,
        risk_paths=tuple(config.risk.paths),
        est_tokens=len(task.goal.split()),
    )
    classification = Classification(
        category=category,
        complexity=raw_classification.complexity,
        risk=raw_classification.risk,
        language=raw_classification.language,
        languages=raw_classification.languages,
        risk_evidence=raw_classification.risk_evidence,
    )

    # Pre-dispatch: a write node with no executable check and default verification is rejected
    # rather than reported as verified (design section 10 item 1). Read nodes are not rejected.
    try:
        commands = executable_commands(task) if task.verify == "default" else ()
    except VerificationError as exc:
        return fail(EXIT_INVALID, str(exc))
    if access == "write" and task.verify == "default" and not commands:
        return fail(
            EXIT_INVALID,
            f"node {node_id} has no executable acceptance or build/test/lint command and "
            "verify is not 'none'; refusing to report an untested change as verified "
            "(design section 10 item 1)",
        )

    # Routing happens before any run directory exists, so a "no agent" outcome leaves no state.
    registry = _default_adapters(config)
    for name, adapter in (adapters or {}).items():
        registry[name] = adapter
    available, levels = await _probe(config, registry)

    pre_resolution = resolve_level(
        task=task.permissions,
        cli=cli_permissions,
        default=config.defaults.permissions,
        planner_read_only=access == "read",
    )

    decision = route_agent(
        config,
        classification,
        pinned=task.routing.agent,
        available=available,
        levels=levels,
        permissions=pre_resolution.effective,
        est_tokens=len(task.goal.split()),
        files=len(task.context.files) + len(task.owns),
        attempt=1,
    )
    if decision.rejected or decision.agent is None:
        return fail(
            EXIT_NO_AGENT,
            f"no eligible agent for node {node_id}: {decision.reason}",
        )

    agent_name = decision.agent
    agent_config = config.agents[agent_name]
    model_decision = select_model(
        agent_config,
        classification,
        est_tokens=len(task.goal.split()),
        files=len(task.context.files) + len(task.owns),
        attempt=1,
    )
    resolved_model = model_decision.model
    if model_tier is not None:
        resolved_model = agent_config.models.get(model_tier, model_tier)

    # Re-resolve permissions with the chosen agent's config (and per-agent CLI override).
    cli_level = _most_restrictive(cli_permissions, (agent_permissions or {}).get(agent_name))
    final_resolution = resolve_level(
        task=task.permissions,
        cli=cli_level,
        agent=agent_config.permissions,
        default=config.defaults.permissions,
        planner_read_only=access == "read",
    )
    permission = decide(agent_name, levels[agent_name], final_resolution)
    if not permission.supported:
        return fail(EXIT_INVALID, permission.reason or "unsupported permission level")

    # Record the routing decision on the task before it is snapshotted.
    task.routing.agent = agent_name
    task.routing.model = resolved_model
    task.routing.effort = model_decision.effort
    task.routing.agent_rule = decision.agent_rule
    task.routing.model_rule = model_decision.model_rule

    config_text, config_hash = snapshot(config)
    effective_timeout = (
        timeout_s
        if timeout_s is not None
        else task.budget.timeout_s
        if task.budget.timeout_s is not None
        else config.defaults.timeout_s
    )

    try:
        store = RunStore.create(repo_root, config_hash=config_hash, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - store creation failures are all run failures
        return fail(EXIT_FAILURE, f"could not create run: {exc}")

    lock: RunLock | None = None
    route_log: RouteLog | None = None
    try:
        worktree_manager = WorktreeManager(
            repo_root, store.run_id, worktree_root=config.paths.worktree_root
        )
    except WorktreeError as exc:
        # Construction only fails on a bad `paths.worktree_root` (invalid configuration).
        return fail(EXIT_INVALID, str(exc), run_id=store.run_id)
    worktree = None
    max_attempts = (
        task.budget.max_attempts if task.budget.max_attempts is not None else DEFAULT_MAX_ATTEMPTS
    )
    try:
        lock = RunLock.acquire(repo_root, store.run_id)
        store.write_text("config.snapshot.json", config_text)
        task.run_id = store.run_id
        store.add_node(node_id)
        store.update_run(lambda record: setattr(record, "graph", _single_node_graph(task)))
        node_dir = store.node_dir(node_id)
        store.write_spec(f"{node_dir.relative_to(store.run_dir).as_posix()}/task.spec.yaml", task)

        # Keep the user's git view clean before pinning the base (section 6): the first run
        # idempotently adds the marked runtime-state block to `info/exclude`, so `git status`
        # and the dirty check in `resolve_base` never see `.agents/...` runtime state.
        ensure_exclude_block(repo_root)

        base = resolve_base(repo_root, store.run_id, allow_dirty=allow_dirty, exclude=exclude)
        input_key = _input_key(task, base.commit, config_hash)

        events = EventLog(store.events_path)
        route_log = RouteLog(
            events, store.routes_path, store.route_log_path, config.route_log, run_id=store.run_id
        )
        events.append(
            "run.started",
            {
                "config_hash": config_hash,
                "base_commit": base.commit,
                "allow_dirty": allow_dirty,
                "run_id": store.run_id,
                "sensitive_paths": list(base.sensitive_paths),
            },
        )
        route_log.route(
            node_id=node_id,
            attempt=1,
            event="routed",
            category=category,
            complexity=classification.complexity,
            agent_decision=decision,
            model_decision=model_decision,
            title=task.title,
            goal=task.goal,
        )
        banner = format_banner((permission,), run_id=store.run_id)
        print(banner, file=stdout)

        _set_node(store, node_id, "running", input_key=input_key)

        worktree = worktree_manager.create(node_id, base.commit)
        runner = _AttemptRunner(
            repo_root=repo_root,
            store=store,
            events=events,
            task=task,
            node_id=node_id,
            node_dir=node_dir,
            worktree_manager=worktree_manager,
            worktree_path=worktree.path,
            base_commit=base.commit,
            input_key=input_key,
            access=access,
            permission=permission.effective,
            timeout_s=effective_timeout,
            adapter=registry[agent_name],
            agent_name=agent_name,
        )

        last_reason = f"node {node_id} did not complete an attempt"
        last_exit_code = EXIT_FAILURE
        attempts_run = 0

        for attempt in range(1, max_attempts + 1):
            # Every retry goes back through the Router before it starts (section 8.2); with no
            # escalation or cross-agent fallback in this slice it stays on the same agent.
            if attempt > 1:
                retry_decision = route_agent(
                    config,
                    classification,
                    pinned=agent_name,
                    available=available,
                    levels=levels,
                    permissions=permission.effective,
                    est_tokens=len(task.goal.split()),
                    files=len(task.context.files) + len(task.owns),
                    attempt=attempt,
                )
                if retry_decision.rejected or retry_decision.agent != agent_name:
                    route_log.route(
                        node_id=node_id,
                        attempt=attempt,
                        event="rejected",
                        category=category,
                        complexity=classification.complexity,
                        agent_decision=retry_decision,
                        executed=False,
                    )
                    last_reason = (
                        f"attempt {attempt}: no eligible agent for the retry: "
                        f"{retry_decision.reason}"
                    )
                    last_exit_code = EXIT_NO_AGENT
                    break
                retry_model = select_model(
                    agent_config,
                    classification,
                    est_tokens=len(task.goal.split()),
                    files=len(task.context.files) + len(task.owns),
                    attempt=attempt,
                )
                model_decision = retry_model
                resolved_model = retry_model.model
                if model_tier is not None:
                    resolved_model = agent_config.models.get(model_tier, model_tier)
                task.routing.model = resolved_model
                task.routing.effort = retry_model.effort
                task.routing.model_rule = retry_model.model_rule
                route_log.route(
                    node_id=node_id,
                    attempt=attempt,
                    event="routed",
                    category=category,
                    complexity=classification.complexity,
                    agent_decision=retry_decision,
                    model_decision=retry_model,
                    title=task.title,
                    goal=task.goal,
                )

            attempts_run = attempt
            _set_node(store, node_id, "running", attempts=attempt)
            outcome = await runner.run(
                attempt, None if resolved_model == NO_MODEL else resolved_model
            )
            if isinstance(outcome, RunResult):
                return outcome

            # The attempt failed. Record it durably; the next iteration re-routes and starts a
            # new attempt, whose `start_attempt` archives this attempt's result file.
            last_reason = outcome.reason
            last_exit_code = outcome.exit_code
            events.append(
                "warning",
                {
                    "message": last_reason,
                    "node_id": node_id,
                    "attempt": attempt,
                    "exit": last_exit_code,
                },
            )
            if attempt >= max_attempts or not outcome.retryable:
                break

        _set_node(store, node_id, "failed", attempts=attempts_run)
        store.update_run(lambda record: setattr(record, "state", "failed"))
        events.append("node.finished", {"node_id": node_id, "status": "failed"})
        events.append("run.finished", {"state": "failed"})
        return RunResult(
            exit_code=last_exit_code,
            run_id=store.run_id,
            node_id=node_id,
            node_status="failed",
            reasons=(last_reason,),
            error=last_reason,
        )
    except RunLockError as exc:
        return fail(EXIT_FAILURE, str(exc), run_id=store.run_id)
    except (DirtyTreeError, EmptyRepositoryError, InvalidRefError, NotARepositoryError) as exc:
        return fail(EXIT_INVALID, str(exc), run_id=store.run_id)
    except GitBaseError as exc:  # GitFailure and other unexpected base-resolution failures
        return fail(EXIT_FAILURE, str(exc), run_id=store.run_id)
    except ExcludeError as exc:
        return fail(EXIT_INVALID, str(exc), run_id=store.run_id)
    except ValueError as exc:  # a bad `--exclude` glob, or an invalid result/event payload
        return fail(EXIT_INVALID, str(exc), run_id=store.run_id)
    except (
        WorktreeError,
        CaptureError,
        VerificationError,
        PublicationError,
        StoreError,
        CommandNotFound,
        EventLogCorrupt,
    ) as exc:
        return fail(EXIT_FAILURE, str(exc), run_id=store.run_id)
    except Exception as exc:  # noqa: BLE001 - a run must always end in a mapped exit code
        return fail(EXIT_FAILURE, f"unexpected failure: {exc}", run_id=store.run_id)
    finally:
        if route_log is not None:
            route_log.close()
        if lock is not None:
            lock.release()
        # Retention policy (section 6): a successful node is decommissioned; a failed node's
        # worktree is kept for debugging. The capture candidate is owned by `_AttemptRunner`,
        # which decommissions it on every path.
        if worktree is not None and result_node_ok(store, node_id):
            worktree_manager.decommission(node_id)


def _set_final_commit(record: Any, *, commit: str) -> None:
    record.final_commit = commit


def _remaining_timeout(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _single_node_graph(task: TaskSpec) -> Graph:
    node = GraphNode.model_validate(
        {"id": task.id, **{name: getattr(task, name) for name in TaskCore.model_fields}}
    )
    return Graph(
        nodes=[node],
        commands=task.commands,
        integration_acceptance=list(task.acceptance),
        verify=task.verify,
        verification_skip_reason=task.verification_skip_reason,
    )


def _set_node(
    store: RunStore,
    node_id: str,
    state: str,
    *,
    attempts: int | None = None,
    input_key: str | None = None,
    output_commit: str | None = None,
    blocked_reason: str | None = None,
) -> None:
    def change(record: Any) -> None:
        node = record.nodes[node_id]
        node.state = state
        if attempts is not None:
            node.attempts = attempts
        if input_key is not None:
            node.input_key = input_key
        if output_commit is not None:
            node.output_commit = output_commit
        if blocked_reason is not None:
            node.blocked_reason = blocked_reason

    store.update_run(change)


def _read_only_verification() -> Any:
    """A publishable, no-op verification for a read node (section 8.3: no deliverable)."""
    from relay.core.verify import VerifyResult

    return VerifyResult(outcome="passed", mode="default", checks=())


def _finalize_result(
    result: ResultSpec,
    *,
    task: TaskSpec,
    node_id: str,
    agent: str | None,
    model: str | None,
    status: NodeStatus,
    input_commit: str,
    output_commit: str,
    changed_files: Sequence[str],
    verification: Any,
    read_only: bool,
) -> ResultSpec:
    """Assemble the orchestrator-owned ``result.spec.yaml`` (design sections 5.3, 8.5).

    Operational fields (id, agent, changes, verification) are owned by the orchestrator and
    overwrite whatever the agent declared; summary/usage/session/questions pass through.
    """
    final = result.model_copy()
    final.id = node_id
    final.agent = agent
    final.model = model
    final.status = status
    final.changes = Changes(
        input_commit=input_commit,
        output_commit=output_commit,
        files=list(changed_files),
    )
    if verification is None:
        final.verification = Verification(mode="default", acceptance="not_applicable")
    else:
        final.verification = Verification(
            mode=verification.mode,
            acceptance="not_applicable" if read_only else verification.outcome,
            skip_reason=verification.skip_reason,
            details=[
                VerifyDetail(
                    run=check.name,
                    exit=check.exit_code if check.exit_code is not None else 1,
                )
                for check in verification.checks
                if check.kind == "command"
            ],
        )
    return final


def result_node_ok(store: RunStore, node_id: str) -> bool:
    """True when the node finished ``done`` (its worktree may be decommissioned)."""
    try:
        record = store.read_run()
    except Exception:  # noqa: BLE001 - the record is best-effort during cleanup
        return False
    return record.nodes.get(node_id) is not None and record.nodes[node_id].state == "done"
