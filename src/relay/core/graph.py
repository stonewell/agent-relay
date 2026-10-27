"""Foreground graph coordination (design sections 6, 8.1–8.3, 10, 14)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, TypeVar, cast

from relay.adapters.base import AgentAdapter, ReuseFingerprint

from . import publish as publication
from .answers import answer_context, read_answers
from .capture import capture_changes
from .classifier import Classification, classify
from .events import EventLog, read_events
from .gitbase import (
    DirtyTreeError,
    EmptyRepositoryError,
    InvalidRefError,
    NotARepositoryError,
    base_ref,
    read_ref,
    resolve_base,
    write_ref,
)
from .gitexclude import ensure_exclude_block
from .model_router import ModelDecision, escalate, select_model
from .permissions import decide, format_banner, resolve_level
from .platform.proc import ProcessCancelled, cancel_processes_when, observe_processes
from .platform.procid import process_identity
from .progress import ProgressChannel
from .publish import _ORCHESTRATOR_ENV as ORCHESTRATOR_ENV  # pyright: ignore[reportPrivateUsage]
from .publish import _compare_and_swap as compare_and_swap  # pyright: ignore[reportPrivateUsage]
from .publish import _git as git  # pyright: ignore[reportPrivateUsage]
from .recovery import prepare_interrupted
from .registry import Config, snapshot
from .route_log import RouteLog
from .router import AgentDecision, route_agent
from .run import _AttemptFailure, _AttemptRunner, _default_adapters, _probe  # pyright: ignore[reportPrivateUsage]
from .runlock import RunLock
from .runrecord import WorkerRecord
from .scheduler import Outcome, SchedulerControl, schedule
from .scopes import writers_overlap
from .sessions import SessionOwner
from .snapshots import merge_snapshot
from .specs import Graph, GraphNode, ResultSpec, TaskSpec, load_spec
from .specs.checkpoint import RetainedBase
from .specs.common import PermissionLevel, normalize_repo_path
from .specs.route import PreviousRoute, RouteEntry, RouteEvent
from .specs.task import Commands, RunCheck
from .store import RunStore, read_text
from .verify import VerificationError, effective_scope, executable_commands, verify
from .worktree import WorktreeManager

VERIFIER_VERSION = "graph-verifier-1"
T = TypeVar("T")


async def _drain_on_cancel(work: Coroutine[Any, Any, T], cancellation: threading.Event) -> T:
    """Keep ownership until cooperative workers finish, even after repeated cancellation."""
    pending = asyncio.create_task(work)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        cancellation.set()
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve a raced worker exception without replacing the caller's interruption.
        if not pending.cancelled():
            pending.exception()
        raise


async def _run_owned_thread(work: Callable[[], T], cancellation: threading.Event) -> T:
    """Drain the actual thread even if Runner shutdown cancels every asyncio task."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-graph")
    pending = executor.submit(copy_context().run, work)
    try:
        return await asyncio.wrap_future(pending)
    except asyncio.CancelledError:
        cancellation.set()
        # The asyncio wrapper can be cancelled while its underlying thread still runs.
        # Yield during drainage so other workers can receive their cancellation too.
        while not pending.done():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                cancellation.set()
        if not pending.cancelled():
            pending.exception()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@dataclass(frozen=True)
class GraphResult:
    exit_code: int
    run_id: str | None
    state: str
    nodes: dict[str, Outcome]
    integration_commit: str | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "run_id": self.run_id,
            "state": self.state,
            "nodes": {
                key: {"state": value.state, "exit_code": value.exit_code, "reason": value.reason}
                for key, value in self.nodes.items()
            },
            "integration_commit": self.integration_commit,
            **({"error": self.error} if self.error else {}),
        }


def task_for(node: GraphNode, graph: Graph, run_id: str) -> TaskSpec:
    data = node.model_dump(mode="json", by_alias=True)
    data.pop("priority", None)
    data["run_id"] = run_id
    task = TaskSpec.model_validate(data)
    if task.repo.root != "." or task.repo.input_commit or task.repo.base_branch:
        raise ValueError("graph nodes use the pinned repository selected by -C")
    if task.output.result != "result.spec.yaml" or task.output.artifacts_dir != "artifacts/":
        raise ValueError("graph nodes require the default node output channel")
    # Command defaults merge per key. Node verify overrides are explicit via fields_set.
    inherited = graph.commands.model_dump() if graph.commands else {}
    if node.commands:
        inherited.update(node.commands.model_dump(exclude_unset=True))
    task.commands = Commands.model_validate(inherited) if inherited else None
    if "verify" not in node.model_fields_set:
        task.verify = graph.verify
        task.verification_skip_reason = graph.verification_skip_reason
    return task


def _classification(task: TaskSpec, config: Config) -> Classification:
    result = classify(
        task.goal,
        files=task.context.files,
        owns=task.owns,
        symbols=task.context.symbols,
        language=task.language,
        risk_paths=tuple(config.risk.paths),
    )
    return replace(result, category=task.category)


