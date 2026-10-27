"""In-process application service for the conversational front-end (design 8.7, 14.1, 15).

`SessionApp` composes the session owner, the planning engine, the session controller and the
shared delivery service into the command surface a front-end submits to, and `view()` returns
a read-only snapshot for projections. It decides nothing a model could: the coordinator only
proposes, approval is an explicit operator call on an exact revision, and execution stays with
the controller and the existing scheduler.

`ServiceHost` runs the service on a dedicated event-loop thread, so Relay execution is owned by
this service and its explicit shutdown path, never by a UI widget or a Textual worker (whose
lifetime follows its owning node). Cancelling an awaiting caller never cancels an operation.

Nothing here imports Textual.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import secrets
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeVar

from relay.adapters.base import AgentAdapter

from .answers import current_questions, read_answers
from .apply import (
    ApplyError,
    DirtyOverlapError,
    MergeConflictError,
    changed_paths,
    detached_checkout,
)
from .capture import CaptureResult, Change, ChangeCounts
from .checkpoints import is_deliverable, load_checkpoint
from .controller import SessionController
from .coordinator import (
    QUALIFICATIONS,
    CoordinationLimits,
    CoordinatorProfile,
    Qualification,
    qualification_for,
    select_coordinator,
)
from .delivery import (
    DeliveryBlocked,
    PreparedDelivery,
    Verdict,
    install_delivery,
    integration_task,
    prepare_checkpoint_delivery,
    prepare_run_delivery,
    verify_delivery,
)
from .events import EventLog, read_events
from .graph import _classification, task_for  # pyright: ignore[reportPrivateUsage]
from .model_router import select_model
from .permissions import resolve_level
from .planning import (
    Coordinate,
    PlanningEngine,
    PlanningSetup,
    TurnResult,
    proposer_record,
    recover_turns,
)
from .planning import validate_plan as _validate_plan
from .progress import ProgressSubscription
from .publish import diff_artifact_path
from .registry import Config
from .router import route_agent
from .run import _default_adapters, _probe  # pyright: ignore[reportPrivateUsage]
from .sessions import (
    DELETING,
    SessionBusy,
    SessionConflict,
    SessionDeletion,
    SessionDeletionPlan,
    SessionError,
    SessionOwner,
    SessionOwnershipUnavailable,
    SessionStore,
    delete_session,
    plan_session_deletion,
)
from .specs import Graph, ResultSpec, load_spec
from .specs.common import PermissionLevel
from .specs.task import RunCheck, ScopeCheck
from .store import RunStore, StoreError, read_text
from .verify import verify

__all__ = [
    "CoordinationUnavailable",
    "CoordinatorBackend",
    "CoordinatorOption",
    "DeliveryReview",
    "NodeDetail",
    "NodeView",
    "PlanView",
    "QuestionSetView",
    "ServiceHost",
    "SessionApp",
    "SessionSummary",
    "SessionView",
    "default_coordinators",
]

T = TypeVar("T")

_TERMINAL = frozenset({"done", "failed", "cancelled"})
_TEXT_LIMIT = 6000
_DIFF_LIMIT = 65536
_CONTAINS_TTL_S = 2.0
_GIT_TIMEOUT_S = 30.0


class CoordinationUnavailable(SessionError):
    """No qualified coordinator is selected; saved sessions can still be inspected."""


def _token() -> str:
    return secrets.token_hex(6)


def _bounded(text: str, limit: int = _TEXT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters retained on disk)"


# --- host -----------------------------------------------------------------------------------------


class ServiceHost:
    """A dedicated event-loop thread that owns Relay execution for an interactive front-end."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._main, name="relay-service", daemon=False)
        self._started = False
        self._closed = False

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def start(self) -> ServiceHost:
        if not self._started:
            self._started = True
            self._thread.start()
        return self

    @property
    def closed(self) -> bool:
        return self._closed

    def submit(self, coro: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        if self._closed or not self._started:
            coro.close()
            raise RuntimeError("the service host is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def call(self, coro: Coroutine[Any, Any, T]) -> T:
        """Await a service operation from another loop; cancelling the caller never cancels it."""
        return await asyncio.shield(asyncio.wrap_future(self.submit(coro)))

    def close(
        self,
        shutdown: Callable[[], Coroutine[Any, Any, None]] | None = None,
        *,
        still_running: Callable[[], bool] | None = None,
    ) -> None:
        """Run `shutdown` on the service loop, wait for it (through repeated Ctrl-C), then stop.

        Ownership is released only by `shutdown` itself, after real drainage, so a second
        interrupt while waiting cannot release it early. If `still_running` reports work after
        `shutdown` (for instance because shutdown failed), the loop keeps running until it ends:
        stopping the loop under a run that can still publish would abandon it.
        """
        if self._closed or not self._started:
            self._closed = True
            return
        error: BaseException | None = None
        if shutdown is not None:
            future = self.submit(shutdown())
            while True:
                try:
                    future.result()
                    break
                except KeyboardInterrupt:
                    continue
                except BaseException as exc:  # noqa: BLE001 - re-raised once the thread is gone
                    error = exc
                    break
        while still_running is not None and still_running():
            with contextlib.suppress(KeyboardInterrupt):
                time.sleep(0.1)
        with contextlib.suppress(BaseException):
            asyncio.run_coroutine_threadsafe(_settle_tasks(), self._loop).result(15)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        while self._thread.is_alive():
            with contextlib.suppress(KeyboardInterrupt):
                self._thread.join()
        if error is not None:
            raise error


async def _settle_tasks() -> None:
    """After shutdown only projections can remain (execution has drained): let them finish."""
    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current]
    if pending:
        _, late = await asyncio.wait(pending, timeout=10)
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)


# --- coordinators ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class CoordinatorBackend:
    """A CLI that could coordinate: its probed profile and `coordinate` capability, if any."""

    agent: str
    profile: CoordinatorProfile | None
    coordinate: Coordinate | None
    note: str = ""


@dataclass(frozen=True)
class CoordinatorOption:
    agent: str
    available: bool
    mode: str | None
    reason: str
    model: str | None
    selected: bool


async def default_coordinators(config: Config) -> dict[str, CoordinatorBackend]:
    """Probe the installed CLIs (`--version` only, never a model call) for coordination."""
    from relay.adapters.claude import ClaudeAdapter

    found: dict[str, CoordinatorBackend] = {}
    if "claude" in config.agents:
        adapter = ClaudeAdapter(config.agents["claude"])
        probe = await adapter.probe()
        if probe.available:
            found["claude"] = CoordinatorBackend(
                "claude", adapter.coordinator_profile(probe.version), adapter.coordinate
            )
        else:
            found["claude"] = CoordinatorBackend("claude", None, None, "claude is not installed")
    if "codex" in config.agents:
        found["codex"] = CoordinatorBackend(
            "codex",
            None,
            None,
            "Codex has no qualifying restricted profile (spike 2.31: `-s read-only` reads "
            "outside the workspace), so it cannot coordinate",
        )
    return found


# --- views ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    created_at: str
    state: str
    plan_revision: int
    rounds: int
    checkpoints: int


@dataclass(frozen=True)
class MessageView:
    message_id: str
    author: str
    provenance: str
    content: str
    ts: str


@dataclass(frozen=True)
class QuestionView:
    id: str
    text: str
    kind: str
    options: tuple[str, ...]
    required: bool
    default: str | None


@dataclass(frozen=True)
class QuestionSetView:
    source: Literal["coordinator", "node"]
    revision: int
    node_id: str | None
    state: Literal["open", "answered", "stale"]
    questions: tuple[QuestionView, ...]
    note: str = ""
    retained: tuple[str, ...] = ()
    """Answers already recorded for this set (shown when a limit stops their use)."""


