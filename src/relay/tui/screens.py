"""Modal screens: plan review, delivery review, sessions, coordinator, revise, answers, help.

Screens show service results and return the operator's explicit choice to the app; any service
call made here only awaits the service, which owns the operation (a closed screen cancels
nothing).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from relay.core.session_app import (
    CoordinatorOption,
    DeliveryReview,
    PlanView,
    QuestionSetView,
    SessionSummary,
)

from . import keys
from .widgets import render_delivery, render_plan

if TYPE_CHECKING:
    from .app import RelayApp

__all__ = [
    "AnswerScreen",
    "ConfirmScreen",
    "CoordinatorScreen",
    "DeliveryScreen",
    "HelpScreen",
    "PlanScreen",
    "ReviseScreen",
    "SessionsScreen",
]

ReviseChoice = Literal["finish", "cancel", "back"]


class _Modal(ModalScreen[Any]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def action_close(self) -> None:
        self.dismiss(None)

    @property
    def relay(self) -> RelayApp:
        from .app import RelayApp

        app: object = self.app  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        assert isinstance(app, RelayApp)
        return app


class ConfirmScreen(_Modal):
    """An explicit confirmation; Cancel has the initial focus."""

    def __init__(self, message: str, confirm: str = "Confirm") -> None:
        super().__init__()
        self._message = message
        self._confirm = confirm

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self._message, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", compact=True)
                yield Button(self._confirm, id="confirm", variant="error", compact=True)

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class PlanScreen(_Modal):
    """Plan review; approval is an explicit action on exactly this revision."""

    def __init__(self, plan: PlanView) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            with VerticalScroll(id="plan-body", can_focus=True):
                yield Static(render_plan(self.plan), markup=False, id="plan-text")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", compact=True)
                blocked = bool(self.plan.problems) or self.plan.approved
                label = (
                    "Already approved"
                    if self.plan.approved
                    else f"Approve revision {self.plan.revision}"
                )
                yield Button(label, id="approve", variant="success", disabled=blocked, compact=True)

    def on_mount(self) -> None:
        self.query_one("#plan-body").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve":
            self.dismiss(self.plan.revision)
        else:
            self.dismiss(None)


class ReviseScreen(_Modal):
    """Checkpoint and revise: admission is already paused; choose how active work settles."""

    def __init__(self, active: list[str]) -> None:
        super().__init__()
        self._active = active

    def compose(self) -> ComposeResult:
        running = ", ".join(self._active) or "none"
        text = (
            "Checkpoint and revise\n\n"
            "Admission is paused: no new task will start.\n"
            f"Active work: {running}\n\n"
            "Let active work finish, or cancel it explicitly (cancelled edits stay diagnostic and "
            "never become a base). The verified outputs are then sealed as a checkpoint and the "
            "coordinator proposes a revised plan for your approval."
        )
        with Vertical(classes="dialog"):
            yield Static(text, markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Back (resume admission)", id="back", compact=True)
                yield Button("Let active work finish", id="finish", compact=True)
                yield Button("Cancel active work", id="cancel", variant="error", compact=True)

    def on_mount(self) -> None:
        self.query_one("#finish", Button).focus()

    def action_close(self) -> None:
        self.dismiss("back")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice: ReviseChoice = "back"
        if event.button.id == "finish":
            choice = "finish"
        elif event.button.id == "cancel":
            choice = "cancel"
        self.dismiss(choice)


class AnswerScreen(_Modal):
    """Structured answers for one question set; Ctrl+S submits."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("ctrl+s", "submit", "Submit")]

    def __init__(self, questions: QuestionSetView, prefill: str = "") -> None:
        super().__init__()
        self.questions = questions
        self._prefill = prefill

    def compose(self) -> ComposeResult:
        who = f"node {self.questions.node_id}" if self.questions.node_id else "the coordinator"
        with Vertical(classes="dialog"):
            yield Static(f"Answer {who}, question set {self.questions.revision}", markup=False)
            for index, q in enumerate(self.questions.questions):
                hint = f" (one of: {', '.join(q.options)})" if q.options else ""
                yield Label(f"{q.id}: {q.text}{hint}", markup=False)
                yield Input(
                    value=self._prefill if index == 0 else "",
                    placeholder=q.default or ("required" if q.required else "optional"),
                    id=f"answer-{index}",
                )
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", compact=True)
                yield Button("Submit answers (Ctrl+S)", id="submit", compact=True)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.focus_next()

    def action_submit(self) -> None:
        answers: dict[str, str | list[str]] = {}
        for index, q in enumerate(self.questions.questions):
            value = self.query_one(f"#answer-{index}", Input).value.strip()
            if value:
                answers[q.id] = value
        self.dismiss(answers)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.action_submit()
        else:
            self.dismiss(None)


class CoordinatorScreen(_Modal):
    """Only qualified modes can be chosen; unavailable ones explain why (section 7.4)."""

    def __init__(self, options: tuple[CoordinatorOption, ...]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        items: list[Option] = []
        for option in self._options:
            mark = (
                "selected"
                if option.selected
                else ("available" if option.available else "unavailable")
            )
            mode = f" {option.mode}" if option.mode else ""
            model = f", model {option.model}" if option.model else ""
            items.append(
                Option(
                    f"{option.agent} [{mark}]{mode}{model}\n  {option.reason}",
                    id=option.agent,
                    disabled=not option.available,
                )
            )
        with Vertical(classes="dialog"):
            yield Static("Coordinator (only qualified restricted modes)", markup=False)
            if not items:
                yield Static("No Claude or Codex agent is configured.", markup=False)
            yield OptionList(*items, id="coordinators", markup=False)
            if not any(o.available for o in self._options):
                yield Static(
                    "No coordinator is qualified here, so coordination is unavailable. Saved "
                    "sessions stay inspectable and headless commands keep working.",
                    markup=False,
                )
            yield Button("Close", id="close", compact=True)

    def on_mount(self) -> None:
        self.query_one("#coordinators", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_id)

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)