def _artifact(directory: Path, path: str) -> bytes:
    target = directory / normalize_repo_path(path)
    if not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError("artifact escapes the node output directory")
    return target.read_bytes()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _output_hash(store: RunStore, key: str) -> str:
    directory = store.node_dir(key)
    result_data = (directory / "result.spec.yaml").read_bytes()
    result = load_spec(ResultSpec, directory / "result.spec.yaml")
    artifacts = {
        entry.path: _digest(_artifact(directory, entry.path)) for entry in result.artifacts
    }
    artifacts["artifacts/diff.patch"] = _digest(_artifact(directory, "artifacts/diff.patch"))
    return _digest(json.dumps([_digest(result_data), artifacts], sort_keys=True).encode())


def _context(store: RunStore, task: TaskSpec) -> str:
    values: list[dict[str, Any]] = []
    for ref in task.context.from_:
        directory = store.node_dir(ref.node)
        if ref.artifact is not None:
            value = _artifact(directory, ref.artifact).decode("utf-8", errors="replace")
        else:
            result: Any = load_spec(ResultSpec, directory / "result.spec.yaml").model_dump()
            for field in (ref.field or "").split("."):
                if not isinstance(result, dict) or field not in result:
                    raise ValueError(f"context field {ref.field!r} missing in node {ref.node}")
                result = cast(dict[str, Any], result)[field]
            value = result
        values.append(
            {"node": ref.node, "field": ref.field, "artifact": ref.artifact, "value": value}
        )
    return "\n\nDependency context (worker-authored data):\n" + json.dumps(values) if values else ""