@dataclass(frozen=True)
class NodeView:
    node_id: str
    state: str
    blocked_reason: str | None
    depends_on: tuple[str, ...]
    access: str
    owns: tuple[str, ...]
    agent: str | None
    model: str | None
    attempts: int
    max_attempts: int
    output_commit: str | None
    activity: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoundView:
    run_id: str
    plan_revision: int
    round_state: str
    run_state: str
    interrupted_reason: str | None
    checkpoint_id: str | None
    final_commit: str | None


@dataclass(frozen=True)
class TurnView:
    turn_id: str
    status: str
    steps_used: int
    error: str | None


@dataclass(frozen=True)
class PlanNodeView:
    node_id: str
    category: str
    goal: str
    depends_on: tuple[str, ...]
    access: str
    owns: tuple[str, ...]
    permissions: str
    candidates: tuple[str, ...]
    model: str | None
    checks: tuple[str, ...]
    limits: str
    context: tuple[str, ...]


@dataclass(frozen=True)
class PlanView:
    revision: int
    parent: int | None
    digest: str
    explanation: str
    change_summary: str | None
    base_label: str
    nodes: tuple[PlanNodeView, ...]
    integration_checks: tuple[str, ...]
    policy: str
    approved: bool
    problems: tuple[str, ...]
    proposer: str | None


@dataclass(frozen=True)
class BaseView:
    kind: str  # head | checkpoint | integration
    label: str
    commit: str | None
    primary_unchanged: bool


@dataclass(frozen=True)
class SessionView:
    session_id: str | None
    owned: bool
    readonly_reason: str | None
    session_state: str
    admission: str
    session_revision: int
    plan_revision: int
    coordinating: bool
    checkpointing: bool
    stopping: bool
    round_running: bool
    run_id: str | None
    messages: tuple[MessageView, ...]
    question_sets: tuple[QuestionSetView, ...]
    nodes: tuple[NodeView, ...]
    rounds: tuple[RoundView, ...]
    turns: tuple[TurnView, ...]
    pending_plan: PlanView | None
    recovery: tuple[str, ...]
    coordinators: tuple[CoordinatorOption, ...]
    coordinator: CoordinatorOption | None
    base: BaseView
    deliverable: str | None
    notices: tuple[tuple[int, str], ...]
    """Numbered service notices (checkpoint summaries, finished rounds, errors)."""
    stamp: str = ""
    """Changes whenever anything shown changes; projections can skip identical views."""
    applying: bool = False
    """A confirmed delivery is being installed; quitting waits for it."""


@dataclass(frozen=True)
class NodeDetail:
    node_id: str
    run_id: str
    handoff: str
    sources: tuple[str, ...]
    changed_files: tuple[str, ...]
    diff: str
    verification: str
    logs: str


@dataclass(frozen=True)
class DeliveryReview:
    kind: str
    ref: str
    target: str | None
    target_tip: str | None
    prepared_commit: str | None
    digest: str | None
    changed_paths: tuple[str, ...]
    cumulative_paths: tuple[str, ...]
    diff: str
    checks: tuple[str, ...]
    outcome: str
    outcome_detail: str
    overlaps: tuple[str, ...]
    problems: tuple[str, ...]
    noop: bool
    primary_unchanged: bool

    @property
    def applicable(self) -> bool:
        return self.digest is not None and not self.problems and self.outcome != "failed"


@dataclass
class _Flags:
    coordinating: bool = False
    checkpointing: bool = False
    stopping: bool = False
    starting: bool = False
    finishing: bool = False
    finished: set[str] = field(default_factory=lambda: set[str]())


# --- the service ----------------------------------------------------------------------------------