class SessionsScreen(_Modal):
    """Historical sessions; Enter opens, Ctrl+D deletes after confirmation."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("ctrl+d,delete", "delete", "Delete session"),
    ]

    def __init__(self, sessions: list[SessionSummary], current: str | None) -> None:
        super().__init__()
        self._sessions = sessions
        self._current = current

    def compose(self) -> ComposeResult:
        items = [Option("+ New session", id="new")]
        for s in self._sessions:
            mark = " (open)" if s.session_id == self._current else ""
            items.append(
                Option(
                    f"{s.session_id}{mark} [{s.state}] plan r{s.plan_revision}, "
                    f"{s.rounds} round(s), {s.checkpoints} checkpoint(s), {s.created_at}",
                    id=s.session_id,
                )
            )
        with Vertical(classes="dialog wide"):
            yield Static(
                "Sessions: Enter opens; Ctrl+D deletes (confirmed). Switching away from an "
                "executing session requires stopping it first.",
                markup=False,
            )
            yield OptionList(*items, id="sessions", markup=False)

    def on_mount(self) -> None:
        self.query_one("#sessions", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(("open", event.option_id))

    def action_delete(self) -> None:
        option = self.query_one("#sessions", OptionList).highlighted_option
        if option is not None and option.id not in (None, "new"):
            self.dismiss(("delete", option.id))


class DeliveryScreen(_Modal):
    """Delivery review through the shared service; Apply confirms the prepared operation."""

    def __init__(self) -> None:
        super().__init__()
        self.review: DeliveryReview | None = None
        self.result: dict[str, Any] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            with Horizontal(classes="target"):
                yield Label("Target branch:")
                yield Input(placeholder="current branch", id="target")
            with VerticalScroll(id="delivery-body", can_focus=True):
                yield Static("Preparing delivery...", markup=False, id="delivery-text")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", compact=True)
                yield Button("Prepare again", id="prepare", compact=True)
                yield Button("Apply", id="apply", variant="success", disabled=True, compact=True)

    def on_mount(self) -> None:
        self.query_one("#delivery-body").focus()
        self._prepare()

    def _prepare(self) -> None:
        target = self.query_one("#target", Input).value.strip() or None
        self.query_one("#apply", Button).disabled = True
        self.query_one("#delivery-text", Static).update("Preparing delivery...")
        self.run_worker(partial(self._prepared, target), group="delivery", exclusive=True)

    async def _prepared(self, target: str | None) -> None:
        relay = self.relay
        try:
            review = await relay.host.call(relay.service.prepare_delivery(target))
        except Exception as exc:  # noqa: BLE001 - shown to the operator
            self.query_one("#delivery-text", Static).update(f"Cannot prepare delivery: {exc}")
            return
        self.review = review
        self.query_one("#delivery-text", Static).update(render_delivery(review))
        apply = self.query_one("#apply", Button)
        apply.disabled = not review.applicable
        if review.digest:
            apply.label = f"Apply prepared operation {review.digest[:12]}"

    async def _apply(self) -> None:
        review = self.review
        if review is None or review.digest is None:
            return
        relay = self.relay
        self.query_one("#apply", Button).disabled = True
        try:
            result = await relay.host.call(relay.service.apply_delivery(review.digest))
        except Exception as exc:  # noqa: BLE001 - shown to the operator
            self.query_one("#delivery-text", Static).update(f"Apply refused: {exc}")
            return
        self.result = result
        changed = "updated" if result["changed"] else "already contained the work"
        self.query_one("#delivery-text", Static).update(
            f"APPLIED: {result['target']} {changed} at {str(result['commit'])[:12]}; "
            f"working tree {'updated' if result['installed'] else 'not checked out'}."
        )
        self.query_one("#close", Button).focus()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._prepare()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prepare":
            self._prepare()
        elif event.button.id == "apply":
            self.run_worker(self._apply, group="delivery", exclusive=True)
        else:
            self.dismiss(self.result)


_HELP_ROWS = (
    ("send", "send the composer: answer the active question, steer, or a new request"),
    ("toggle_mode", "switch the composer between answering and a new/steering request"),
    ("next_view", "next view: tasks, conversation, inspector"),
    ("view_conversation", "conversation"),
    ("view_tasks", "tasks"),
    ("view_inspector", "inspector"),
    ("plan", "review the pending plan and approve it explicitly"),
    ("deliver", "review and apply delivery (shared delivery service)"),
    ("sessions", "sessions (open, new, Ctrl+D delete)"),
    ("stop", "stop execution (drains active work; shows Stopping)"),
    ("coordinator", "coordinator selection (only qualified modes)"),
    ("help", "this help"),
    ("quit", "quit (stops and drains first; there is no detach)"),
)


def help_text() -> str:
    """Shortcut help for the effective key map; unbound actions are listed as palette-only."""
    bound = [f"  {keys.label(a):<9}{text}" for a, text in _HELP_ROWS if keys.label(a)]
    palette = [text.split(" (")[0] for a, text in _HELP_ROWS if not keys.label(a)]
    lines = ["Keyboard (everything works without a mouse; Tab/Shift+Tab move focus)", *bound]
    lines.append("  Ctrl+P   command palette: every action is listed, bound or not")
    if palette:
        lines.append("  Palette only: " + "; ".join(palette))
    lines.append("Remap under tui.keys in agents.yaml. Typing or moving never changes execution.")
    return "\n".join(lines) + "\n"


class HelpScreen(_Modal):
    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(help_text(), markup=False)
            yield Button("Close", id="close", compact=True)

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)