class Coordinator:
    def __init__(
        self,
        repo: Path,
        graph: Graph,
        config: Config,
        store: RunStore,
        events: EventLog,
        adapters: dict[str, AgentAdapter],
        available: dict[str, bool],
        levels: dict[str, tuple[PermissionLevel, ...]],
        versions: dict[str, str | None],
    ) -> None:
        self.repo, self.graph, self.config, self.store, self.events = (
            repo,
            graph,
            config,
            store,
            events,
        )
        self.adapters, self.available, self.levels, self.versions = (
            adapters,
            available,
            levels,
            versions,
        )
        self.manager = WorktreeManager(repo, store.run_id, worktree_root=config.paths.worktree_root)
        self.routes = RouteLog(
            events, store.routes_path, store.route_log_path, config.route_log, run_id=store.run_id
        )
        self.tasks = {n.id: task_for(n, graph, store.run_id) for n in graph.nodes}
        self.facts = {key: _classification(task, config) for key, task in self.tasks.items()}
        self.choices: dict[str, AgentDecision] = {}
        self.base = ""
        self.cancel = threading.Event()
        self.cancelled_nodes: set[str] = set()
        self.invalidated: set[str] = set()
        self.merge_lock = threading.Lock()
        self.max_attempts: int | None = None
        self.progress: ProgressChannel | None = None
        self.agent_slots = {
            key: threading.BoundedSemaphore(agent.concurrency or config.defaults.concurrency)
            for key, agent in config.agents.items()
        }

    def choose(self, node: GraphNode, load: dict[str, int]) -> str | None:
        task = self.tasks[node.id]
        resolution = resolve_level(
            task=task.permissions,
            default=self.config.defaults.permissions,
            planner_read_only=task.access == "read",
        )
        decision = route_agent(
            self.config,
            self.facts[node.id],
            pinned=task.routing.agent,
            available=self.available,
            levels=self.levels,
            permissions=resolution.effective,
            load=load,
        )
        self.choices[node.id] = decision
        return None if decision.queued else decision.agent or ""

    def transition(self, key: str, outcome: Outcome) -> None:
        def change(record: Any) -> None:
            record.nodes[key].state = outcome.state
            if outcome.state != "running":
                record.nodes[key].blocked_reason = outcome.reason

        before = self.store.read_run().nodes[key].state
        self.store.update_run(change)
        self.events.append(
            "node.state",
            {"node_id": key, "from": before, "to": outcome.state, "reason": outcome.reason},
        )

    def integration_task(self) -> TaskSpec:
        return TaskSpec(
            run_id=self.store.run_id,
            id="graph-verification",
            category="implement",
            goal="Verify graph",
            commands=self.graph.commands,
            acceptance=list(self.graph.integration_acceptance),
            verify=self.graph.verify,
            verification_skip_reason=self.graph.verification_skip_reason,
        )

    def verify_snapshot(self, commit: str) -> None:
        if not any(task.access == "write" for task in self.tasks.values()):
            return
        with self.merge_lock:
            worktree = self.manager.create("graph-verification", commit)
            capture = None
            try:
                capture = capture_changes(
                    worktree.path, self.base, candidate_dir=self.store.path("merge-candidate")
                )
                task = self.integration_task()
                writer_scopes = [
                    effective_scope(t) for t in self.tasks.values() if t.access == "write"
                ]
                result = verify(capture, task, overall_timeout_s=self.config.defaults.timeout_s)
                # Each graph scope and the union of writers are independent restrictions.
                if all(scope is not None for scope in writer_scopes):
                    from .verify import scope_violations

                    union = tuple(p for scope in writer_scopes if scope for p in scope)
                    if scope_violations(capture.changes, union):
                        raise VerificationError(
                            "merged snapshot exceeds contributing writer scopes"
                        )
                self.events.append(
                    "verify.result", {"node_id": "graph-verification", "outcome": result.outcome}
                )
                if not result.publication_allowed:
                    raise VerificationError(f"merged snapshot verification {result.outcome}")
            finally:
                if capture is not None:
                    self.manager.decommission_candidate(capture.candidate_path)
                self.manager.decommission("graph-verification")

    def snapshot_for(self, node: GraphNode) -> str:
        record = self.store.read_run()
        order = {n.id: i for i, n in enumerate(self.graph.ordered_nodes(priority=False))}
        outputs: list[tuple[str, str]] = []
        for dep in sorted(node.depends_on, key=lambda key: order[key]):
            commit = record.nodes[dep].output_commit
            if commit is None:
                raise ValueError(f"dependency {dep} has no output commit")
            outputs.append((dep, commit))
        return merge_snapshot(
            self.repo,
            self.store.run_id,
            node.id,
            self.base,
            outputs,
            verify=self.verify_snapshot,
            resolve=self.resolve_conflict,
        )

    def fingerprint(self, task: TaskSpec) -> str | None:
        adapter = self.adapters.get(task.routing.agent or "")
        return adapter.reuse_fingerprint(task) if isinstance(adapter, ReuseFingerprint) else None

    def key(self, task: TaskSpec, commit: str, context: str, fingerprint: str | None) -> str:
        dependencies = {
            dep: [self.store.read_run().nodes[dep].output_commit, _output_hash(self.store, dep)]
            for dep in task.depends_on
        }
        payload = {
            "task": task.model_dump(mode="json", by_alias=True),
            "input_commit": commit,
            "tree": git(self.repo, "rev-parse", f"{commit}^{{tree}}").strip(),  # pyright: ignore[reportPrivateUsage]
            "dependencies": dependencies,
            "context": context,
            "answers": answer_context(self.store.node_dir(task.id), task.id),
            "project": read_text(self.store.path("PROJECT.spec.md")),
            "config": self.store.read_run().config_hash,
            "versions": self.versions,
            "execution_fingerprint": fingerprint,
            "verifier": VERIFIER_VERSION,
        }
        return _digest(json.dumps(payload, sort_keys=True).encode())

    def reusable(self, key: str, input_key: str, fingerprint: str | None) -> bool:
        if key in self.invalidated:
            return False
        if fingerprint is None:
            return False
        node = self.store.read_run().nodes[key]
        if node.input_key != input_key or not node.output_commit:
            return False
        try:
            if (
                read_ref(self.repo, publication.node_ref(self.store.run_id, key))
                != node.output_commit
            ):
                return False
            publication._require_commit(self.repo, node.output_commit, "stored output")  # pyright: ignore[reportPrivateUsage]
            return read_text(self.store.node_dir(key) / "output.hash") == _output_hash(
                self.store, key
            )
        except (OSError, ValueError, publication.PublicationError):
            return False

    def observe(self, key: str, phase: str, pid: int | None) -> None:
        def update(record: Any) -> None:
            current = record.nodes[key]
            current.process_tracking = True
            if phase == "starting":
                current.worker_pending, current.worker = True, None
            elif phase == "started" and pid is not None:
                identity = process_identity(pid)
                current.worker = WorkerRecord(pid=identity.pid, start=identity.start)
                current.worker_pending = False
            elif phase == "finished":
                current.worker_pending, current.worker = False, None

        self.store.update_run(update)

    async def execute(self, node: GraphNode, agent: str, cancellation: threading.Event) -> Outcome:
        # All blocking adapter and git work is off the event loop. A shared RunStore and
        # EventLog serialize durable writes from these worker threads.
        def run() -> Outcome:
            with (
                cancel_processes_when(cancellation),
                observe_processes(partial(self.observe, node.id)),
            ):
                return self.execute_sync(node, agent, cancellation)

        try:
            return await _run_owned_thread(run, cancellation)
        except ProcessCancelled:
            return Outcome("cancelled", 4, "attempt cancelled")
        except VerificationError as exc:
            return Outcome("failed", 5, str(exc))

    def execute_sync(self, node: GraphNode, agent: str, cancellation: threading.Event) -> Outcome:
        task = self.tasks[node.id].model_copy(deep=True)
        commit = self.snapshot_for(node)
        context = _context(self.store, task)
        old = self.store.read_run().nodes[node.id]
        previous_task = self.store.node_dir(node.id) / "task.spec.yaml"
        fingerprint = (
            self.fingerprint(load_spec(TaskSpec, previous_task))
            if old.output_commit is not None and previous_task.exists()
            else None
        )
        key = self.key(task, commit, context, fingerprint)
        if self.reusable(node.id, key, fingerprint):
            self.manager.decommission(node.id)
            self.store.update_run(lambda r: setattr(r.nodes[node.id], "recovery_pending", False))
            self.events.append("node.reused", {"node_id": node.id, "input_key": key})
            return Outcome("done")
        if old.output_commit is not None and fingerprint is None:
            self.events.append(
                "warning",
                {"node_id": node.id, "message": "execution_fingerprint_uncomparable"},
            )
        if self.max_attempts is not None:
            # A raised retry cap must not invalidate otherwise reusable completed nodes.
            task.budget.max_attempts = node.budget.max_attempts = self.max_attempts
            self.tasks[node.id].budget.max_attempts = self.max_attempts

            def raise_limit(record: Any) -> None:
                for saved in record.graph.nodes:
                    if saved.id == node.id:
                        saved.budget.max_attempts = self.max_attempts

            self.store.update_run(raise_limit)
        limit = task.budget.max_attempts or 1
        if old.attempts >= limit:
            reason = (
                "execution_fingerprint_uncomparable; attempts_exhausted; "
                "increase --max-attempts to rerun"
                if old.output_commit is not None and fingerprint is None
                else "attempts_exhausted"
            )
            return Outcome("blocked", 6, reason)
        if old.output_commit is not None:
            self.events.append("node.invalidated", {"node_id": node.id, "input_key": key})
            descendants = {node.id}
            for child in self.graph.ordered_nodes():
                if set(child.depends_on) & descendants:
                    descendants.add(child.id)
            self.invalidated.update(descendants)
        if old.blocked_reason in {"needs_input", "attempts_exhausted"}:
            answers = read_answers(self.store.node_dir(node.id), node.id)
            if old.question_revision and not any(
                answer.question_revision == old.question_revision for answer in answers.submissions
            ):
                return Outcome("blocked", 6, "needs_input")
        if self.manager.node_path(node.id).exists() and previous_task.exists():
            recorded = load_spec(TaskSpec, previous_task)
            if recorded.repo.input_commit != commit or old.output_commit is not None:
                self.manager.decommission(node.id)
        task.repo.input_commit = commit
        project = read_text(self.store.path("PROJECT.spec.md"))
        task.goal += context + ("\n\nProject instructions:\n" + project if project else "")
        worktree = self.manager.create(node.id, commit, exist_ok=True)
        directory = self.store.node_dir(node.id)
        decision: AgentDecision = self.choices[node.id]
        previous: PreviousRoute | None = None
        model: ModelDecision | None = None
        runner: _AttemptRunner | None = None
        excluded: set[str] = set()
        pinned = node.routing.agent
        outcome = _AttemptFailure("no attempt completed", 1)
        if old.attempts:
            history = read_events(self.store.events_path)
            failure = next(
                (
                    event
                    for event in reversed(history)
                    if event.type == "warning"
                    and event.data.get("node_id") == node.id
                    and event.data.get("attempt") == old.attempts
                ),
                None,
            )
            if failure is not None:
                prior_agent: str | None = None
                for item in history:
                    if item.type != "node.routed" or item.data["node_id"] != node.id:
                        continue
                    entry = RouteEntry.model_validate(item.data["decision"])
                    if entry.agent is None or not entry.executed:
                        continue
                    selected = select_model(
                        self.config.agents[entry.agent], self.facts[node.id], attempt=entry.attempt
                    )
                    if entry.event == "escalated" and prior_agent == entry.agent and model:
                        selected = escalate(self.config.agents[entry.agent], model)
                    if prior_agent and prior_agent != entry.agent:
                        excluded.add(prior_agent)
                    model = selected
                    agent = prior_agent = entry.agent
                if model is not None:
                    previous = PreviousRoute(
                        agent=agent, model=model.model, reason=str(failure.data["message"])
                    )
                    decision = replace(decision, agent=agent)
        for attempt in range(old.attempts + 1, limit + 1):
            if cancellation.is_set():
                raise ProcessCancelled("attempt cancelled")
            event: RouteEvent = "routed"
            if previous is not None:
                assert model is not None
                upgraded = escalate(self.config.agents[agent], model)
                if upgraded != model:
                    model, event = upgraded, "escalated"
                    decision = route_agent(
                        self.config,
                        self.facts[node.id],
                        pinned=agent,
                        available=self.available,
                        levels=self.levels,
                        permissions=decision.permissions,
                        attempt=attempt,
                    )
                elif pinned is None:
                    excluded.add(agent)
                    availability = {k: v and k not in excluded for k, v in self.available.items()}
                    decision = route_agent(
                        self.config,
                        self.facts[node.id],
                        available=availability,
                        levels=self.levels,
                        permissions=decision.permissions,
                        attempt=attempt,
                    )
                    if decision.rejected:
                        decision = route_agent(
                            self.config,
                            self.facts[node.id],
                            pinned=agent,
                            available=self.available,
                            levels=self.levels,
                            permissions=decision.permissions,
                            attempt=attempt,
                        )
                        event = "routed"
                    else:
                        event = "fallback"
                        decision = replace(decision, agent_rule="fallback")
                    agent = decision.agent or ""
                    model = None
                    if runner:
                        runner.resume_session = None
                else:
                    decision = route_agent(
                        self.config,
                        self.facts[node.id],
                        pinned=agent,
                        available=self.available,
                        levels=self.levels,
                        permissions=decision.permissions,
                        attempt=attempt,
                    )
            if decision.rejected or not agent:
                self.routes.route(
                    node_id=node.id,
                    attempt=attempt,
                    event="rejected",
                    category=task.category,
                    complexity=self.facts[node.id].complexity,
                    agent_decision=decision,
                    executed=False,
                )
                return Outcome("failed", 3, decision.reason)
            config = self.config.agents[agent]
            permission = decide(
                agent,
                self.levels[agent],
                resolve_level(
                    task=task.permissions,
                    agent=config.permissions,
                    default=self.config.defaults.permissions,
                    planner_read_only=task.access == "read",
                ),
            )
            if not permission.supported:
                return Outcome("failed", 2, permission.reason)
            print(format_banner((permission,), run_id=self.store.run_id), file=sys.stderr)
            decision = replace(decision, permissions=permission.effective)
            model = model or select_model(config, self.facts[node.id], attempt=attempt)
            self.routes.route(
                node_id=node.id,
                attempt=attempt,
                event=event,
                category=task.category,
                complexity=self.facts[node.id].complexity,
                agent_decision=decision,
                model_decision=model,
                goal=task.goal,
                title=task.title,
                previous=previous,
                resumed=old.attempts > 0,
            )
            task.routing.agent, task.routing.model = agent, model.model
            task.routing.effort = model.effort
            task.routing.agent_rule, task.routing.model_rule = decision.agent_rule, model.model_rule
            task.permissions = permission.effective
            remaining = task.budget.max_duration_s
            timeout = float(task.budget.timeout_s or self.config.defaults.timeout_s)
            if remaining is not None:
                timeout = min(
                    timeout, remaining - self.store.read_run().nodes[node.id].active_duration_s
                )
            if timeout <= 0:
                return Outcome("blocked", 6, "duration_exhausted")
            fingerprint = self.fingerprint(task)
            key = self.key(self.tasks[node.id], commit, context, fingerprint)
            if runner is None:
                runner = _AttemptRunner(
                    self.repo,
                    self.store,
                    self.events,
                    task,
                    node.id,
                    directory,
                    self.manager,
                    worktree.path,
                    commit,
                    key,
                    task.access or "write",
                    permission.effective,
                    timeout,
                    self.adapters[agent],
                    agent,
                    question_revision=old.question_revision,
                    graph_mode=True,
                    progress=self.progress,
                )
            runner.agent_name, runner.adapter, runner.timeout_s = (
                agent,
                self.adapters[agent],
                timeout,
            )
            runner.permission = permission.effective
            runner.input_key = key
            slots = self.agent_slots[agent]
            while not slots.acquire(timeout=0.1):
                if cancellation.is_set():
                    raise ProcessCancelled("attempt cancelled waiting for agent capacity")
            try:
                if cancellation.is_set():
                    raise ProcessCancelled("attempt cancelled before dispatch")
                if old.output_commit is not None:
                    ref = publication.node_ref(self.store.run_id, node.id)
                    current = read_ref(self.repo, ref)
                    if current and not publication._delete_ref_cas(self.repo, ref, current):  # pyright: ignore[reportPrivateUsage]
                        raise publication.PublicationError("output ref changed during invalidation")
                    old.output_commit = None
                self.store.update_run(
                    lambda r, value=key: setattr(r.nodes[node.id], "input_key", value)
                )
                self.store.update_run(lambda r: setattr(r.nodes[node.id], "output_commit", None))
                self.store.update_run(
                    lambda r, count=attempt: setattr(r.nodes[node.id], "attempts", count)
                )
                self.store.write_spec(f"{old.dir}/task.spec.yaml", task)
                outcome = asyncio.run(
                    runner.run(attempt, None if model.model == "default" else model.model)
                )
            finally:
                slots.release()
            if not isinstance(outcome, _AttemptFailure):
                if outcome.exit_code == 0:
                    self.store.write_text(
                        f"{old.dir}/output.hash", _output_hash(self.store, node.id)
                    )
                    self.manager.decommission(node.id)
                    return Outcome("done")
                return Outcome(
                    "blocked" if outcome.exit_code == 6 else "failed",
                    outcome.exit_code,
                    outcome.error or ", ".join(outcome.reasons),
                )
            previous = PreviousRoute(agent=agent, model=model.model, reason=outcome.reason)
            self.events.append(
                "warning", {"message": outcome.reason, "node_id": node.id, "attempt": attempt}
            )
            if not outcome.retryable and "timed out" not in outcome.reason:
                break
        return Outcome("failed", outcome.exit_code, outcome.reason)

    def resolve_conflict(self, left: str, right: str, consumer: str) -> str:
        """One bounded writer edits only conflicted paths in the exact automatic merge tree."""
        identity = _digest(f"{consumer}:{left}:{right}".encode())[:24]
        key = f"resolve-{identity}"
        if key in self.tasks:
            raise ValueError("graph node id collides with an implicit conflict resolver")
        record = self.store.read_run()
        if key in record.nodes and record.nodes[key].attempts:
            saved_task = load_spec(TaskSpec, self.store.node_dir(key) / "task.spec.yaml")
            fingerprint = self.fingerprint(saved_task)
            resolver_key = self.key(
                saved_task,
                str(saved_task.repo.input_commit),
                self.graph.model_dump_json(),
                fingerprint,
            )
            result = load_spec(ResultSpec, self.store.node_dir(key) / "result.spec.yaml")
            if (
                record.nodes[key].state == "done"
                and self.reusable(key, resolver_key, fingerprint)
                and result.changes
                and result.changes.output_commit
                and result.changes.output_commit == record.nodes[key].output_commit
            ):
                tree = git(
                    self.repo, "rev-parse", f"{result.changes.output_commit}^{{tree}}"
                ).strip()  # pyright: ignore[reportPrivateUsage]
                self.manager.decommission(key)
                self.store.update_run(lambda r: setattr(r.nodes[key], "recovery_pending", False))
                return git(
                    self.repo,
                    "commit-tree",
                    tree,
                    "-p",
                    left,
                    "-p",
                    right,
                    "-m",
                    f"relay: resolve {consumer}",
                    env=ORCHESTRATOR_ENV,
                ).strip()  # pyright: ignore[reportPrivateUsage]
            raise publication.IntegrationConflictError(
                "conflict resolver cache cannot be reused; already exhausted its one attempt"
            )
        if len(record.nodes) >= self.graph.policy.max_nodes:
            raise publication.IntegrationConflictError("conflict resolver exceeds policy.max_nodes")
        proc = publication._run(self.repo, "merge-tree", "--write-tree", "--name-only", left, right)  # pyright: ignore[reportPrivateUsage]
        if proc.returncode != 1:
            raise publication.IntegrationConflictError("could not reconstruct conflict inputs")
        tree = proc.stdout.splitlines()[0].strip()
        paths = publication._parse_conflict_paths(proc.stdout)  # pyright: ignore[reportPrivateUsage]
        if not paths:
            raise publication.IntegrationConflictError("merge conflict has no bounded path list")
        marker = git(
            self.repo,
            "commit-tree",
            tree,
            "-p",
            left,
            "-m",
            f"relay: conflict inputs {consumer}",
            env=ORCHESTRATOR_ENV,
        ).strip()  # pyright: ignore[reportPrivateUsage]
        from .snapshots import retain
        from .specs.task import Budget, ScopeCheck

        retain(self.repo, f"refs/relay/{self.store.run_id}/snapshots@{key}/input", marker)
        task = TaskSpec(
            run_id=self.store.run_id,
            id=key,
            category="implement",
            access="write",
            goal=(
                f"Resolve only the merge conflicts on {paths}. "
                f"Exact parent commits: {left}, {right}. Preserve all cleanly merged changes."
            ),
            owns=paths,
            acceptance=[
                entry for entry in self.graph.integration_acceptance if isinstance(entry, RunCheck)
            ]
            + [ScopeCheck(diff_scope=paths)],
            commands=self.graph.commands,
            verify=self.graph.verify,
            verification_skip_reason=self.graph.verification_skip_reason,
            budget=Budget(max_attempts=1, timeout_s=self.config.defaults.timeout_s),
        )
        task.repo.input_commit = marker
        facts = _classification(task, self.config)
        decision = route_agent(self.config, facts, available=self.available, levels=self.levels)
        agent = decision.agent
        if decision.rejected or agent is None:
            raise publication.IntegrationConflictError("no eligible conflict-resolution worker")
        agent_config = self.config.agents[agent]
        permission = decide(
            agent,
            self.levels[agent],
            resolve_level(
                agent=agent_config.permissions,
                default=self.config.defaults.permissions,
            ),
        )
        if not permission.supported:
            raise publication.IntegrationConflictError(
                permission.reason or "unsupported resolver permissions"
            )
        directory = self.store.add_node(key)
        model = select_model(agent_config, facts)
        task.routing.agent, task.routing.model, task.routing.effort = (
            agent,
            model.model,
            model.effort,
        )
        task.permissions = permission.effective
        resolver_key = self.key(task, marker, self.graph.model_dump_json(), self.fingerprint(task))
        self.store.update_run(lambda r: setattr(r.nodes[key], "input_key", resolver_key))
        self.store.write_spec(f"{directory.name}/task.spec.yaml", task)
        self.routes.route(
            node_id=key,
            attempt=1,
            event="routed",
            category=task.category,
            complexity=facts.complexity,
            agent_decision=decision,
            model_decision=model,
            goal=task.goal,
        )
        self.store.update_run(lambda r: setattr(r.nodes[key], "attempts", 1))
        worktree = self.manager.create(key, marker)
        runner = _AttemptRunner(
            self.repo,
            self.store,
            self.events,
            task,
            key,
            directory,
            self.manager,
            worktree.path,
            marker,
            resolver_key,
            "write",
            permission.effective,
            self.config.defaults.timeout_s,
            self.adapters[agent],
            agent,
            graph_mode=True,
            progress=self.progress,
        )
        with self.agent_slots[agent]:
            outcome = asyncio.run(runner.run(1, None if model.model == "default" else model.model))
        if (
            isinstance(outcome, _AttemptFailure)
            or outcome.exit_code != 0
            or not outcome.output_commit
        ):
            self.store.update_run(lambda r: setattr(r.nodes[key], "state", "failed"))
            raise publication.IntegrationConflictError("bounded conflict resolution failed")
        tree = git(self.repo, "rev-parse", f"{outcome.output_commit}^{{tree}}").strip()  # pyright: ignore[reportPrivateUsage]
        merged = git(
            self.repo,
            "commit-tree",
            tree,
            "-p",
            left,
            "-p",
            right,
            "-m",
            f"relay: resolve {consumer}",
            env=ORCHESTRATOR_ENV,
        ).strip()  # pyright: ignore[reportPrivateUsage]
        retain(self.repo, f"refs/relay/{self.store.run_id}/snapshots@{key}/output", merged)
        self.store.write_text(f"{directory.name}/output.hash", _output_hash(self.store, key))
        self.events.append(
            "merge.result", {"node_id": key, "parents": [left, right], "commit": merged}
        )
        self.manager.decommission(key)
        return merged