class SessionApp:
    """The front-end's application service; every method runs on the service loop."""

    def __init__(
        self,
        repo_root: Path,
        *,
        config: Config,
        adapters: Mapping[str, AgentAdapter] | None = None,
        coordinators: Mapping[str, CoordinatorBackend] | None = None,
        records: tuple[Qualification, ...] = QUALIFICATIONS,
        limits: CoordinationLimits | None = None,
        concurrency: int | None = None,
        poll_s: float = 0.1,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self._adapters = adapters
        self._backends: dict[str, CoordinatorBackend] | None = (
            dict(coordinators) if coordinators is not None else None
        )
        self._records = records
        self._limits = limits or CoordinationLimits()
        self._concurrency = concurrency
        self._poll_s = poll_s
        self._choice: str | None = None
        self._available: dict[str, bool] = {}
        self._levels: dict[str, tuple[PermissionLevel, ...]] = {}
        self._owner: SessionOwner | None = None
        self._readonly: SessionStore | None = None
        self._readonly_reason: str | None = None
        self._controller: SessionController | None = None
        self._engine: PlanningEngine | None = None
        self._turn: asyncio.Task[TurnResult] | None = None
        self._revise: asyncio.Task[Any] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._progress: ProgressSubscription | None = None
        self._activity: dict[str, deque[str]] = {}
        self._prepared: dict[str, tuple[PreparedDelivery, Verdict]] = {}
        self._notices: deque[tuple[int, str]] = deque(maxlen=24)
        self._notice_seq = 0
        self._flags = _Flags()
        self._plans: dict[tuple[str, int], PlanView] = {}
        self._routes: dict[str, tuple[int, dict[str, tuple[str | None, str | None]]]] = {}
        self._contains: dict[str, tuple[bool, float]] = {}
        self._releasing = asyncio.Lock()
        self._closing: asyncio.Future[None] | None = None
        self._apply: asyncio.Future[dict[str, Any]] | None = None

    # --- startup and coordinators -----------------------------------------------------------

    async def start(self) -> None:
        """Probe agents and coordinators once (`--version` probes only; never a model call)."""
        registry = _default_adapters(self.config)
        registry.update(self._adapters or {})
        self._available, self._levels = await _probe(self.config, registry)
        if self._backends is None:
            self._backends = await default_coordinators(self.config)
        profiles = {k: b.profile for k, b in self._backends.items() if b.profile is not None}
        chosen = select_coordinator(profiles, records=self._records)
        self._choice = chosen.agent

    def coordinator_options(self) -> tuple[CoordinatorOption, ...]:
        options: list[CoordinatorOption] = []
        for name in sorted(self._backends or {}):
            backend = (self._backends or {})[name]
            model = self._coordinator_model(name)
            if backend.profile is None or backend.coordinate is None:
                options.append(
                    CoordinatorOption(
                        name, False, None, backend.note or "no coordinate capability", model, False
                    )
                )
                continue
            selection = select_coordinator(
                {name: backend.profile}, requested=name, records=self._records
            )
            available = selection.available and model is not None
            reason = selection.reason
            if selection.available and model is None:
                reason = f"the balanced model tier does not resolve for {name}"
            options.append(
                CoordinatorOption(
                    name, available, selection.mode, reason, model, name == self._choice
                )
            )
        return tuple(options)

    def _coordinator_model(self, agent: str) -> str | None:
        entry = self.config.agents.get(agent)
        return entry.models.get("balanced") if entry is not None else None

    def _qualification(self) -> Qualification | None:
        backend = (self._backends or {}).get(self._choice or "")
        if backend is None or backend.profile is None:
            return None
        record = qualification_for(backend.profile, self._records)
        return record if record is not None and record.status == "qualified" else None

    async def select_coordinator(self, agent: str) -> CoordinatorOption:
        """Choose the coordinator; only qualified modes can be chosen (section 7.4)."""
        options = {o.agent: o for o in self.coordinator_options()}
        option = options.get(agent)
        if option is None:
            raise CoordinationUnavailable(f"coordinator {agent!r} is not installed or enabled")
        if not option.available:
            raise CoordinationUnavailable(f"{agent} cannot coordinate: {option.reason}")
        self._choice = agent
        self._build_engine()
        return {o.agent: o for o in self.coordinator_options()}[agent]

    def _build_engine(self) -> None:
        self._engine = None
        owner, record = self._owner, self._qualification()
        backend = (self._backends or {}).get(self._choice or "")
        if owner is None or record is None or backend is None or backend.coordinate is None:
            return
        base = self._next_base(owner.store)
        self._engine = PlanningEngine(
            owner,
            PlanningSetup(
                coordinate=backend.coordinate,
                qualification=record,
                config=self.config,
                base_commit=base.commit or self._head(),
                available=self._available,
                levels=self._levels,
                cwd=self.repo_root,
                model=self._coordinator_model(backend.agent),
                limits=self._limits,
            ),
        )

    def _require_engine(self) -> PlanningEngine:
        if self._engine is None:
            reasons = "; ".join(f"{o.agent}: {o.reason}" for o in self.coordinator_options())
            raise CoordinationUnavailable(
                "coordination is unavailable"
                + (f" ({reasons})" if reasons else "")
                + ". Saved sessions can still be inspected and headless commands still work."
            )
        if self._owner is None:
            raise SessionError(self._readonly_reason or "no session is open")
        return self._engine

    # --- sessions ---------------------------------------------------------------------------

    async def sessions(self) -> list[SessionSummary]:
        return await asyncio.to_thread(self._summaries)

    def _summaries(self) -> list[SessionSummary]:
        found: list[SessionSummary] = []
        for session_id in SessionStore.list_sessions(self.repo_root):
            with contextlib.suppress(SessionError, OSError, ValueError):
                store = SessionStore(self.repo_root, session_id)
                record = store.read()
                state = "deleting" if (store.dir / DELETING).exists() else record.state
                found.append(
                    SessionSummary(
                        session_id,
                        record.created_at,
                        state,
                        record.plan_revision,
                        len(record.rounds),
                        len(record.checkpoints),
                    )
                )
        return sorted(found, key=lambda s: (s.created_at, s.session_id), reverse=True)

    @property
    def session_id(self) -> str | None:
        if self._owner is not None:
            return self._owner.store.session_id
        return self._readonly.session_id if self._readonly is not None else None

    @property
    def executing(self) -> bool:
        return self._controller is not None and self._controller.running

    def _busy(self) -> str | None:
        """What would be cut short by releasing the session now, if anything."""
        if self.executing or self._flags.checkpointing or self._flags.starting:
            return "a round is executing"
        if self._turn is not None and not self._turn.done():
            return "the coordinator is working on a turn"
        if self._apply is not None and not self._apply.done():
            return "a delivery is being applied"
        return None

    def _require_open(self) -> None:
        if self._closing is not None:
            raise SessionError("the application is closing")

    async def open(self, session_id: str | None = None) -> str:
        """Open (or create) a session as its owner; never dispatches or repeats a call.

        A session owned by another process, or a filesystem without native locking, opens
        read-only with the reason shown. Switching away while a round executes, a coordinator
        turn runs or a delivery is applied is refused: stop it first.
        """
        self._require_open()
        busy = self._busy()
        if busy is not None:
            raise SessionConflict(f"{busy}; stop it before switching sessions")
        async with self._releasing:
            await self._release_locked()
            self._require_open()
            if session_id is None:
                store = await asyncio.to_thread(
                    SessionStore.create, self.repo_root, coordinator=self._coordinator_record()
                )
            else:
                store = SessionStore(self.repo_root, session_id)
                if not await asyncio.to_thread((store.dir / "session.json").is_file):
                    raise SessionError(f"session {session_id} not found")
            await self._acquire(store)
        return store.session_id

    def _coordinator_record(self) -> dict[str, Any]:
        record = self._qualification()
        if record is None:
            return {}
        return {
            "agent": record.profile.agent,
            "mode": record.mode,
            "cli_version": record.profile.cli_version,
            "config_digest": record.profile.config_digest,
            "model": self._coordinator_model(record.profile.agent),
        }

    async def _acquire(self, store: SessionStore) -> None:
        try:
            owner = await asyncio.to_thread(store.acquire_owner)
        except (SessionBusy, SessionOwnershipUnavailable, SessionError) as exc:
            self._readonly, self._readonly_reason = store, f"read-only: {exc}"
            return
        self._readonly, self._readonly_reason = None, None
        self._owner = owner
        recovered = await asyncio.to_thread(recover_turns, owner)
        for turn_id, state in recovered.items():
            self._notice(f"turn {turn_id} was {state} when the session was reopened")
        controller = SessionController(
            owner, config=self.config, adapters=self._adapters, concurrency=self._concurrency
        )
        self._controller = controller
        self._progress = controller.progress.subscribe()
        self._activity = {}
        self._flags.finished = set()
        self._build_engine()
        self._monitor = asyncio.create_task(self._watch())

    async def _release(self) -> None:
        """Stop the monitor, shut the controller down (drains, then releases ownership).

        Serialized: a second caller waits for the first drain instead of returning early.
        """
        async with self._releasing:
            await self._release_locked()

    async def _release_locked(self) -> None:
        # Nothing that can still write the session may outlive its ownership.
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()  # the engine drains the coordinator process first
            await asyncio.gather(self._turn, return_exceptions=True)
        if self._apply is not None and not self._apply.done():
            await asyncio.gather(self._apply, return_exceptions=True)  # a thread: let it end
        if self._monitor is not None:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
            self._monitor = None
        if self._controller is not None:
            await self._controller.shutdown()
        elif self._owner is not None:
            await asyncio.to_thread(self._owner.close)
        self._controller = self._engine = self._owner = None
        self._readonly, self._readonly_reason = None, None
        self._prepared.clear()

    async def stop(self) -> None:
        """Stop execution (section 8.7): close admission, cancel, drain, persist, then reopen.

        Shows `stopping` until drainage finishes. The session is reacquired afterwards, under
        the same release lock, so it stays inspectable and can be continued explicitly; nothing
        dispatches by itself. Once the application is closing, it is not reacquired.
        """
        session_id = self.session_id
        self._flags.stopping = True
        try:
            await self._cancel_tasks()
            async with self._releasing:
                await self._release_locked()
                if session_id is not None and self._closing is None:
                    await self._acquire(SessionStore(self.repo_root, session_id))
        finally:
            self._flags.stopping = False

    async def shutdown(self) -> None:
        """Closing the application: the same drainage as stop, then ownership is released.

        Every caller awaits one shared drain, so a repeated or concurrent shutdown (the UI's
        quit and the host's final close) can never return while execution still drains.
        """
        if self._closing is None:
            self._closing = asyncio.ensure_future(self._shutdown())
        await asyncio.shield(self._closing)

    async def _shutdown(self) -> None:
        self._flags.stopping = True
        try:
            try:
                await self._cancel_tasks()
            finally:
                await self._release()  # always: a failed cancellation still drains and releases
        finally:
            self._flags.stopping = False

    async def _cancel_tasks(self) -> None:
        if self._apply is not None and not self._apply.done():
            # A delivery runs in a thread and cannot be interrupted safely: wait for it.
            await asyncio.gather(self._apply, return_exceptions=True)
        if self._revise is not None and not self._revise.done() and self._controller is not None:
            # A checkpoint waiting for active work: cancel that work, let the seal finish.
            snapshot = await asyncio.to_thread(self._controller.snapshot)
            for node_id, node in snapshot["nodes"].items():
                if node["state"] == "running":
                    with contextlib.suppress(SessionError):
                        await self._controller.cancel_node(f"stop-{_token()}", node_id)
            await asyncio.gather(self._revise, return_exceptions=True)
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()  # the engine drains the coordinator process first
            await asyncio.gather(self._turn, return_exceptions=True)

    async def deletion_plan(self, session_id: str) -> SessionDeletionPlan:
        """Exactly what deleting `session_id` would remove, for the confirmation (read-only)."""
        return await asyncio.to_thread(
            plan_session_deletion,
            self.repo_root,
            session_id,
            worktree_root=self.config.paths.worktree_root,
        )

    async def delete(
        self, session_id: str, *, include_undelivered: bool = False
    ) -> SessionDeletion:
        """Confirmed deletion of a session and its runs (section 6).

        The caller has shown the operator the deletion plan; `include_undelivered` records an
        explicit confirmation that undelivered rounds are deleted too.
        """
        self._require_open()
        if session_id == self.session_id:
            busy = self._busy()
            if busy is not None:
                raise SessionConflict(f"{busy}; stop it before deleting this session")
            await self._release()
        return await asyncio.to_thread(
            delete_session,
            self.repo_root,
            session_id,
            include_undelivered=include_undelivered,
            worktree_root=self.config.paths.worktree_root,
        )

    # --- turns ------------------------------------------------------------------------------

    async def _run_turn(self, coro: Coroutine[Any, Any, TurnResult]) -> TurnResult:
        if self._turn is not None and not self._turn.done():
            coro.close()
            raise SessionConflict("the coordinator is already working on a turn")
        self._flags.coordinating = True
        try:
            self._turn = asyncio.create_task(coro)
            result = await self._turn
        finally:
            self._flags.coordinating = False
        if result.status == "failed" and result.error:
            self._notice(f"coordinator turn {result.turn_id} failed: {result.error}")
        return result

    def _prepare_engine(self) -> PlanningEngine:
        engine = self._require_engine()
        base = self._next_base(engine.owner.store)
        engine.plan_base = None if base.kind == "head" else self._base_spec(engine.owner.store)
        engine.setup.base_commit = base.commit or self._head()
        return engine

    async def submit(self, text: str) -> TurnResult:
        """A new operator request (no round executing): one bounded coordination turn."""
        if not text.strip():
            raise SessionError("type a request first")
        if self._owner is None and self._readonly is None and self._qualification() is None:
            self._require_engine()  # explains why coordination is unavailable
        if self.executing:
            raise SessionConflict("a round is executing; steering uses Checkpoint and revise")
        engine = self._prepare_engine()
        return await self._run_turn(engine.submit(text))

    async def answer_coordinator(
        self, revision: int, answers: dict[str, str | list[str]]
    ) -> TurnResult:
        engine = self._require_engine()
        if self.executing:
            raise SessionConflict("a round is executing; answer the coordinator after it settles")
        sets = {s.revision: s for s in self._question_sets(engine.owner.store)}
        current = sets.get(revision)
        if current is None or current.state != "open":
            state = current.state if current is not None else "unknown"
            raise SessionConflict(f"question set {revision} is {state}; it cannot be answered")
        self._prepare_engine()
        return await self._run_turn(engine.answer(revision, answers))

    async def resume_turn(self, turn_id: str) -> TurnResult:
        if self.executing:
            raise SessionConflict("a round is executing; resume the turn after it settles")
        engine = self._prepare_engine()
        return await self._run_turn(engine.resume_turn(turn_id))

    async def review_round(self) -> TurnResult:
        engine = self._prepare_engine()
        if self.executing:
            raise SessionConflict("a round is executing; review it after it settles")
        rounds = engine.owner.record.rounds
        if not rounds:
            raise SessionConflict("no round to review")
        store = await asyncio.to_thread(_reader, self.repo_root, rounds[-1].run_id)
        return await self._run_turn(engine.review_round(store))

    # --- approval and rounds ----------------------------------------------------------------

    def _require_controller(self) -> tuple[SessionOwner, SessionController]:
        if self._owner is None or self._controller is None:
            raise SessionError(self._readonly_reason or "no session is open")
        return self._owner, self._controller

    async def approve(self, revision: int) -> str:
        """Approve exactly `revision` (the operator's action) and start its round."""
        owner, controller = self._require_controller()
        if self.executing:
            raise SessionConflict("a round is already executing")
        record = owner.record
        if revision != record.plan_revision:
            raise SessionConflict(
                f"plan revision {revision} is superseded; review revision {record.plan_revision}"
            )
        view = await asyncio.to_thread(self._plan_view, owner.store, revision)
        if view.problems:
            raise SessionConflict("the plan no longer validates: " + "; ".join(view.problems))
        proposer = (await asyncio.to_thread(owner.store.plan, revision)).get("proposer")
        if proposer is None:
            raise SessionConflict(
                f"plan revision {revision} records no proposing coordinator; ask for a new plan "
                "revision before approving"
            )
        current = self._qualification()
        if current is None or proposer != proposer_record(current):
            raise SessionConflict(
                "the coordinator or its configuration changed since this plan was proposed; "
                "ask for a new plan revision before approving"
            )
        self._flags.starting = True
        try:
            await asyncio.to_thread(
                owner.approve,
                revision,
                view.digest,
                idempotency_key=f"approve-{revision}-{view.digest[:16]}",
                expected_session_revision=record.revision,
            )
            run_id = await controller.start_round()
        finally:
            self._flags.starting = False
        self._flags.finished.discard(run_id)
        return run_id

    async def continue_round(self) -> str:
        """Explicitly continue the interrupted round after a stop or a crash."""
        owner, controller = self._require_controller()
        for item in reversed(owner.record.rounds):
            if item.state == "active":
                run_id = await controller.continue_round(item.run_id)
                self._flags.finished.discard(run_id)
                return run_id
        raise SessionConflict("no interrupted round to continue")

    async def restore_round(self, run_id: str) -> str:
        """Explicitly restore a superseded round (abandoning the revision)."""
        _, controller = self._require_controller()
        restored = await controller.restore_round(run_id)
        self._flags.finished.discard(restored)
        return restored

    async def pause_admission(self) -> None:
        """Close the durable gate (the first step of Checkpoint and revise)."""
        _, controller = self._require_controller()
        await controller.pause(f"pause-{_token()}")

    async def resume_admission(self) -> None:
        _, controller = self._require_controller()
        await controller.resume(f"resume-{_token()}")

    async def checkpoint_and_revise(self, text: str, *, cancel_active: bool) -> TurnResult | None:
        """Checkpoint and revise (section 8.7), then ask the coordinator for a revised plan.

        Active work finishes or is explicitly cancelled; the checkpoint becomes the base the
        revised plan is proposed against. The new plan still needs approval.
        """
        _, controller = self._require_controller()
        if not self.executing:
            raise SessionConflict("no round is executing; submit the request instead")
        run_id = controller.run_id
        assert run_id is not None
        verifier = await asyncio.to_thread(self._checkpoint_verifier, run_id)
        self._flags.checkpointing = True
        try:
            self._revise = asyncio.create_task(
                controller.revise(
                    f"revise-{_token()}", cancel_active=cancel_active, verify=verifier
                )
            )
            manifest = await self._revise
        finally:
            self._flags.checkpointing = False
        if manifest is not None:
            self._notice(
                f"checkpoint {manifest.checkpoint_id} sealed ({manifest.state}) from {run_id}; "
                "the round is superseded and admission is open for a revised plan"
            )
        if self._engine is None:
            self._build_engine()
        if self._engine is None:
            self._notice("coordination is unavailable, so no revised plan was requested")
            return None
        if not text.strip():
            return None
        engine = self._prepare_engine()
        return await self._run_turn(engine.submit(text))

    def _checkpoint_verifier(self, run_id: str) -> Callable[[str], None] | None:
        """Run the round's integration checks on the combined commit, or None if it opted out."""
        store = _reader(self.repo_root, run_id)
        graph = store.read_run().graph
        task = integration_task(graph)
        if task is None or task.verify == "none":
            return None
        repo = self.repo_root

        def check(commit: str) -> None:
            from .checkpoints import round_base

            base = round_base(store)
            paths = changed_paths(repo, base, commit)
            changes = tuple(Change(path=path, status="modified") for path in paths)
            parsed = _git(repo, "rev-parse", f"{commit}^{{tree}}")
            if parsed.returncode != 0:
                raise RuntimeError(f"cannot read the tree of {commit}: {parsed.stderr.strip()}")
            tree = parsed.stdout.strip()
            with detached_checkout(repo, commit) as worktree:
                capture = CaptureResult(
                    input_commit=base,
                    candidate_tree=tree,
                    candidate_commit=commit,
                    candidate_path=worktree,
                    changes=changes,
                    counts=ChangeCounts(modified=len(changes)),
                )
                result = verify(capture, task)
            if result.outcome != "passed":
                failed = [c.name for c in result.checks if c.status in ("failed", "timed_out")]
                raise RuntimeError("integration checks " + (", ".join(failed) or result.outcome))

        return check

    async def answer_node(
        self, node_id: str, revision: int, answers: dict[str, str | list[str]]
    ) -> str:
        """A structured answer for a waiting node while independent work continues."""
        _, controller = self._require_controller()
        if not controller.running:
            raise SessionConflict("continue the round before answering its node questions")
        from .sessions import digest_of

        command = f"answer-{node_id}-{revision}-{digest_of(answers)[:12]}"
        await controller.answer(command, node_id, revision, dict(answers))
        return command

    async def cancel_node(self, node_id: str) -> None:
        _, controller = self._require_controller()
        await controller.cancel_node(f"cancel-{node_id}-{_token()}", node_id)

    async def cancel_round(self) -> None:
        _, controller = self._require_controller()
        await controller.cancel_round(f"cancel-round-{_token()}")

    # --- background bookkeeping -------------------------------------------------------------

    async def _watch(self) -> None:
        """Finish a settled round and collect progress; owned by this service, not the UI."""
        failures = 0
        while True:
            await asyncio.sleep(self._poll_s)
            try:
                await self._watch_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a transient read must not end the watch
                failures += 1
                if failures in (1, 50):
                    self._notice(f"cannot read the round state: {exc}")

    async def _watch_once(self) -> None:
        self._drain_progress()
        controller = self._controller
        flags = self._flags
        if controller is None or controller.run_id is None:
            return
        if flags.checkpointing or flags.stopping or flags.finishing or flags.starting:
            return
        run_id = controller.run_id
        if run_id in flags.finished:
            return
        if controller.running:
            snapshot = await asyncio.to_thread(controller.snapshot)
            if not _settled(snapshot["nodes"]):
                return
        flags.finishing = True
        try:
            result = await controller.finish_round()
            flags.finished.add(run_id)
            detail = f" ({result.error})" if result.error else ""
            self._notice(f"round {run_id} finished: {result.state}{detail}")
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
        except Exception as exc:  # noqa: BLE001 - shown to the operator, never swallowed silently
            flags.finished.add(run_id)
            self._notice(f"round {run_id} ended with an error: {exc}")
        finally:
            flags.finishing = False

    def _drain_progress(self) -> None:
        if self._progress is None:
            return
        for record in self._progress.drain():
            lines = self._activity.setdefault(record.node_id, deque(maxlen=8))
            lines.append(f"[{record.kind}] {record.text.strip()[:200]}")

    def _notice(self, text: str) -> None:
        self._notice_seq += 1
        self._notices.append((self._notice_seq, text))

    # --- delivery ---------------------------------------------------------------------------

    def _deliverable(self, store: SessionStore) -> tuple[str, str] | None:
        record = store.read()
        if not record.rounds:
            return None
        last = record.rounds[-1]
        if last.state == "superseded" and last.checkpoint_id:
            with contextlib.suppress(SessionError, OSError, ValueError):
                if is_deliverable(store, last.checkpoint_id):
                    return ("checkpoint", last.checkpoint_id)
            return None
        with contextlib.suppress(StoreError, OSError, ValueError):
            run = _reader(self.repo_root, last.run_id).read_run()
            if run.state == "done" and run.final_commit:
                return ("run", last.run_id)
        return None

    async def prepare_delivery(self, target: str | None = None) -> DeliveryReview:
        """Prepare and verify delivery through the shared service; changes nothing."""
        owner, _ = self._require_controller()
        if self.executing:
            raise SessionConflict("a round is executing; deliver after it settles")
        found = await asyncio.to_thread(self._deliverable, owner.store)
        if found is None:
            raise SessionConflict("nothing is deliverable yet: finish a round first")
        return await asyncio.to_thread(self._prepare, owner.store, found[0], found[1], target)

    def _prepare(
        self, store: SessionStore, kind: str, ref: str, target: str | None
    ) -> DeliveryReview:
        def refused(problems: list[str], overlaps: tuple[str, ...] = ()) -> DeliveryReview:
            return DeliveryReview(
                kind, ref, target, None, None, None, (), (), "", (), "not_run",
                "preparation refused", overlaps, tuple(problems), False, True,
            )  # fmt: skip

        try:
            if kind == "checkpoint":
                prepared = prepare_checkpoint_delivery(
                    self.repo_root, store.session_id, ref, target=target
                )
            else:
                prepared = prepare_run_delivery(self.repo_root, ref, target=target)
        except DirtyOverlapError as exc:
            message = "uncommitted edits overlap the delivered paths; commit or stash them"
            return refused([message], tuple(exc.overlaps))
        except MergeConflictError as exc:
            return refused([f"delivery would conflict on: {', '.join(exc.conflicts)}"])
        except DeliveryBlocked as exc:
            return refused(exc.problems)
        except (ApplyError, SessionError, StoreError, OSError, ValueError) as exc:
            return refused([str(exc)])
        verdict = verify_delivery(self.repo_root, prepared)
        self._prepared[prepared.digest] = (prepared, verdict)
        plan = prepared.plan
        baseline = prepared.policy.baseline_commit if prepared.policy else plan.target_tip
        checks: list[str] = []
        if prepared.policy is not None:
            checks += [check.run for check in prepared.policy.run_checks]
            checks += ["diff_scope " + " ".join(s) for s in prepared.policy.scope_checks]
            checks += [f"verify: none in {s.run_id} ({s.reason})" for s in prepared.policy.skips]
        elif prepared.task is not None:
            checks += [_acceptance_text(a) for a in prepared.task.acceptance]
        return DeliveryReview(
            kind=kind,
            ref=ref,
            target=plan.target_ref,
            target_tip=plan.target_tip,
            prepared_commit=plan.prepared_commit,
            digest=prepared.digest,
            changed_paths=plan.changed_paths,
            cumulative_paths=prepared.cumulative_paths,
            diff=self._diff(baseline, plan.prepared_commit),
            checks=tuple(checks),
            outcome=verdict.outcome,
            outcome_detail=verdict.detail,
            overlaps=(),
            problems=(),
            noop=plan.noop,
            primary_unchanged=not self._contained(plan.prepared_commit),
        )

    def _diff(self, old: str, new: str) -> str:
        proc = _git(self.repo_root, "diff", "--stat", "--patch", "--no-color", old, new)
        text = proc.stdout if proc.returncode == 0 else proc.stderr
        if len(text) > _DIFF_LIMIT:
            text = text[:_DIFF_LIMIT] + f"\n... diff truncated; see git diff {old} {new}"
        return text

    async def apply_delivery(self, digest: str) -> dict[str, Any]:
        """Install the confirmed prepared operation through the shared delivery service."""
        self._require_open()
        self._require_controller()
        if self.executing:
            raise SessionConflict("a round is executing; deliver after it settles")
        if self._apply is not None and not self._apply.done():
            raise SessionConflict("a delivery is already being applied")
        found = self._prepared.get(digest)
        if found is None:
            raise SessionConflict("that prepared operation is unknown; review delivery again")
        prepared, verdict = found
        if verdict.outcome == "failed":
            raise SessionConflict(f"verification failed on the prepared tree: {verdict.detail}")
        # A service-owned operation: stop and shutdown wait for it before releasing ownership.
        self._apply = asyncio.ensure_future(asyncio.to_thread(self._install, prepared, digest))
        outcome = await asyncio.shield(self._apply)
        self._prepared.clear()
        return outcome

    def _install(self, prepared: PreparedDelivery, digest: str) -> dict[str, Any]:
        run_id = prepared.plan.run_id
        store = _reader(self.repo_root, run_id)
        op_id = _token()
        log = EventLog(store.events_path, owner_generation=store.read_run().owner_generation)

        def progress(step: str) -> None:
            log.append("apply.progress", {"op_id": op_id, "step": step})

        try:
            log.append(
                "apply.started",
                {"op_id": op_id, "target": prepared.plan.target_ref,
                 "commit": prepared.plan.prepared_commit},
            )  # fmt: skip
            try:
                outcome = install_delivery(
                    self.repo_root,
                    prepared,
                    confirmed_digest=digest,
                    on_step=progress,
                )
            except Exception as exc:
                log.append(
                    "apply.finished", {"op_id": op_id, "status": "failed", "error": str(exc)}
                )
                raise
            log.append("apply.finished", {"op_id": op_id, "status": "applied"})
        finally:
            log.close()
        self._notice(
            f"applied {outcome.result.prepared_commit[:12]} to {outcome.result.target_ref}"
            + ("" if outcome.result.changed else " (already contained)")
        )
        return {
            "target": outcome.result.target_ref,
            "commit": outcome.result.prepared_commit,
            "changed": outcome.result.changed,
            "installed": outcome.installed,
        }

    # --- inspection -------------------------------------------------------------------------

    async def node_detail(self, node_id: str) -> NodeDetail:
        store = self._store()
        if store is None:
            raise SessionError("no session is open")
        return await asyncio.to_thread(self._node_detail, store, node_id)

    def _node_detail(self, session: SessionStore, node_id: str) -> NodeDetail:
        run_id = self._current_run(session)
        if run_id is None:
            raise SessionError("no round has run yet")
        store = _reader(self.repo_root, run_id)
        directory = store.node_dir(node_id)

        def text(name: str, limit: int = _TEXT_LIMIT) -> str:
            path = directory / name
            if not path.is_file():
                return ""
            return _bounded(read_text(path), limit)

        sources: list[str] = []
        with contextlib.suppress(OSError, ValueError, StoreError):
            for event in read_events(store.events_path):
                if event.type == "node.started" and event.data.get("node_id") == node_id:
                    sources.append(f"input commit {event.data.get('input_commit')}")
            graph = store.read_run().graph
            if graph is not None:
                for node in graph.nodes:
                    if node.id == node_id:
                        sources += [f"depends on {d}" for d in node.depends_on]
                        for ref in node.context.from_:
                            target = ref.field or ref.artifact or ""
                            sources.append(f"context from {ref.node} {target}".rstrip())
        files: tuple[str, ...] = ()
        verification = ""
        result_path = directory / "result.spec.yaml"
        if result_path.is_file():
            with contextlib.suppress(OSError, ValueError):
                result = load_spec(ResultSpec, result_path)
                if result.changes is not None:
                    files = tuple(result.changes.files)
                if result.verification is not None:
                    v = result.verification
                    lines = [f"acceptance: {v.acceptance} (mode {v.mode})"]
                    if v.skip_reason:
                        lines.append(f"skip reason: {v.skip_reason}")
                    lines += [f"  {d.run} -> exit {d.exit}" for d in v.details]
                    verification = "\n".join(lines)
                else:
                    verification = f"status: {result.status} (no verification recorded)"
        diff_path = diff_artifact_path(directory)
        diff = _bounded(read_text(diff_path), _DIFF_LIMIT) if diff_path.is_file() else ""
        logs = "\n".join(
            part
            for part in (
                ("stdout.log:\n" + text("stdout.log", 2000)) if text("stdout.log") else "",
                ("stderr.log:\n" + text("stderr.log", 2000)) if text("stderr.log") else "",
                "\n".join(self._activity.get(node_id, ())),
            )
            if part
        )
        return NodeDetail(
            node_id=node_id,
            run_id=run_id,
            handoff=text("prompt.md") or text("task.spec.yaml"),
            sources=tuple(sources),
            changed_files=files,
            diff=diff,
            verification=verification,
            logs=logs,
        )

    async def plan(self, revision: int | None = None) -> PlanView:
        store = self._store()
        if store is None:
            raise SessionError("no session is open")
        number = revision or store.read().plan_revision
        if not number:
            raise SessionError("no plan has been proposed yet")
        return await asyncio.to_thread(self._plan_view, store, number)

    def _store(self) -> SessionStore | None:
        return self._owner.store if self._owner is not None else self._readonly

    def _head(self) -> str:
        return _git(self.repo_root, "rev-parse", "HEAD").stdout.strip()

    def _contained(self, commit: str) -> bool:
        """Whether the checked-out HEAD already contains `commit` (cached briefly per pair)."""
        now = time.monotonic()
        cached = self._contains.get(commit)
        if cached is not None and now - cached[1] < _CONTAINS_TTL_S:
            return cached[0]
        head = self._head()
        found = bool(head) and (
            _git(self.repo_root, "merge-base", "--is-ancestor", commit, head).returncode == 0
        )
        self._contains[commit] = (found, now)
        return found

    def _base_spec(self, store: SessionStore) -> dict[str, Any] | None:
        record = store.read()
        if not record.rounds:
            return None
        last = record.rounds[-1]
        if last.state == "superseded" and last.checkpoint_id:
            with contextlib.suppress(SessionError, OSError, ValueError):
                manifest = load_checkpoint(store, last.checkpoint_id)
                if manifest.state == "sealed" and manifest.combined_commit:
                    return {"kind": "checkpoint", "checkpoint_id": last.checkpoint_id}
            return None
        with contextlib.suppress(StoreError, OSError, ValueError):
            run = _reader(self.repo_root, last.run_id).read_run()
            if run.state == "done" and run.final_commit and not self._contained(run.final_commit):
                return {"kind": "integration", "source_run": last.run_id}
        return None

    def _next_base(self, store: SessionStore) -> BaseView:
        spec = self._base_spec(store)
        if spec is None:
            return BaseView("head", "repository HEAD (pinned when the round starts)", None, False)
        if spec["kind"] == "checkpoint":
            manifest = load_checkpoint(store, str(spec["checkpoint_id"]))
            return BaseView(
                "checkpoint",
                f"checkpoint {manifest.checkpoint_id} of {manifest.source_run} (unapplied)",
                manifest.combined_commit,
                True,
            )
        run = _reader(self.repo_root, str(spec["source_run"])).read_run()
        return BaseView(
            "integration",
            f"unapplied integration of {run.run_id}",
            run.final_commit,
            True,
        )

    def _current_run(self, store: SessionStore) -> str | None:
        if self._controller is not None and self._controller.run_id is not None:
            return self._controller.run_id
        rounds = store.read().rounds
        return rounds[-1].run_id if rounds else None

    # --- the view ---------------------------------------------------------------------------

    async def view(self) -> SessionView:
        return await asyncio.to_thread(self._view)

    def _view(self) -> SessionView:
        store = self._store()
        coordinators = self.coordinator_options()
        chosen = next((o for o in coordinators if o.selected), None)
        flags = self._flags
        if store is None:
            return SessionView(
                None, False, self._readonly_reason, "none", "open", 0, 0, False, False,
                flags.stopping, False, None, (), (), (), (), (), None, (), coordinators, chosen,
                BaseView("head", "repository HEAD", None, False), None, tuple(self._notices),
                stamp=f"none|{flags.stopping}|{self._notice_seq}",
            )  # fmt: skip
        record = store.read()
        messages = tuple(
            MessageView(m["message_id"], m["author"], m["provenance"], m["content"], m["ts"])
            for m in store.messages()
        )
        turns = sorted(store.list_records("turns"), key=lambda t: _number(t["turn_id"]))
        turn_views = tuple(
            TurnView(
                t["turn_id"],
                t["status"],
                t.get("steps_used", 0),
                _outcome(t).get("error"),
            )
            for t in turns
        )
        question_sets = list(self._question_sets(store, turns))
        run_id = self._current_run(store)
        nodes: tuple[NodeView, ...] = ()
        if run_id is not None:
            with contextlib.suppress(StoreError, OSError, ValueError):
                nodes, node_questions = self._nodes(run_id)
                question_sets += node_questions
        rounds = tuple(self._rounds(record.rounds))
        pending: PlanView | None = None
        if record.plan_revision and not any(
            a["plan_revision"] == record.plan_revision for a in store.approvals()
        ):
            with contextlib.suppress(SessionError, OSError, ValueError):
                pending = self._plan_view(store, record.plan_revision)
        running = self.executing or flags.starting
        base = self._next_base(store)
        deliverable = None
        if not running:
            found = self._deliverable(store)
            deliverable = f"{found[0]} {found[1]}" if found else None
        recovery = self._recovery(record.state, rounds, turn_views, running)
        stamp = "|".join(
            str(part)
            for part in (
                record.session_id, record.revision, len(messages), running,
                flags.coordinating, flags.checkpointing, flags.stopping, self._notice_seq,
                self._apply is not None and not self._apply.done(),
                tuple((n.node_id, n.state, n.blocked_reason, n.attempts, n.agent, n.activity)
                      for n in nodes),
                tuple((q.source, q.revision, q.state, q.node_id) for q in question_sets),
                tuple((r.run_id, r.round_state, r.run_state) for r in rounds),
                tuple((t.turn_id, t.status) for t in turn_views),
                chosen.agent if chosen else None, deliverable, base.kind,
            )
        )  # fmt: skip
        return SessionView(
            session_id=record.session_id,
            owned=self._owner is not None,
            readonly_reason=self._readonly_reason,
            session_state=record.state,
            admission=record.admission,
            session_revision=record.revision,
            plan_revision=record.plan_revision,
            coordinating=flags.coordinating,
            checkpointing=flags.checkpointing,
            stopping=flags.stopping,
            round_running=running,
            run_id=run_id,
            messages=messages,
            question_sets=tuple(question_sets),
            nodes=nodes,
            rounds=rounds,
            turns=turn_views,
            pending_plan=pending,
            recovery=recovery,
            coordinators=coordinators,
            coordinator=chosen,
            base=base,
            deliverable=deliverable,
            notices=tuple(self._notices),
            stamp=stamp,
            applying=self._apply is not None and not self._apply.done(),
        )

    def _question_sets(
        self, store: SessionStore, turns: list[dict[str, Any]] | None = None
    ) -> list[QuestionSetView]:
        if turns is None:
            turns = sorted(store.list_records("turns"), key=lambda t: _number(t["turn_id"]))
        latest_turn = turns[-1]["turn_id"] if turns else None
        records = sorted(store.list_records("questions"), key=lambda r: int(r["revision"]))
        newest = records[-1]["revision"] if records else None
        found: list[QuestionSetView] = []
        for item in records:
            state: Literal["open", "answered", "stale"] = "answered"
            note = ""
            if item["state"] == "open":
                if item["revision"] == newest and item.get("turn_id") == latest_turn:
                    state = "open"
                else:
                    state, note = "stale", "superseded by a later request; it cannot be answered"
            found.append(
                QuestionSetView(
                    "coordinator",
                    int(item["revision"]),
                    None,
                    state,
                    tuple(_question(q) for q in item["questions"]),
                    note,
                )
            )
        return found

    def _nodes(self, run_id: str) -> tuple[tuple[NodeView, ...], list[QuestionSetView]]:
        store = _reader(self.repo_root, run_id)
        record = store.read_run()
        graph = record.graph
        spec = {n.id: n for n in graph.nodes} if graph is not None else {}
        routes = self._route_info(store)
        views: list[NodeView] = []
        questions: list[QuestionSetView] = []
        for node_id, node in record.nodes.items():
            graph_node = spec.get(node_id)
            access, owns, limit = "write", (), 1
            if graph_node is not None and graph is not None:
                with contextlib.suppress(ValueError):
                    task = task_for(graph_node, graph, run_id)
                    access, owns = task.access or "write", tuple(task.owns)
                    limit = task.budget.max_attempts or 1
            agent, model = routes.get(node_id, (None, None))
            views.append(
                NodeView(
                    node_id=node_id,
                    state=node.state,
                    blocked_reason=node.blocked_reason,
                    depends_on=tuple(graph_node.depends_on) if graph_node is not None else (),
                    access=access,
                    owns=owns,
                    agent=agent,
                    model=model,
                    attempts=node.attempts,
                    max_attempts=limit,
                    output_commit=node.output_commit,
                    activity=tuple(self._activity.get(node_id, ())),
                )
            )
            if node.state == "blocked" and node.blocked_reason in (
                "needs_input",
                "attempts_exhausted",
                "duration_exhausted",
            ):
                questions.append(self._node_questions(store, node_id, node.blocked_reason))
        return tuple(views), questions

    def _node_questions(self, store: RunStore, node_id: str, reason: str | None) -> QuestionSetView:
        try:
            result = current_questions(store, node_id, live=True)
        except (ValueError, OSError):
            result = None
        items = tuple(_question(q.model_dump()) for q in result.open_questions) if result else ()
        revision = (result.question_revision or 1) if result else 0
        if reason == "needs_input":
            return QuestionSetView("node", revision, node_id, "open", items)
        retained: tuple[str, ...] = ()
        with contextlib.suppress(ValueError, OSError):
            submissions = read_answers(store.node_dir(node_id), node_id).submissions
            if submissions:
                retained = tuple(f"{k}: {v}" for k, v in submissions[-1].answers.items())
        limit = "attempt" if reason == "attempts_exhausted" else "active-time"
        note = (
            f"the {limit} limit is exhausted: the answer is retained but cannot run; continuing "
            "needs an explicit policy change (a revised, approved plan with a higher limit)"
        )
        return QuestionSetView("node", revision, node_id, "stale", items, note, retained)

    def _route_info(self, store: RunStore) -> dict[str, tuple[str | None, str | None]]:
        path = store.events_path
        size = path.stat().st_size if path.is_file() else 0
        cached = self._routes.get(store.run_id)
        if cached is not None and cached[0] == size:
            return cached[1]
        found: dict[str, tuple[str | None, str | None]] = {}
        with contextlib.suppress(OSError, ValueError):
            for event in read_events(path):
                if event.type == "node.routed":
                    decision = event.data.get("decision")
                    if isinstance(decision, Mapping):
                        values: Mapping[str, Any] = decision  # pyright: ignore[reportUnknownVariableType]
                        found[str(event.data["node_id"])] = (
                            values.get("agent"),
                            values.get("model"),
                        )
        self._routes[store.run_id] = (size, found)
        return found

    def _rounds(self, rounds: list[Any]) -> list[RoundView]:
        views: list[RoundView] = []
        for item in rounds:
            run_state, reason, final = "unknown", None, None
            with contextlib.suppress(StoreError, OSError, ValueError):
                run = _reader(self.repo_root, item.run_id).read_run()
                run_state, reason, final = run.state, run.interrupted_reason, run.final_commit
            views.append(
                RoundView(
                    item.run_id,
                    item.plan_revision,
                    item.state,
                    run_state,
                    reason,
                    item.checkpoint_id,
                    final,
                )
            )
        return views

    def _recovery(
        self,
        state: str,
        rounds: tuple[RoundView, ...],
        turns: tuple[TurnView, ...],
        running: bool,
    ) -> tuple[str, ...]:
        needs: list[str] = []
        if running:
            return ()
        for item in rounds:
            if item.round_state == "active" and item.run_state == "interrupted":
                needs.append(
                    f"round {item.run_id} is interrupted; continue it explicitly (Continue round) "
                    "after recovery proves old workers ended"
                )
            elif item.round_state == "active" and item.run_state == "running":
                needs.append(
                    f"round {item.run_id} was running when its owner stopped; Continue round "
                    "recovers it first"
                )
        for turn in turns:
            if turn.status == "interrupted":
                needs.append(
                    f"coordinator turn {turn.turn_id} is interrupted; Resume turn continues it"
                )
            elif turn.status == "blocked":
                needs.append(
                    f"coordinator turn {turn.turn_id} is blocked: an old coordinator process could "
                    "not be confirmed stopped; check it before continuing"
                )
        if state == "interrupted" and not needs:
            needs.append("the session was interrupted; review its state before continuing")
        return tuple(needs)

    def _plan_view(self, store: SessionStore, revision: int) -> PlanView:
        key = (store.session_id, revision)
        approved = any(a["plan_revision"] == revision for a in store.approvals())
        cached = self._plans.get(key)
        if cached is not None:
            return replace(cached, approved=approved)
        body = store.plan(revision)
        graph = Graph.model_validate(body["graph"])
        explanation: str = body.get("explanation", "")
        summary = None
        if "\nChanges: " in explanation:
            explanation, summary = explanation.split("\nChanges: ", 1)
        base = body.get("base")
        if not base:
            base_label = "repository HEAD, pinned when the round starts"
        elif base.get("kind") == "checkpoint":
            base_label = f"checkpoint {base.get('checkpoint_id')} (retained, unapplied)"
        else:
            base_label = f"unapplied integration of {base.get('source_run')}"
        nodes = tuple(self._plan_node(graph, node) for node in graph.nodes)
        problems = tuple(
            _validate_plan(
                graph, config=self.config, available=self._available, levels=self._levels
            )
        )
        integration = tuple(_acceptance_text(a) for a in graph.integration_acceptance)
        if graph.commands is not None:
            integration += tuple(f"{k}: {v}" for k, v in graph.commands.model_dump().items() if v)
        if graph.verify == "none":
            integration += (f"verify: none ({graph.verification_skip_reason})",)
        policy = (
            f"on failure {graph.policy.on_failure}; at most {graph.policy.max_nodes} nodes; "
            f"concurrency {self._concurrency or self.config.defaults.concurrency}"
        )
        view = PlanView(
            revision=revision,
            parent=body.get("parent"),
            digest=body["digest"],
            explanation=explanation,
            change_summary=summary,
            base_label=base_label,
            nodes=nodes,
            integration_checks=integration,
            policy=policy,
            approved=approved,
            problems=problems,
            proposer=_proposer_label(body.get("proposer")),
        )
        self._plans[key] = view
        return view

    def _plan_node(self, graph: Graph, node: Any) -> PlanNodeView:
        try:
            task = task_for(node, graph, "preflight")
        except ValueError as exc:
            return PlanNodeView(
                node.id, node.category, node.goal, tuple(node.depends_on), "?", (), "?", (),
                None, (), str(exc), (),
            )  # fmt: skip
        resolution = resolve_level(
            task=task.permissions,
            default=self.config.defaults.permissions,
            planner_read_only=task.access == "read",
        )
        classification = _classification(task, self.config)
        decision = route_agent(
            self.config,
            classification,
            pinned=task.routing.agent,
            available=self._available,
            levels=self._levels,
            permissions=resolution.effective,
            load={},
        )
        candidates = [decision.agent] if decision.agent else []
        candidates += [a.agent for a in decision.alternatives if a.agent not in candidates]
        model = None
        if decision.agent and decision.agent in self.config.agents:
            model = (
                task.routing.model
                or select_model(self.config.agents[decision.agent], classification).model
            )
        checks = [_acceptance_text(a) for a in task.acceptance]
        if task.commands is not None:
            checks += [f"{k}: {v}" for k, v in task.commands.model_dump().items() if v]
        budget = task.budget
        limits = (
            f"max attempts {budget.max_attempts or 1}"
            + (f", timeout {budget.timeout_s}s" if budget.timeout_s else "")
            + (f", active time {budget.max_duration_s}s" if budget.max_duration_s else "")
        )
        context = [
            f"from {r.node} {r.field or r.artifact or ''}".rstrip() for r in task.context.from_
        ]
        context += [f"file {f}" for f in task.context.files]
        return PlanNodeView(
            node_id=node.id,
            category=task.category,
            goal=task.goal,
            depends_on=tuple(task.depends_on),
            access=task.access or "write",
            owns=tuple(task.owns),
            permissions=resolution.effective,
            candidates=tuple(c for c in candidates if c),
            model=model,
            checks=tuple(checks),
            limits=limits,
            context=tuple(context),
        )


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """A read-only git query: no stdin, bounded time; a timeout reads as a failed query."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(exc.cmd, 124, "", f"git {args[0]} timed out")


def _reader(repo_root: Path, run_id: str) -> RunStore:
    """A read-only handle on a run store.

    `RunStore.open` sweeps leftover temp files, which would race the active writer's atomic
    writes; projections and inspection must never do that.
    """
    store = RunStore(repo_root, run_id)
    if not store.run_json.is_file():
        raise StoreError(f"run {run_id} not found")
    return store


def _settled(nodes: Mapping[str, Mapping[str, Any]]) -> bool:
    """Nothing runs, nothing waits for dispatch and no node waits for an operator answer."""
    if not nodes:
        return False
    for node in nodes.values():
        state = node["state"]
        if state in _TERMINAL:
            continue
        if state == "blocked" and node.get("blocked_reason") != "needs_input":
            continue
        return False
    return True


def _proposer_label(proposer: object) -> str | None:
    """A display label for the recorded proposing profile (the record itself is immutable)."""
    if not isinstance(proposer, dict):
        return None
    data: dict[str, Any] = proposer  # pyright: ignore[reportUnknownVariableType]
    return f"{data.get('agent')} {data.get('cli_version')} ({data.get('mode')})"


def _outcome(turn: Mapping[str, Any]) -> dict[str, Any]:
    outcome = turn.get("outcome")
    return dict(outcome) if isinstance(outcome, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def _number(turn_id: str) -> int:
    digits = "".join(c for c in turn_id if c.isdigit())
    return int(digits) if digits else 0


def _question(raw: Mapping[str, Any]) -> QuestionView:
    return QuestionView(
        id=str(raw["id"]),
        text=str(raw["text"]),
        kind=str(raw.get("kind") or "free"),
        options=tuple(str(o) for o in (raw.get("options") or ())),
        required=bool(raw.get("required", True)),
        default=raw.get("default"),
    )


def _acceptance_text(entry: RunCheck | ScopeCheck) -> str:
    if isinstance(entry, RunCheck):
        return f"run {entry.run} (expect exit {entry.expect_exit})"
    return "diff_scope " + " ".join(entry.diff_scope)
