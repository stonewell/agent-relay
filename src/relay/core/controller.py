"""Shared async session controller (design 8.7; task 2.22).

`SessionController` is an application service over the existing core, not another scheduler.
It owns one approved round at a time: it starts the run through the session's idempotent
launch intent, hosts the foreground graph run as a task independent of any UI widget, accepts
typed owner commands (answers, pause/resume, cancellation) with command ids and expected
revisions, and shuts down by draining real worker completion before releasing ownership.

Nothing here executes or schedules work itself: the graph run and its scheduler remain the
only dispatchers, and every state change is durable before it is acknowledged.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from relay.adapters.base import AgentAdapter

from .answers import record_answers
from .graph import GraphResult, run_graph
from .registry import Config
from .scheduler import SchedulerControl
from .sessions import SessionConflict, SessionError, SessionOwner
from .specs import Graph
from .specs.answers import AnswerValue, Submission
from .store import RunStore, iso

__all__ = ["AnswerRejected", "CommandResult", "SessionController"]

Provenance = Literal["operator-tty", "operator-cli", "host-submitted", "host-attested"]


class AnswerRejected(SessionError):
    """The answer cannot be recorded now (stale, ineligible or invalid); nothing changed."""


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    applied: bool  # False for an identical duplicate (a no-op)
    detail: str = ""


class SessionController:
    """One session owner's application service for the active approved round."""

    def __init__(
        self,
        owner: SessionOwner,
        *,
        config: Config,
        adapters: Mapping[str, AgentAdapter] | None = None,
        concurrency: int | None = None,
        allow_dirty: bool = False,
    ) -> None:
        self.owner = owner
        self._config = config
        self._adapters = adapters
        self._concurrency = concurrency
        self._allow_dirty = allow_dirty
        self._control: SchedulerControl | None = None
        self._store: RunStore | None = None
        self._task: asyncio.Task[GraphResult] | None = None
        self._closed = False

    # --- the active round -------------------------------------------------------------------

    @property
    def run_id(self) -> str | None:
        return self._store.run_id if self._store is not None else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start_round(self) -> str:
        """Create (or attach to) the approved launch's run and begin executing it.

        The run holds open for operator answers (`hold`) until :meth:`finish_round` or
        :meth:`shutdown`. Raises :class:`SessionConflict` unless the durable gate, revision and
        intent allow admission (section 8.7).
        """
        if self._closed:
            raise SessionError("controller is shut down")
        if self.running:
            raise SessionConflict("a round is already active in this session")
        store = await asyncio.to_thread(self.owner.start_run)
        record = self.owner.record
        assert record.intent is not None
        plan = self.owner.store.plan(record.intent.plan_revision)
        graph = Graph.model_validate(plan["graph"])
        control = SchedulerControl()
        control.hold(True)
        self._control, self._store = control, store
        self._task = asyncio.create_task(
            run_graph(
                self.owner.store.repo_root,
                graph,
                config=self._config,
                adapters=self._adapters,
                concurrency=self._concurrency,
                allow_dirty=self._allow_dirty,
                store=store,
                session_owner=self.owner,
                control=control,
            )
        )
        await self._bound()
        return store.run_id

    async def _bound(self) -> None:
        """Wait until the run's shared writer exists (or the run ended before reaching it)."""
        assert self._control is not None and self._task is not None
        while not self._control.bound:
            if self._task.done():
                return
            await asyncio.sleep(0.02)

    async def finish_round(self) -> GraphResult:
        """Stop holding the round open and wait for it to finish naturally."""
        task, control = self._require_round()
        control.hold(False)
        return await task

    # --- owner commands ---------------------------------------------------------------------

    def _require_round(self) -> tuple[asyncio.Task[GraphResult], SchedulerControl]:
        if self._task is None or self._control is None:
            raise SessionConflict("no active round")
        return self._task, self._control

    def _command(self, command_id: str, content: dict[str, Any]) -> bool:
        return self.owner.record_command(command_id, content)

    async def answer(
        self,
        command_id: str,
        node_id: str,
        question_revision: int,
        answers: dict[str, AnswerValue],
        *,
        provenance: Provenance = "operator-tty",
    ) -> CommandResult:
        """Record an answer for a blocked node while independent nodes keep running.

        Eligibility depends on that node's durable question set. A stale revision, a node that
        is not waiting for input or invalid answers raise :class:`AnswerRejected`. The answer
        is recorded through the active run's shared writer and the node is requeued; whether
        it dispatches now depends on the admission gate and the node's existing limits.
        """
        task, control = self._require_round()
        content = {
            "kind": "answer",
            "node": node_id,
            "question_revision": question_revision,
            "answers": answers,
            "provenance": provenance,
        }
        if not await asyncio.to_thread(self._command, command_id, content):
            return CommandResult(command_id, False, "duplicate")
        store, events = control.store, control.events
        if task.done() or store is None or events is None:
            raise AnswerRejected("the round is not running")
        node = store.read_run().nodes.get(node_id)
        if node is None or node.state != "blocked" or node.blocked_reason != "needs_input":
            raise AnswerRejected(f"node {node_id} is not waiting for input")
        if node.question_revision != question_revision:
            raise AnswerRejected(
                f"stale question revision {question_revision}; current is {node.question_revision}"
            )
        submission = Submission(
            submission_id=command_id,
            question_revision=question_revision,
            provenance=provenance,
            submitted_at=iso(datetime.now(UTC)),
            answers=answers,
        )
        try:
            await asyncio.to_thread(
                record_answers, store, node_id, submission, events=events, live=True
            )
        except ValueError as exc:
            raise AnswerRejected(str(exc)) from exc
        control.requeue(node_id)
        return CommandResult(command_id, True)

    async def pause(self, command_id: str) -> CommandResult:
        """Durably close admission: running workers continue, nothing new is dispatched."""
        _, control = self._require_round()
        if not await asyncio.to_thread(self._command, command_id, {"kind": "pause"}):
            return CommandResult(command_id, False, "duplicate")
        await asyncio.to_thread(self.owner.close_admission)
        control.pause()
        return CommandResult(command_id, True)

    async def resume(self, command_id: str) -> CommandResult:
        _, control = self._require_round()
        if not await asyncio.to_thread(self._command, command_id, {"kind": "resume"}):
            return CommandResult(command_id, False, "duplicate")
        await asyncio.to_thread(self.owner.open_admission)
        control.resume()
        return CommandResult(command_id, True)

    async def drain(self, *, timeout_s: float | None = None) -> bool:
        """Wait until no worker is running; True when drained, False on timeout."""
        task, control = self._require_round()
        loop = asyncio.get_running_loop()
        deadline = None if timeout_s is None else loop.time() + timeout_s
        while control.running or control.has_commands:
            if task.done():
                break
            if deadline is not None and loop.time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    async def cancel_node(self, command_id: str, node_id: str) -> CommandResult:
        _, control = self._require_round()
        if not await asyncio.to_thread(
            self._command, command_id, {"kind": "cancel_node", "node": node_id}
        ):
            return CommandResult(command_id, False, "duplicate")
        control.cancelled_nodes.add(node_id)
        return CommandResult(command_id, True)

    async def cancel_round(self, command_id: str) -> CommandResult:
        """Cancel the round and wait until every worker has actually stopped."""
        task, control = self._require_round()
        if not await asyncio.to_thread(self._command, command_id, {"kind": "cancel_round"}):
            return CommandResult(command_id, False, "duplicate")
        control.hold(False)
        control.cancel.set()
        await asyncio.wait({task})
        return CommandResult(command_id, True)

    # --- projection -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """A consistent read of the session and active run for projections (read only)."""
        record = self.owner.record
        out: dict[str, Any] = {
            "session_id": record.session_id,
            "session_revision": record.revision,
            "state": record.state,
            "admission": record.admission,
            "plan_revision": record.plan_revision,
            "linked_runs": list(record.linked_runs),
            "run_id": self.run_id,
            "round_running": self.running,
            "nodes": {},
        }
        if self._store is not None:
            run = self._store.read_run()
            out["run_state"] = run.state
            out["nodes"] = {
                key: {
                    "state": node.state,
                    "blocked_reason": node.blocked_reason,
                    "question_revision": node.question_revision,
                    "attempts": node.attempts,
                }
                for key, node in run.nodes.items()
            }
        return out

    # --- lifetime ---------------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close admission, cancel, drain real completion, then release ownership.

        Cancellation of this coroutine (repeated cancel, a second SIGINT, cancelling every
        asyncio task) never releases ownership while the run can still publish: the wait on
        the run task is repeated until it is actually done, and the cancellation is re-raised
        afterwards.
        """
        if self._closed:
            return
        self._closed = True
        interrupted = False
        task, control = self._task, self._control
        if control is not None:
            control.hold(False)
            control.pause()
        if task is not None:
            task.cancel()  # the run records itself `interrupted` and drains its workers
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    interrupted = True
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
            if self._store is not None and self._store.read_run().state == "interrupted":
                await asyncio.to_thread(self.owner.mark_interrupted)
        await asyncio.to_thread(self.owner.close)
        if interrupted:
            raise asyncio.CancelledError