async def run_graph(
    repo: Path,
    graph: Graph,
    *,
    config: Config,
    adapters: Mapping[str, AgentAdapter] | None = None,
    run_id: str | None = None,
    resume_run: str | None = None,
    max_attempts: int | None = None,
    concurrency: int | None = None,
    allow_dirty: bool = False,
    store: RunStore | None = None,
    session_owner: SessionOwner | None = None,
    control: SchedulerControl | None = None,
    retained_base: RetainedBase | None = None,
    progress: ProgressChannel | None = None,
) -> GraphResult:
    """Coordinate one foreground graph, with no detached supervisor or IPC."""
    if concurrency is not None and concurrency < 1:
        raise ValueError("concurrency must be positive")
    graph = Graph.model_validate(graph.model_dump(mode="json", by_alias=True, exclude_unset=True))
    for node in graph.nodes:
        effective = task_for(node, graph, "preflight")
        node.verify = effective.verify
        node.verification_skip_reason = effective.verification_skip_reason
        node.commands = effective.commands
    registry = _default_adapters(config)
    registry.update(adapters or {})
    config_text, config_hash = snapshot(config)
    # Reject missing verification before creating runtime state or calling a model.
    for node in graph.nodes:
        task = task_for(node, graph, "preflight")
        if task.access == "write" and task.verify == "default" and not executable_commands(task):
            return GraphResult(
                2, None, "failed", {}, error=f"node {node.id} has no executable checks"
            )
    if any(node.access == "write" for node in graph.nodes) and graph.verify == "default":
        check = TaskSpec(
            run_id="preflight",
            id="check",
            category="test",
            goal="Verify",
            commands=graph.commands,
            acceptance=graph.integration_acceptance,
        )
        if not executable_commands(check):
            return GraphResult(
                2, None, "failed", {}, error="graph has no integration acceptance or commands"
            )
    available, levels = await _probe(config, registry)
    versions = {name: (await adapter.probe()).version for name, adapter in registry.items()}
    if store is None:
        store = (
            RunStore(repo, resume_run)
            if resume_run
            else RunStore.create(repo, config_hash=config_hash, run_id=run_id)
        )
    lock = RunLock.acquire(
        repo, store.run_id, takeover=resume_run is not None, session_owner=session_owner
    )
    events = EventLog(store.events_path)
    coordinator = Coordinator(
        repo, graph, config, store, events, registry, available, levels, versions
    )
    coordinator.max_attempts = max_attempts
    coordinator.progress = progress
    monitor: asyncio.Task[None] | None = None
    results: dict[str, Outcome] = {}
    try:
        prior_integration = read_ref(repo, publication.integration_branch(store.run_id))
        if resume_run:
            record = store.read_run()
            for key, node in record.nodes.items():
                if (
                    record.state in {"running", "interrupted"}
                    or node.recovery_pending
                    or node.worker_pending
                    or node.worker is not None
                    or node.blocked_reason == "worker_unconfirmed"
                ) and not prepare_interrupted(store, key):
                    return GraphResult(
                        6,
                        store.run_id,
                        "blocked",
                        {},
                        error="old worker termination unconfirmed",
                    )
            # Scratch belongs to the old attempt, not its editable checkout. Wait until
            # every recorded worker is gone before removing any shared verification tree.
            for key in record.nodes:
                coordinator.manager.decommission_candidate(store.path(f"{key}/candidate"))
            coordinator.manager.decommission_candidate(store.path("merge-candidate"))
            coordinator.manager.decommission("graph-verification")
            started = next(
                event for event in read_events(store.events_path) if event.type == "run.started"
            )
            coordinator.base = str(started.data["base_commit"])
            for request in store.path("cancel-requests").glob("*.json"):
                request.unlink()
        else:
            store.write_text("graph.marker", "foreground\n")
            ensure_exclude_block(repo)
            if retained_base is not None:
                # An explicit, pinned base (checkpoint or unapplied integration): never HEAD.
                assert retained_base.commit is not None
                write_ref(repo, base_ref(store.run_id), retained_base.commit)
                store.update_run(lambda r: setattr(r, "base", retained_base))
                coordinator.base = retained_base.commit
            else:
                base = resolve_base(repo, store.run_id, allow_dirty=allow_dirty)
                coordinator.base = base.commit
            project = repo / "PROJECT.spec.md"
            store.write_text("PROJECT.spec.md", read_text(project) if project.exists() else "")
            events.append(
                "run.started",
                {
                    "config_hash": config_hash,
                    "base_commit": coordinator.base,
                    "allow_dirty": allow_dirty and retained_base is None,
                    "run_id": store.run_id,
                },
            )
        for node in graph.nodes:
            store.add_node(node.id)
            if max_attempts is not None:
                if max_attempts < (node.budget.max_attempts or 1):
                    raise ValueError("--max-attempts must not lower the total limit")
                if store.read_run().nodes[node.id].state != "done":
                    node.budget.max_attempts = max_attempts
                    coordinator.tasks[node.id].budget.max_attempts = max_attempts
        store.write_text("config.snapshot.json", config_text)
        store.update_run(lambda r: setattr(r, "graph", graph))
        store.update_run(lambda r: setattr(r, "config_hash", config_hash))
        store.update_run(lambda r: setattr(r, "state", "running"))

        async def watch_cancel() -> None:
            while True:
                for request in store.path("cancel-requests").glob("*.json"):
                    data = json.loads(read_text(request))
                    if data.get("node"):
                        coordinator.cancelled_nodes.add(str(data["node"]))
                    else:
                        coordinator.cancel.set()
                await asyncio.sleep(0.1)

        monitor = asyncio.create_task(watch_cancel())
        if control is not None:
            control.bind(store, events, coordinator.cancel, coordinator.cancelled_nodes)
        results = await _drain_on_cancel(
            schedule(
                graph,
                concurrency=concurrency or config.defaults.concurrency,
                capacities={
                    k: a.concurrency or config.defaults.concurrency
                    for k, a in config.agents.items()
                },
                choose=coordinator.choose,
                execute=coordinator.execute,
                transition=coordinator.transition,
                overlaps=writers_overlap,
                cancellation=coordinator.cancel,
                cancelled_nodes=coordinator.cancelled_nodes,
                control=control,
            ),
            coordinator.cancel,
        )
        codes = {outcome.exit_code for outcome in results.values()}
        code = next((c for c in (4, 5, 1, 2, 3, 6, 7) if c in codes), 0)
        commit = None
        if code == 0:
            record = store.read_run()
            outputs = [(key, str(record.nodes[key].output_commit)) for key in sorted(results)]

            def finish() -> str:
                with (
                    cancel_processes_when(coordinator.cancel),
                    observe_processes(partial(coordinator.observe, graph.nodes[0].id)),
                ):
                    merged = merge_snapshot(
                        repo,
                        store.run_id,
                        "final",
                        coordinator.base,
                        outputs,
                        verify=coordinator.verify_snapshot,
                        resolve=coordinator.resolve_conflict,
                    )
                    coordinator.verify_snapshot(merged)
                    if coordinator.cancel.is_set():
                        raise ProcessCancelled("cancelled during integration")
                    return merged

            commit = await _run_owned_thread(finish, coordinator.cancel)
            branch = publication.integration_branch(store.run_id)
            if not compare_and_swap(repo, branch, commit, prior_integration or "0" * len(commit)):  # pyright: ignore[reportPrivateUsage]
                raise publication.PublicationError("integration branch changed concurrently")
            store.update_run(lambda r: setattr(r, "final_commit", commit))
            events.append(
                "integration.published",
                {"commit": commit, "branch": branch, "nodes": sorted(results)},
            )
        state = (
            "done"
            if code == 0
            else "cancelled"
            if code == 4
            else "blocked"
            if code == 6
            else "failed"
        )
        store.update_run(lambda r: setattr(r, "state", state))
        events.append("run.finished", {"state": state, "integration_commit": commit})
        return GraphResult(code, store.run_id, state, results, commit)
    except asyncio.CancelledError:
        coordinator.cancel.set()
        store.update_run(lambda r: setattr(r, "state", "interrupted"))
        events.append("run.finished", {"state": "interrupted"})
        raise
    except Exception as exc:
        code = (
            4
            if isinstance(exc, ProcessCancelled)
            else 5
            if isinstance(exc, VerificationError)
            else 2
            if isinstance(
                exc,
                (
                    ValueError,
                    DirtyTreeError,
                    EmptyRepositoryError,
                    InvalidRefError,
                    NotARepositoryError,
                ),
            )
            else 1
        )
        state = "cancelled" if code == 4 else "failed"
        store.update_run(lambda r: setattr(r, "state", state))
        events.append("run.finished", {"state": state})
        return GraphResult(code, store.run_id, state, results, error=str(exc))
    finally:
        if monitor:
            monitor.cancel()
        coordinator.routes.close()
        events.close()
        lock.release()
