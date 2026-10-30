"""Conversation, task sidebar and inspector widgets (design 14.1).

Every widget renders plain text (`markup=False`): coordinator, worker and repository content is
untrusted and is never interpreted as markup. Widgets only project the service view.
"""

from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from relay.core.session_app import (
    DeliveryReview,
    NodeDetail,
    NodeView,
    PlanView,
    QuestionSetView,
    SessionView,
)

from . import keys
from .projection import node_label

__all__ = [
    "Conversation",
    "Inspector",
    "TaskList",
    "render_delivery",
    "render_plan",
    "render_questions",
]


def _author(author: str) -> str:
    return {"operator": "You", "coordinator": "Coordinator"}.get(author, author)


def render_questions(qs: QuestionSetView) -> str:
    who = f"Node {qs.node_id}" if qs.node_id else "Coordinator"
    head = f"QUESTIONS [{qs.state}] {who}, question set {qs.revision}"
    lines = [head]
    for q in qs.questions:
        optional = "" if q.required else " (optional)"
        lines.append(f"  {q.id}: {q.text}{optional}")
        for number, option in enumerate(q.options, 1):
            marker = " (default)" if option == q.default else ""
            lines.append(f"      {number}) {option}{marker}")
        if q.options:
            lines.append("      or type your own answer")
    if qs.retained:
        lines.append("  retained answer: " + "; ".join(qs.retained))
    if qs.note:
        lines.append(f"  note: {qs.note}")
    return "\n".join(lines)


def render_plan(plan: PlanView) -> str:
    """Graph, scopes, candidates, permissions, checks, limits, base and context (section 14.1)."""
    state = "approved" if plan.approved else "awaiting approval"
    lines = [
        f"PLAN revision {plan.revision} [{state}]"
        + (f" (revises {plan.parent})" if plan.parent else ""),
        f"Digest: {plan.digest[:16]}",
        f"Why: {plan.explanation}",
        "Change summary: "
        + (plan.change_summary or ("first plan" if not plan.parent else "none given")),
        f"Pinned base: {plan.base_label}",
        f"Limits and policy: {plan.policy}",
        "Integration checks: " + ("; ".join(plan.integration_checks) or "none"),
        "",
        "Tasks:",
    ]
    for node in plan.nodes:
        deps = ", ".join(node.depends_on) or "none"
        lines += [
            f"- {node.node_id} [{node.category}, {node.access}] {node.goal}",
            f"    depends on: {deps}",
            f"    scope (owns): {', '.join(node.owns) or 'whole repository'}",
            f"    candidates: {', '.join(node.candidates) or 'NONE ELIGIBLE'}"
            + (f"; model {node.model}" if node.model else ""),
            f"    effective permissions: {node.permissions}",
            f"    checks: {'; '.join(node.checks) or 'graph-level only'}",
            f"    limits: {node.limits}",
            f"    selected context: {'; '.join(node.context) or 'pinned base and PROJECT.spec.md'}",
        ]
    if plan.problems:
        lines += ["", "CANNOT APPROVE: " + "; ".join(plan.problems)]
    return "\n".join(lines)


def render_delivery(review: DeliveryReview) -> str:
    lines = [f"DELIVERY of {review.kind} {review.ref}"]
    if review.problems:
        lines.append("REFUSED: " + "; ".join(review.problems))
    if review.overlaps:
        lines.append(
            "Dirty overlap (uncommitted edits on delivered paths): " + ", ".join(review.overlaps)
        )
    else:
        lines.append("Dirty overlap: none found")
    lines += [
        f"Target branch: {review.target or 'current branch'}"
        + (f" at {review.target_tip[:12]}" if review.target_tip else ""),
        "Prepared commit: " + (review.prepared_commit or "-"),
        "Prepared operation: " + (review.digest[:16] if review.digest else "-"),
        f"Verification on the prepared tree: {review.outcome.upper()} ({review.outcome_detail})",
        "Required checks: " + ("; ".join(review.checks) or "none recorded"),
        "Changed on target: " + (", ".join(review.changed_paths) or "nothing"),
        "Cumulative session delta: " + (", ".join(review.cumulative_paths) or "-"),
    ]
    if review.noop:
        lines.append("The target already contains this work: applying changes nothing.")
    elif review.primary_unchanged:
        lines.append("Your primary branch is unchanged until you apply.")
    lines += ["", "Cumulative diff:", review.diff or "(no diff)"]
    return "\n".join(lines)


