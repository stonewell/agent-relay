"""Foreground ``relay run`` loop for a single node (design sections 6, 8.2, 9, 10, 14).

One node runs in the foreground, with bounded retries up to ``max_attempts``.
Every attempt is routed, started through ``start_attempt`` (so an earlier attempt's
``result.spec.yaml`` is archived under ``attempts/NN/``), and only a verified, published attempt
ends the node ``done``. Retries can escalate models and fall back across unpinned agents.
Foreground input uses durable question sets and operator replies (task 1.21).
Execution has no supervisor or task graph. This module is the single
writer of a run's durable state for the lifetime of the foreground process: it takes the
interim run lock (section 6), records each routing decision, runs adapter invocations,
captures/verifies/publishes the successful output, and releases the lock on every exit path.

Exit codes (design section 14) are plain ints here -- the core never imports ``relay.cli``.
The CLI maps them onto :class:`relay.cli.exitcodes.Exit`; ``relay doctor`` does the same.

* ``0`` ok
* ``1`` execution/store/lock failure
* ``2`` invalid input or configuration (dirty tree, missing executable checks, unsupported
  permission level)
* ``3`` no eligible agent
* ``5`` verification failed
* ``6`` blocked for input or an exhausted limit

Read nodes (access ``read``: review, debug, architecture, codebase-research; section 8.1) are
not rejected for missing executable checks, but their verification is recorded as
``not_applicable`` and their output commit equals their input commit (section 8.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, TextIO

from relay.adapters.base import (
    AgentAdapter,
    CommandNotFound,
    ProcessResult,
    ProgressCapable,
    RunContext,
    run_invocation,
    start_attempt,
)
from relay.adapters.parsing import ResultInvalid
from relay.core.answers import answer_context, current_questions, read_answers, record_answers
from relay.core.capture import CaptureError, capture_changes
from relay.core.classifier import Classification, classify
from relay.core.events import EventLog, EventLogCorrupt, read_events
from relay.core.gitbase import (
    DirtyTreeError,
    EmptyRepositoryError,
    GitBaseError,
    InvalidRefError,
    NotARepositoryError,
    read_ref,
    resolve_base,
)
from relay.core.gitexclude import ExcludeError, ensure_exclude_block
from relay.core.model_router import NO_MODEL, ModelDecision, escalate, select_model
from relay.core.permissions import decide, format_banner, resolve_level
from relay.core.platform.proc import observe_processes, stream_output
from relay.core.platform.procid import process_identity
from relay.core.progress import ProgressChannel, ProgressTap
from relay.core.publish import (
    PublicationError,
    integration_branch,
    node_ref,
    publish,
    publish_integration,
)
from relay.core.recovery import foreground_interrupts, prepare_interrupted
from relay.core.registry import Agent, Config, snapshot
from relay.core.route_log import RouteLog
from relay.core.router import AgentDecision, route_agent
from relay.core.runlock import RunLock, RunLockError
from relay.core.runrecord import WorkerRecord
from relay.core.specs import load_spec
from relay.core.specs.answers import AnswerValue, Submission
from relay.core.specs.common import Access, NodeStatus, PermissionLevel
from relay.core.specs.graph import Graph, GraphNode
from relay.core.specs.result import Changes, ResultSpec, Verification, VerifyDetail
from relay.core.specs.route import PreviousRoute, RouteEntry, RouteEvent
from relay.core.specs.task import TaskCore, TaskSpec
from relay.core.store import RunStore, StoreError, iso, sweep_temp_files
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
EXIT_BLOCKED = 6

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


def _override_model(model: ModelDecision, agent: Agent, tier: str | None) -> ModelDecision:
    if tier is None:
        return model
    return replace(
        model,
        model=agent.models.get(tier, tier),
        tier=tier if tier in agent.models else None,
        model_rule="cli-override",
    )


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
    question_revision: int = 0
    graph_mode: bool = False
    progress: ProgressChannel | None = None
    _tap: ProgressTap | None = None

    def _phase(self, attempt: int, name: str) -> None:
        """Durable phase event, plus the lossy public lifecycle projection (section 11)."""
        self.events.append(
            "attempt.phase", {"node_id": self.node_id, "attempt": attempt, "phase": name}
        )
        if self._tap is not None:
            self._tap.phase(name)

    def _run_state(self, state: str) -> None:
        if not self.graph_mode:
            self.store.update_run(lambda record: setattr(record, "state", state))

    def _run_finished(self, state: str) -> None:
        if not self.graph_mode:
            self.events.append("run.finished", {"state": state})

    async def run(self, attempt: int, model: str | None) -> RunResult | _AttemptFailure:
        started = time.monotonic()

        def begin(record: Any) -> None:
            record.nodes[self.node_id].attempt_started_at = iso(datetime.now(UTC))
            record.nodes[self.node_id].attempt_timeout_s = self.timeout_s
            record.nodes[self.node_id].recovery_pending = False
            record.nodes[self.node_id].process_tracking = True

        self.store.update_run(begin)
        try:
            with observe_processes(self._observe_process):
                return await self._run(attempt, model)
        finally:
            duration = time.monotonic() - started

            def update(record: Any) -> None:
                record.nodes[self.node_id].active_duration_s += duration
                record.nodes[self.node_id].attempt_started_at = None
                record.nodes[self.node_id].attempt_timeout_s = None

            self.store.update_run(update)

    def _observe_process(self, phase: str, pid: int | None) -> None:
        def change(record: Any) -> None:
            node = record.nodes[self.node_id]
            if phase == "starting":
                node.worker_pending = True
                node.worker = None
            elif phase == "started" and pid is not None:
                identity = process_identity(pid)
                node.worker = WorkerRecord(pid=identity.pid, start=identity.start)
                node.worker_pending = False
            elif phase == "finished":
                node.worker = None
                node.worker_pending = False

        self.store.update_run(change)

    async def _run(self, attempt: int, model: str | None) -> RunResult | _AttemptFailure:
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
        effective_task = self.task.model_copy(deep=True)
        effective_task.goal += (
            "\n\nIf a decision cannot be resolved from the repository, stop and return "
            "status needs_input with typed open_questions instead of guessing."
            + answer_context(self.node_dir, self.node_id)
        )
        invocation = start_attempt(self.adapter, effective_task, ctx)
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
        if self.progress is not None:
            parser = (
                self.adapter.progress_parser()
                if isinstance(self.adapter, ProgressCapable)
                else None
            )
            self._tap = ProgressTap(
                self.progress,
                parser,
                run_id=self.store.run_id,
                node_id=self.node_id,
                attempt=attempt,
            )
        self._phase(attempt, "agent")
        # Streaming only changes what is *shown*: the whole output is still captured (bounded)
        # and parsed for the final result exactly as before.
        with (
            stream_output(self._tap.feed)
            if self._tap is not None and self._tap.streaming
            else (contextlib.nullcontext())
        ):
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
        if self.graph_mode and result.spawn_requests:
            return _AttemptFailure("spawn_requests is available from phase 4", EXIT_INVALID, False)

        if result.status == "needs_input":
            self.question_revision += 1
            result.question_revision = self.question_revision
            self.store.update_run(
                lambda record: setattr(
                    record.nodes[self.node_id], "question_revision", self.question_revision
                )
            )
            result.id = self.node_id
            result.agent = self.agent_name
            result.changes = Changes(input_commit=self.base_commit)
            result.verification = None
            relative = self.node_dir.relative_to(self.store.run_dir).as_posix()
            self.store.write_spec(f"{relative}/result.spec.yaml", result)
            _set_node(self.store, self.node_id, "blocked", blocked_reason="needs_input")
            self._run_state("blocked")
            self.events.append(
                "input.requested",
                {
                    "node_id": self.node_id,
                    "question_revision": self.question_revision,
                },
            )
            return RunResult(
                exit_code=EXIT_BLOCKED,
                run_id=self.store.run_id,
                node_id=self.node_id,
                node_status="needs_input",
                reasons=("needs_input",),
            )

        if result.status in ("blocked", "cancelled"):
            # A terminal non-done status is not retried in this slice.
            _set_node(self.store, self.node_id, "failed")
            self._run_state("failed")
            self.events.append("node.finished", {"node_id": self.node_id, "status": "failed"})
            self._run_finished("failed")
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
            self._phase(attempt, "capture")
            candidate_dir = self.store.path(f"{node_id}/candidate")
            capture = capture_changes(
                self.worktree_path, self.base_commit, candidate_dir=candidate_dir
            )
            candidate_path = capture.candidate_path
            if self.graph_mode and self.access == "read" and not capture.empty:
                return _AttemptFailure(
                    "readonly node changed its input snapshot", EXIT_VERIFICATION_FAILED, False
                )

            self._phase(attempt, "verify")
            verification: VerifyResult | None = None
            if self.access != "read":
                try:
                    verification = verify(
                        capture, self.task, overall_timeout_s=_remaining_timeout(deadline)
                    )
                except NoExecutableChecksError as exc:
                    _set_node(self.store, node_id, "failed")
                    self._run_state("failed")
                    self._run_finished("failed")
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

            self._phase(attempt, "publish")
            publication = publish(
                self.repo_root,
                self.store.run_id,
                node_id,
                capture,
                verification if verification is not None else _read_only_verification(),
                node_dir=self.node_dir,
                attempt=attempt,
            )
            integration = None
            if not self.graph_mode:
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
            if integration is not None:
                # `done` is published only after `run.finished` is durable, together with
                # `final_commit` in one record write: a reader that sees `done` (relay apply)
                # never races this writer's last event. A crash in between leaves the run
                # `running` with the node done, which resume finishes without redispatch.
                self.events.append(
                    "run.finished", {"state": "done", "integration_commit": integration.commit}
                )
                self.store.update_run(partial(_set_done, commit=integration.commit))
            return RunResult(
                exit_code=EXIT_OK,
                run_id=self.store.run_id,
                node_id=node_id,
                node_status="done",
                output_commit=publication.output_commit,
                integration_commit=integration.commit if integration is not None else None,
            )
        finally:
            # The capture candidate is scratch: remove it whether this attempt published, failed
            # verification, or raised, so the next attempt can capture into the same path.
            if candidate_path is not None:
                self.worktree_manager.decommission_candidate(candidate_path)


@foreground_interrupts
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
    non_interactive: bool = False,
    resume_run: str | None = None,
    retry_failed: bool = False,
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
    if agent_permissions:
        # Preserve CLI ceilings in the effective snapshot for separate retries.
        config = config.model_copy(deep=True)
        for name, level in agent_permissions.items():
            if name in config.agents:
                config.agents[name].permissions = _most_restrictive(
                    config.agents[name].permissions, level
                )

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
    requested_task_permissions = task.permissions
    pinned_agent = task.routing.agent
    routing_attempt = 1
    if resume_run is not None:
        saved_record = RunStore(repo_root, resume_run).read_run()
        saved_graph = saved_record.graph
        if saved_graph is not None:
            pinned_agent = saved_graph.nodes[0].routing.agent
            if model_tier is None and saved_graph.nodes[0].routing.model_rule == "cli-override":
                model_tier = saved_graph.nodes[0].routing.model
        if model_tier is None and task.routing.model_rule == "cli-override":
            # Legacy checkpoints stored only the effective model, not the requested tier.
            model_tier = task.routing.model
        if task.id in saved_record.nodes:
            routing_attempt = saved_record.nodes[task.id].attempts + 1
    access = _access(category, task.access)
    node_id = task.id
    try:
        node_ref(resume_run or "run", node_id)
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
        pinned=pinned_agent,
        available=available,
        levels=levels,
        permissions=pre_resolution.effective,
        est_tokens=len(task.goal.split()),
        files=len(task.context.files) + len(task.owns),
        attempt=routing_attempt,
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
        attempt=routing_attempt,
    )
    model_decision = _override_model(model_decision, agent_config, model_tier)
    resolved_model = model_decision.model

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

    task.permissions = permission.effective
    config_text, config_hash = snapshot(config)
    effective_timeout = (
        timeout_s
        if timeout_s is not None
        else task.budget.timeout_s
        if task.budget.timeout_s is not None
        else config.defaults.timeout_s
    )

    try:
        store = (
            RunStore(repo_root, resume_run)
            if resume_run is not None
            else RunStore.create(repo_root, config_hash=config_hash, run_id=run_id)
        )
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
        lock = RunLock.acquire(repo_root, store.run_id, takeover=resume_run is not None)
        start_attempt_number = 1
        prior_revision = 0
        if resume_run is not None:
            resume_state = _prepare_resume(store, task, max_attempts, retry_failed=retry_failed)
            if isinstance(resume_state, RunResult):
                if resume_state.exit_code == EXIT_OK:
                    worktree_manager.decommission_candidate(store.path(f"{node_id}/candidate"))
                    worktree_manager.decommission(node_id)
                return resume_state
            start_attempt_number, prior_revision = resume_state
            sweep_temp_files(store.run_dir)
            worktree_manager.decommission_candidate(store.path(f"{node_id}/candidate"))
        sensitive_paths: list[str] = []
        if resume_run is None:
            store.write_text("config.snapshot.json", config_text)
            task.run_id = store.run_id
            store.add_node(node_id)
            ensure_exclude_block(repo_root)
            base = resolve_base(repo_root, store.run_id, allow_dirty=allow_dirty, exclude=exclude)
            task.repo.input_commit = base.commit
            sensitive_paths = list(base.sensitive_paths)
        base_commit = task.repo.input_commit
        if base_commit is None:
            raise ValueError("run is missing its pinned input commit")
        node_dir = store.node_dir(node_id)
        context = answer_context(node_dir, node_id)
        input_key = _input_key(task, base_commit, config_hash)
        if context:
            input_key = hashlib.sha256((input_key + context).encode()).hexdigest()
        store.update_run(
            lambda record: setattr(
                record, "graph", _single_node_graph(task, pinned_agent, model_tier)
            )
        )
        store.write_spec(f"{node_dir.relative_to(store.run_dir).as_posix()}/task.spec.yaml", task)

        events = EventLog(store.events_path)
        route_log = RouteLog(
            events, store.routes_path, store.route_log_path, config.route_log, run_id=store.run_id
        )
        if resume_run is None:
            events.append(
                "run.started",
                {
                    "config_hash": config_hash,
                    "base_commit": base_commit,
                    "allow_dirty": allow_dirty,
                    "run_id": store.run_id,
                    "sensitive_paths": sensitive_paths,
                },
            )
        banner = format_banner((permission,), run_id=store.run_id)
        print(banner, file=stdout)

        _set_node(store, node_id, "running", input_key=input_key)

        worktree = worktree_manager.create(node_id, base_commit)
        runner = _AttemptRunner(
            repo_root=repo_root,
            store=store,
            events=events,
            task=task,
            node_id=node_id,
            node_dir=node_dir,
            worktree_manager=worktree_manager,
            worktree_path=worktree.path,
            base_commit=base_commit,
            input_key=input_key,
            access=access,
            permission=permission.effective,
            timeout_s=effective_timeout,
            adapter=registry[agent_name],
            agent_name=agent_name,
            question_revision=prior_revision,
        )

        return await _drive_attempts(
            runner,
            config,
            classification,
            available,
            levels,
            route_log,
            decision,
            model_decision,
            model_tier,
            start_attempt_number,
            max_attempts,
            non_interactive,
            stdout,
            stderr,
            effective_timeout,
            registry,
            pinned_agent,
            requested_task_permissions,
            cli_permissions,
            agent_permissions or {},
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        store.update_run(lambda record: setattr(record, "state", "interrupted"))
        EventLog(store.events_path).append("run.finished", {"state": "interrupted"})
        return RunResult(
            4,
            store.run_id,
            node_id,
            "interrupted",
            reasons=(f"resume with relay resume {store.run_id}",),
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


def _set_done(record: Any, *, commit: str) -> None:
    record.state = "done"
    record.final_commit = commit


def _remaining_timeout(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _single_node_graph(
    task: TaskSpec, pinned_agent: str | None = None, model_tier: str | None = None
) -> Graph:
    node = GraphNode.model_validate(
        {"id": task.id, **{name: getattr(task, name) for name in TaskCore.model_fields}}
    )
    node.routing.agent = pinned_agent
    if model_tier is not None:
        # Keep request provenance separate from the actual model in task.spec/events:
        # the same logical tier may resolve differently after an agent fallback.
        node.routing.model, node.routing.model_rule = model_tier, "cli-override"
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


def _prompt_answers(result: ResultSpec, out: TextIO) -> dict[str, AnswerValue] | str | None:
    answers: dict[str, AnswerValue] = {}
    for question in result.open_questions:
        while True:
            print(f"{question.id}: {question.text}", file=out)
            if question.options:
                print("Options: " + ", ".join(question.options), file=out)
            print(f"Default: {question.default or '(none)'}; :abort cancels", file=out)
            out.flush()
            try:
                answer = input().strip()
            except EOFError:
                return None
            if answer == ":abort":
                return answer
            answer = answer or question.default or ""
            if not answer and question.required:
                print("An answer is required.", file=out)
                continue
            if answer and question.kind == "choice" and answer not in (question.options or []):
                print("Choose one of the listed options.", file=out)
                continue
            if answer:
                answers[question.id] = answer
            break
    return answers


def _handle_input(
    runner: _AttemptRunner,
    outcome: RunResult,
    non_interactive: bool,
    out: TextIO,
    err: TextIO,
    attempt: int,
    max_attempts: int,
) -> RunResult | None:
    store, node_id = runner.store, runner.node_id
    if not non_interactive and sys.stdin.isatty() and out.isatty():
        answers = _prompt_answers(current_questions(store, node_id), out)
        if answers == ":abort":
            _set_node(store, node_id, "cancelled")
            store.update_run(lambda record: setattr(record, "state", "cancelled"))
            runner.events.append("run.finished", {"state": "cancelled"})
            return RunResult(4, store.run_id, node_id, "cancelled")
        if isinstance(answers, dict):
            record_answers(
                store,
                node_id,
                Submission(
                    submission_id=secrets.token_hex(8),
                    question_revision=runner.question_revision,
                    provenance="operator-tty",
                    submitted_at=iso(datetime.now(UTC)),
                    answers=answers,
                ),
                events=runner.events,
            )
            if attempt < max_attempts:
                runner.resume_session = None
                context = answer_context(runner.node_dir, node_id)
                runner.input_key = hashlib.sha256((runner.input_key + context).encode()).hexdigest()
                _set_node(store, node_id, "running", input_key=runner.input_key)
                store.update_run(lambda record: setattr(record, "state", "running"))
                return None
            _set_node(store, node_id, "blocked", blocked_reason="attempts_exhausted")
            outcome = RunResult(
                EXIT_BLOCKED, store.run_id, node_id, "blocked", reasons=("attempts_exhausted",)
            )
    print(
        f"relay reply {store.run_id} {node_id} --answers-file <answers.json> "
        f"--revision {runner.question_revision} --resume --yes",
        file=err,
    )
    runner.events.append("run.finished", {"state": "blocked"})
    return outcome


def _prepare_resume(
    store: RunStore,
    task: TaskSpec,
    max_attempts: int,
    *,
    retry_failed: bool = False,
) -> tuple[int, int] | RunResult:
    record = store.read_run()
    node_id = task.id
    if len(record.nodes) != 1 or node_id not in record.nodes:
        raise ValueError("foreground resume requires exactly one recorded node")
    node = record.nodes[node_id]
    recovery = (
        record.state == "interrupted"
        or (record.state == "done" and record.final_commit is None)
        or node.recovery_pending
        or (retry_failed and record.state == "failed")
    )
    revision = max(
        [
            node.question_revision,
            *[
                entry.question_revision
                for entry in read_answers(store.node_dir(node_id), node_id).submissions
            ],
        ]
    )
    with contextlib.suppress(OSError, ValueError):
        previous = load_spec(ResultSpec, store.node_dir(node_id) / "result.spec.yaml")
        if previous.status == "needs_input":
            revision = max(revision, previous.question_revision or 1)
    if recovery:
        if not prepare_interrupted(store, node_id):
            return RunResult(
                EXIT_BLOCKED,
                store.run_id,
                node_id,
                "blocked",
                reasons=(
                    f"worker_unconfirmed; inspect .agents/runs/{store.run_id}/run.json "
                    "before resuming",
                ),
            )
        if node.state == "done" and node.output_commit is not None:
            return _finish_recovered_node(store, node_id, node.output_commit)
        EventLog(store.events_path).append(
            "warning", {"node_id": node_id, "message": "resuming interrupted foreground node"}
        )
    elif record.state == "blocked":
        pending = current_questions(store, node_id)
        revision = pending.question_revision or 1
        submissions = read_answers(store.node_dir(node_id), node_id).submissions
        if not any(entry.question_revision == revision for entry in submissions):
            raise ValueError("record answers with relay reply before resuming")
    else:
        raise ValueError(
            "resume requires an interrupted or answered blocked run; "
            "use relay run --retry for a failed run"
        )
    next_attempt = node.attempts + 1
    if next_attempt > max_attempts:
        _set_node(store, node_id, "blocked", blocked_reason="attempts_exhausted")
        store.update_run(lambda record: setattr(record, "state", "blocked"))
        EventLog(store.events_path).append("run.finished", {"state": "blocked"})
        return RunResult(
            EXIT_BLOCKED,
            store.run_id,
            node_id,
            "blocked",
            reasons=("attempts_exhausted; raise the total with relay resume --max-attempts N",),
        )
    original = load_spec(TaskSpec, store.node_dir(node_id) / "task.spec.yaml")
    task.repo.input_commit = original.repo.input_commit
    return next_attempt, revision


def _finish_recovered_node(store: RunStore, node_id: str, output: str) -> RunResult:
    if (
        read_ref(store.repo_root, node_ref(store.run_id, node_id)) != output
        or read_ref(store.repo_root, integration_branch(store.run_id)) != output
    ):
        raise PublicationError(
            "completed node refs do not match the durable output; refusing redispatch"
        )
    # As in the attempt runner: `run.finished` first, then `done` with `final_commit` in one
    # write, so `done` never precedes the last event. A crash in between repeats this step.
    EventLog(store.events_path).append(
        "run.finished", {"state": "done", "integration_commit": output}
    )
    record = store.read_run()
    record.state = "done"
    record.final_commit = output
    record.nodes[node_id].recovery_pending = False
    store.write_run(record)
    return RunResult(EXIT_OK, store.run_id, node_id, "done", output, output)


def _set_attempt_timeout(runner: _AttemptRunner, timeout: float) -> bool:
    limit = runner.task.budget.max_duration_s
    if limit is None:
        return True
    used = runner.store.read_run().nodes[runner.node_id].active_duration_s
    remaining = limit - used
    if remaining <= 0:
        _set_node(runner.store, runner.node_id, "blocked", blocked_reason="duration_exhausted")
        runner.store.update_run(lambda record: setattr(record, "state", "blocked"))
        runner.events.append("run.finished", {"state": "blocked"})
        return False
    runner.timeout_s = min(timeout, remaining)
    return True


async def _drive_attempts(
    runner: _AttemptRunner,
    config: Config,
    classification: Classification,
    available: Mapping[str, bool],
    levels: Mapping[str, tuple[PermissionLevel, ...]],
    route_log: RouteLog,
    initial_decision: AgentDecision,
    initial_model: ModelDecision,
    model_tier: str | None,
    start_attempt_number: int,
    max_attempts: int,
    non_interactive: bool,
    stdout: TextIO,
    stderr: TextIO,
    effective_timeout: float,
    adapters: Mapping[str, AgentAdapter],
    pinned_agent: str | None,
    requested_task_permissions: PermissionLevel | None,
    cli_permissions: PermissionLevel | None,
    agent_permissions: Mapping[str, PermissionLevel],
) -> RunResult:
    store, node_id, task = runner.store, runner.node_id, runner.task
    agent_name: str = runner.agent_name
    agent_config = config.agents[agent_name]
    events = runner.events
    category = task.category
    last_reason = f"node {node_id} did not complete an attempt"
    last_exit_code = EXIT_FAILURE
    attempts_run = start_attempt_number - 1
    current_model = initial_model
    previous: PreviousRoute | None = None
    excluded: set[str] = set()
    if start_attempt_number > 1:
        history = read_events(store.events_path)
        prior_agent: str | None = None
        restored_permission: PermissionLevel | None = None
        for event in history:
            if event.type != "node.routed" or event.data["node_id"] != node_id:
                continue
            entry = RouteEntry.model_validate(event.data["decision"])
            if not entry.executed or entry.agent is None or entry.attempt >= start_attempt_number:
                continue
            selected = select_model(
                config.agents[entry.agent],
                classification,
                est_tokens=len(task.goal.split()),
                files=len(task.context.files) + len(task.owns),
                attempt=entry.attempt,
            )
            if entry.event == "escalated" and prior_agent == entry.agent:
                selected = escalate(config.agents[entry.agent], current_model)
            elif prior_agent == entry.agent and entry.model == current_model.model:
                selected = current_model
            if prior_agent and prior_agent != entry.agent:
                excluded.add(prior_agent)
            current_model = replace(
                selected,
                model=entry.model or NO_MODEL,
                effort=entry.effort,
                model_rule=entry.model_rule or selected.model_rule,
            )
            agent_name = prior_agent = entry.agent
            restored_permission = entry.permissions
        agent_config = config.agents[agent_name]
        if prior_agent is not None:
            requested_task_permissions = _most_restrictive(
                requested_task_permissions, restored_permission
            )
        failure = next(
            (
                event
                for event in reversed(history)
                if event.type == "warning"
                and event.data.get("node_id") == node_id
                and event.data.get("attempt") == start_attempt_number - 1
            ),
            None,
        )
        if failure is not None and prior_agent is not None:
            previous = PreviousRoute(
                agent=agent_name, model=current_model.model, reason=str(failure.data["message"])
            )

    for attempt in range(start_attempt_number, max_attempts + 1):
        # Every retry goes back through the Router before it starts (section 8.2); without
        # an explicit pin, fallback retains the existing worktree and total attempt count.
        retry_decision = initial_decision
        retry_model = current_model
        retry_event: RouteEvent = "routed"
        if attempt > 1:
            next_model = (
                escalate(agent_config, current_model)
                if previous is not None and model_tier is None
                else current_model
            )
            retry_available = dict(available)
            retry_pin: str | None = agent_name
            if next_model != current_model and available.get(agent_name, False):
                retry_event = "escalated"
            elif previous is not None and pinned_agent is None:
                excluded.add(agent_name)
                retry_available = {k: v and k not in excluded for k, v in available.items()}
                retry_pin = None
                retry_event = "fallback"
            retry_decision = route_agent(
                config,
                classification,
                pinned=retry_pin,
                available=retry_available,
                levels=levels,
                permissions=runner.permission,
                est_tokens=len(task.goal.split()),
                files=len(task.context.files) + len(task.owns),
                attempt=attempt,
            )
            if retry_decision.rejected and retry_pin is None:
                # At the ceiling, remaining attempts can still retry the current model
                # when no other eligible worker exists (section 4.3).
                retry_decision = route_agent(
                    config,
                    classification,
                    pinned=agent_name,
                    available=available,
                    levels=levels,
                    permissions=runner.permission,
                    est_tokens=len(task.goal.split()),
                    files=len(task.context.files) + len(task.owns),
                    attempt=attempt,
                )
                retry_event = "routed"
            if retry_decision.rejected or retry_decision.agent is None:
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
                    f"attempt {attempt}: no eligible agent for the retry: {retry_decision.reason}"
                )
                last_exit_code = EXIT_NO_AGENT
                break
            chosen_agent = retry_decision.agent
            if chosen_agent != agent_name:
                retry_decision = replace(retry_decision, agent_rule="fallback")
                agent_name = chosen_agent
                agent_config = config.agents[agent_name]
                runner.resume_session = None
            retry_model = current_model
            if previous is not None and retry_event == "fallback":
                retry_model = select_model(
                    agent_config,
                    classification,
                    est_tokens=len(task.goal.split()),
                    files=len(task.context.files) + len(task.owns),
                    attempt=attempt,
                )
            if retry_event == "escalated":
                retry_model = next_model
        retry_model = _override_model(retry_model, agent_config, model_tier)
        current_model = retry_model
        resolved_model = retry_model.model
        permission = decide(
            agent_name,
            levels[agent_name],
            resolve_level(
                task=requested_task_permissions,
                cli=_most_restrictive(cli_permissions, agent_permissions.get(agent_name)),
                agent=agent_config.permissions,
                default=config.defaults.permissions,
                planner_read_only=runner.access == "read",
            ),
        )
        if not permission.supported:
            last_reason, last_exit_code = (
                permission.reason or "unsupported permissions",
                EXIT_INVALID,
            )
            break
        task.permissions = runner.permission = permission.effective
        runner.agent_name, runner.adapter = agent_name, adapters[agent_name]
        task.routing.model, task.routing.agent = resolved_model, agent_name
        task.routing.agent_rule = retry_decision.agent_rule
        task.routing.effort, task.routing.model_rule = retry_model.effort, retry_model.model_rule
        retry_decision = replace(retry_decision, permissions=runner.permission)
        route_log.route(
            node_id=node_id,
            attempt=attempt,
            event=retry_event,
            category=category,
            complexity=classification.complexity,
            agent_decision=retry_decision,
            model_decision=retry_model,
            title=task.title,
            goal=task.goal,
            previous=previous,
            resumed=start_attempt_number > 1,
        )
        store.write_spec(f"{runner.node_dir.name}/task.spec.yaml", task)

        if not _set_attempt_timeout(runner, effective_timeout):
            return RunResult(
                EXIT_BLOCKED, store.run_id, node_id, "blocked", reasons=("duration_exhausted",)
            )
        store.update_run(lambda record: setattr(record, "state", "running"))
        attempts_run = attempt
        _set_node(store, node_id, "running", attempts=attempt)
        outcome = await runner.run(attempt, None if resolved_model == NO_MODEL else resolved_model)
        if isinstance(outcome, RunResult):
            if outcome.exit_code == EXIT_BLOCKED:
                outcome = _handle_input(
                    runner, outcome, non_interactive, stdout, stderr, attempt, max_attempts
                )
                if outcome is None:
                    previous = None
                    continue
            return outcome

        # The attempt failed. Record it durably; the next iteration re-routes and starts a
        # new attempt, whose `start_attempt` archives this attempt's result file.
        if (
            task.budget.max_duration_s is not None
            and store.read_run().nodes[node_id].active_duration_s >= task.budget.max_duration_s
        ):
            _set_node(store, node_id, "blocked", blocked_reason="duration_exhausted")
            store.update_run(lambda record: setattr(record, "state", "blocked"))
            events.append("run.finished", {"state": "blocked"})
            return RunResult(
                EXIT_BLOCKED, store.run_id, node_id, "blocked", reasons=("duration_exhausted",)
            )
        last_reason = outcome.reason
        last_exit_code = outcome.exit_code
        previous = PreviousRoute(agent=agent_name, model=resolved_model, reason=last_reason)
        events.append(
            "warning",
            {
                "message": last_reason,
                "node_id": node_id,
                "attempt": attempt,
                "exit": last_exit_code,
            },
        )
        if attempt >= max_attempts or (not outcome.retryable and "timed out" not in outcome.reason):
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
