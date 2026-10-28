"""Session UI phases as projections of the service view (design 14.1); no Textual here.

Projection states are labels, not extra run states: several node states coexist beneath one
projection, and a waiting node never makes unrelated execution look idle. Every state, node
outcome and control has a text label; colour is only supplementary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from relay.core.session_app import NodeView, QuestionSetView, SessionView

from . import keys

__all__ = [
    "Action",
    "ComposerMode",
    "Projection",
    "composer_mode",
    "node_label",
    "pending_actions",
    "project",
]

State = Literal[
    "No session",
    "Idle",
    "Coordinating",
    "Awaiting answers",
    "Awaiting plan approval",
    "Executing",
    "Checkpointing",
    "Stopping",
    "Interrupted",
    "Ready for review/delivery",
]


@dataclass(frozen=True)
class Projection:
    state: State
    detail: str

    @property
    def text(self) -> str:
        return f"State: {self.state}" + (f" - {self.detail}" if self.detail else "")


def node_label(node: NodeView) -> str:
    """The text label of a node state (never colour alone)."""
    if node.state == "blocked":
        reason = (node.blocked_reason or "blocked").replace("_", " ")
        return f"blocked: {reason}"
    if node.state == "failed":
        return "FAILED"
    return node.state


def _counts(view: SessionView) -> str:
    running = sum(1 for n in view.nodes if n.state == "running")
    waiting = sum(1 for n in view.nodes if n.blocked_reason == "needs_input")
    done = sum(1 for n in view.nodes if n.state == "done")
    pending = sum(1 for n in view.nodes if n.state == "pending")
    failed = sum(1 for n in view.nodes if n.state in ("failed", "cancelled"))
    parts = [
        f"{running} running" if running else "",
        f"{waiting} awaiting answers" if waiting else "",
        f"{pending} pending" if pending else "",
        f"{done} done" if done else "",
        f"{failed} failed/cancelled" if failed else "",
    ]
    text = ", ".join(p for p in parts if p)
    if view.admission == "closed":
        text += ("; " if text else "") + "admission paused"
    return text


def project(view: SessionView | None, *, stopping: bool = False) -> Projection:
    """The one projection state shown for this view."""
    if view is None:
        return Projection("Stopping" if stopping else "No session", "")
    if stopping or view.stopping:
        return Projection("Stopping", "cancelling and draining active work; wait")
    if view.session_id is None:
        return Projection(
            "No session", f"type a request to start one, or open saved sessions{_via('sessions')}"
        )
    if view.checkpointing:
        return Projection("Checkpointing", "admission paused; " + _counts(view))
    if view.coordinating:
        return Projection("Coordinating", "the coordinator is working on this turn")
    if view.round_running:
        running = any(n.state == "running" for n in view.nodes)
        if not running and any(n.blocked_reason == "needs_input" for n in view.nodes):
            return Projection("Awaiting answers", _counts(view))
        return Projection("Executing", _counts(view))
    if any(q.state == "open" and q.source == "coordinator" for q in view.question_sets):
        return Projection("Awaiting answers", "the coordinator asked questions")
    if view.pending_plan is not None:
        return Projection(
            "Awaiting plan approval",
            f"plan revision {view.pending_plan.revision} (review{_via('plan')})",
        )
    if view.recovery:
        return Projection("Interrupted", view.recovery[0])
    if view.deliverable is not None:
        return Projection(
            "Ready for review/delivery", f"{view.deliverable} (review{_via('deliver')})"
        )
    return Projection("Idle", "")


def _key(action: str) -> str:
    return keys.label(action) or action.replace("_", " ")


def _via(action: str) -> str:
    """` with Ctrl+R` for a bound action, "" when only the palette/buttons reach it."""
    text = keys.label(action)
    return f" with {text}" if text else ""


@dataclass(frozen=True)
class ComposerMode:
    kind: Literal["answer", "steer", "request"]
    label: str
    target: QuestionSetView | None = None


def _open_sets(view: SessionView) -> list[QuestionSetView]:
    """Answerable question sets; coordinator questions wait while a round executes."""
    return [
        q
        for q in view.question_sets
        if q.state == "open" and (q.source == "node" or not view.round_running)
    ]


def composer_mode(
    view: SessionView | None, *, prefer_request: bool = False, selected: str | None = None
) -> ComposerMode:
    """What Ctrl+S does with the draft: answer the active question, steer, or a new request."""
    if view is None or view.session_id is None:
        if view is not None and view.coordinator is None:
            return ComposerMode(
                "request",
                "New request: coordination unavailable (select a coordinator for details); "
                f"sessions{_via('sessions')}",
            )
        return ComposerMode(
            "request", f"New request ({keys.label('send') or 'send'} sends; starts a new session)"
        )
    sets = _open_sets(view)
    if sets and not prefer_request:
        chosen = next((q for q in sets if q.node_id is not None and q.node_id == selected), None)
        target = chosen or sets[0]
        who = (
            f"node {target.node_id}" if target.node_id else "the coordinator"
        ) + f" question set {target.revision}"
        first = target.questions[0].text if target.questions else ""
        more = f" (+{len(target.questions) - 1} more)" if len(target.questions) > 1 else ""
        return ComposerMode(
            "answer",
            f"Answer {who}: {first}{more} | "
            f"{_key('send')} answers, {_key('toggle_mode')} new request",
            target,
        )
    if view.round_running:
        return ComposerMode(
            "steer",
            f"Steering request: Checkpoint and revise ({_key('send')} pauses admission "
            "and asks how to settle active work)",
        )
    who = view.coordinator.agent if view.coordinator else None
    if who is None:
        return ComposerMode(
            "request",
            "New request: coordination unavailable (select a coordinator for details); "
            "inspection still works",
        )
    return ComposerMode(
        "request", f"New request to the {who} coordinator ({keys.label('send') or 'send'} sends)"
    )


@dataclass(frozen=True)
class Action:
    id: str
    label: str


def pending_actions(view: SessionView | None) -> list[Action]:
    """The visible, focusable controls for what the operator can do next."""
    if view is None or view.session_id is None:
        return [
            Action("sessions", f"Sessions{keys.hint('sessions')}"),
            Action("coordinator", f"Coordinator{keys.hint('coordinator')}"),
        ]
    actions: list[Action] = []
    if view.pending_plan is not None and not view.round_running:
        actions.append(
            Action("plan", f"Review plan r{view.pending_plan.revision}{keys.hint('plan')}")
        )
    if view.round_running or view.coordinating or view.checkpointing:
        actions.append(Action("stop", f"Stop{keys.hint('stop')}"))
    if not view.round_running:
        if any(
            r.round_state == "active" and r.run_state in ("interrupted", "running")
            for r in view.rounds
        ):
            actions.append(Action("continue", "Continue round"))
        interrupted = [t for t in view.turns if t.status == "interrupted"]
        if interrupted:
            actions.append(Action("resume-turn", f"Resume turn {interrupted[-1].turn_id}"))
        if view.deliverable is not None:
            actions.append(Action("deliver", f"Review delivery{keys.hint('deliver')}"))
    if view.coordinator is None:
        actions.append(Action("coordinator", f"Coordinator unavailable{keys.hint('coordinator')}"))
    return actions