class Conversation(VerticalScroll):
    """Operator intent, coordinator explanations, plan cards, questions and summaries."""

    can_focus = True

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._seen: set[str] = set()
        self._live = Static("", markup=False, id="live", classes="live")

    def on_mount(self) -> None:
        self.mount(self._live)

    def _append(self, key: str, text: str, classes: str) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self.mount(Static(text, markup=False, classes=classes), before=self._live)

    def note(self, key: str, text: str, *, error: bool = False) -> None:
        self._append(key, ("Error: " if error else "Relay: ") + text, "note")

    def show(self, view: SessionView) -> None:
        for message in view.messages:
            key = f"{view.session_id}:{message.message_id}"
            text = message.content
            classes = "message " + message.author
            if message.author == "coordinator" and text.startswith("Proposed plan revision"):
                text = (
                    "PLAN CARD - "
                    + text
                    + f"\n(review the plan{keys.hint('plan')}; approval is explicit)"
                )
                classes += " plan-card"
            elif message.author == "coordinator" and text.startswith("Questions:"):
                classes += " question-card"
            self._append(key, f"{_author(message.author)}: {text}", classes)
        for seq, text in view.notices:
            self._append(f"notice:{seq}", f"Relay: {text}", "note")
        live: list[str] = []
        for qs in view.question_sets:
            if qs.state != "answered":
                live.append(render_questions(qs))
        if view.pending_plan is not None:
            plan = view.pending_plan
            live.append(
                f"PENDING PLAN revision {plan.revision}: {plan.explanation}"
                + (f"\nChange summary: {plan.change_summary}" if plan.change_summary else "")
                + f"\nNothing runs until you approve this revision{keys.hint('plan')}."
            )
        if view.recovery:
            live.append("RECOVERY REQUIRED:\n" + "\n".join(f"  - {r}" for r in view.recovery))
        if view.base.primary_unchanged:
            live.append(
                f"Follow-ups continue from {view.base.label}; your primary branch is unchanged."
            )
        self._live.update("\n\n".join(live))
        self.scroll_end(animate=False)

    def reset(self) -> None:
        self._seen.clear()
        for child in list(self.children):
            if child is not self._live:
                child.remove()
        self._live.update("")


def _task_prompt(node: NodeView) -> str:
    who = f"{node.agent}/{node.model}" if node.agent else "unrouted"
    deps = ", ".join(node.depends_on) or "-"
    return (
        f"{node.node_id} [{node_label(node)}] {who}\n"
        f"  deps: {deps}; {node.access}; attempts {node.attempts}/{node.max_attempts}"
    )


class TaskList(OptionList):
    """The task sidebar: dependencies, agent/model and state, as text labels."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id, markup=False)
        self.border_title = f"Tasks{keys.hint('view_tasks')}"
        self._ids: tuple[str, ...] = ()

    @property
    def selected(self) -> str | None:
        option = self.highlighted_option
        return option.id if option is not None else None

    def show(self, nodes: tuple[NodeView, ...]) -> None:
        ids = tuple(n.node_id for n in nodes)
        if ids == self._ids:
            for node in nodes:
                self.replace_option_prompt(node.node_id, _task_prompt(node))
            return
        keep = self.selected
        with self.prevent(OptionList.OptionHighlighted):
            self.set_options([Option(_task_prompt(n), id=n.node_id) for n in nodes])
            if keep in ids:
                self.highlighted = ids.index(keep)
        self._ids = ids


class Inspector(VerticalScroll):
    """Exact recipient handoffs, sources, changes, verification evidence and retained logs."""

    can_focus = True

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.border_title = f"Inspector{keys.hint('view_inspector')}"
        self._body = Static("Select a task to inspect its handoff.", markup=False)

    def on_mount(self) -> None:
        self.mount(self._body)

    @property
    def text(self) -> str:
        return str(self._body.content)

    def show_session(self, view: SessionView) -> None:
        lines = [f"Session {view.session_id or '-'} ({view.session_state})"]
        if view.readonly_reason:
            lines.append(view.readonly_reason)
        coordinator = view.coordinator
        if coordinator is None:
            lines.append("Coordinator: unavailable (select a coordinator for details)")
        else:
            details = [
                part
                for part in (
                    coordinator.mode or "",
                    f"model {coordinator.model}" if coordinator.model else "",
                )
                if part
            ]
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"Coordinator: {coordinator.agent}{suffix}")
        lines.append(f"Next base: {view.base.label}")
        if view.base.primary_unchanged:
            lines.append("Primary branch: unchanged (work continues from a retained snapshot)")
        for item in view.rounds:
            extra = f", checkpoint {item.checkpoint_id}" if item.checkpoint_id else ""
            lines.append(
                f"Round {item.run_id}: plan r{item.plan_revision}, {item.round_state}, "
                f"run {item.run_state}{extra}"
            )
        for need in view.recovery:
            lines.append(f"Recovery: {need}")
        self._body.update("\n".join(lines))

    def show_node(self, detail: NodeDetail) -> None:
        sections = [
            f"Task {detail.node_id} in round {detail.run_id}",
            "Result reported by the task:\n" + (detail.report or "-"),
            "Exact handoff delivered:\n" + (detail.handoff or "(not dispatched yet)"),
            "Sources:\n" + ("\n".join(detail.sources) or "-"),
            "Changed files:\n" + ("\n".join(detail.changed_files) or "-"),
            "Verification evidence:\n" + (detail.verification or "-"),
            "Diff:\n" + (detail.diff or "-"),
            "Retained logs:\n" + (detail.logs or "-"),
        ]
        self._body.update("\n\n".join(sections))
