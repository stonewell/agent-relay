"""The Relay Textual application (design 14.1, 15; task 2.28).

`RelayApp` projects `SessionApp` views and submits operator commands to it through a
`ServiceHost`. Execution belongs to the service on its own loop thread; Textual workers here
only await results, so closing a screen or cancelling a worker never cancels Relay work. Stop
and quit go through the service's explicit drain path and show `Stopping` until it finishes.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, OptionList, Static, TextArea

from relay.core.session_app import (
    NodeDetail,
    ServiceHost,
    SessionApp,
    SessionView,
    TurnResult,
)
from relay.core.sessions import SessionDeletionPlan

from . import keys
from .projection import Action, ComposerMode, composer_mode, pending_actions, project
from .screens import (
    AnswerScreen,
    ConfirmScreen,
    CoordinatorScreen,
    DeliveryScreen,
    HelpScreen,
    PlanScreen,
    ReviseScreen,
    SessionsScreen,
)
from .widgets import Conversation, Inspector, TaskList

__all__ = ["WIDE_MIN", "RelayApp", "run_tui"]

T = TypeVar("T")

WIDE_MIN = 110
"""At this width or more the sidebar, conversation and inspector are side by side."""

VIEWS = ("conversation", "tasks", "inspector")
ACTION_IDS = ("plan", "stop", "continue", "resume-turn", "deliver", "coordinator", "sessions")

CSS = """
Screen { layout: vertical; }
#status { height: 1; background: $boost; padding: 0 1; }
#main { height: 1fr; }
#tasks { width: 30; border: round $panel; }
#conversation { width: 1fr; border: round $panel; padding: 0 1; }
#inspector { width: 42; border: round $panel; padding: 0 1; }
.-narrow #tasks, .-narrow #inspector { width: 1fr; }
#tasks:focus, #conversation:focus, #inspector:focus { border: round $accent; }
#mode { height: 1; padding: 0 1; color: $text-muted; }
#composer { height: 5; }
#composer:focus { border: tall $accent; }
#actions { height: 1; }
#actions Button { margin-right: 1; }
Button:focus { text-style: bold reverse; }
.note { color: $text-muted; }
.plan-card { border-left: thick $success; padding-left: 1; }
.question-card { border-left: thick $warning; padding-left: 1; }
.live { margin-top: 1; }
.message { margin-bottom: 1; }
ModalScreen { align: center middle; }
.dialog { width: 76; height: auto; max-height: 90%; border: thick $accent;
          background: $surface; padding: 0 1; }
.dialog.wide { width: 95%; height: 90%; }
#plan-body, #delivery-body { height: 1fr; }
.buttons { height: 1; margin-top: 1; }
.buttons Button { margin-right: 2; }
.target { height: 1; }
.target Label { width: 16; }
.target Input { width: 1fr; height: 1; border: none; }
#sessions, #coordinators { height: auto; max-height: 16; }
"""


class RelayApp(App[int]):
    """Conversation, composer, task sidebar and inspector over one session service."""

    CSS = CSS
    TITLE = "relay"

    def __init__(
        self,
        host: ServiceHost,
        service: SessionApp,
        *,
        session_id: str | None = None,
        poll_s: float = 0.2,
    ) -> None:
        super().__init__()
        # Shortcuts come from `tui.keys` (core/keymap.py defaults). They are priority bindings
        # because TextArea owns some keys itself; `check_action` disables them in dialogs.
        keys.configure(service.config.tui.keys)
        for chord, action, description in keys.bindings():
            self._bindings.bind(chord, action, description, priority=True)  # pyright: ignore[reportPrivateUsage]
        self.host = host
        self.service = service
        self.view_state: SessionView | None = None
        self.current_view = "conversation"
        self.narrow = False
        self._reopen = session_id
        self._poll_s = poll_s
        self._prefer_request = False
        self._stopping = False
        self._busy: set[str] = set()
        self._actions: tuple[Action, ...] = ()
        self._detail_for: str | None = None
        self._notes = 0

    # --- layout -------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("State: starting", id="status", markup=False)
        with Horizontal(id="main"):
            yield TaskList(id="tasks")
            yield Conversation(id="conversation")
            yield Inspector(id="inspector")
        yield Static("", id="mode", markup=False)
        yield TextArea(id="composer", soft_wrap=True, tab_behavior="focus")
        with Horizontal(id="actions"):
            for action_id in ACTION_IDS:
                yield Button(action_id, id=f"act-{action_id}", compact=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one(
            "#conversation", Conversation
        ).border_title = f"Conversation{keys.hint('view_conversation')}"
        self.query_one("#composer", TextArea).border_title = "Composer"
        self._layout(self.size.width)
        for action_id in ACTION_IDS:
            self.query_one(f"#act-{action_id}", Button).display = False
        self.query_one("#composer", TextArea).focus()
        if self._reopen is not None:
            session_id = self._reopen
            self._op("open session", lambda: self.host.call(self.service.open(session_id)))
        self.set_interval(self._poll_s, self._schedule_refresh)
        self._schedule_refresh()

    def on_resize(self, event: Any) -> None:
        self._layout(event.size.width)

    def _pane_of(self, widget: Widget | None) -> str | None:
        while widget is not None:
            if widget.id in VIEWS:
                return widget.id
            parent = widget.parent
            widget = parent if isinstance(parent, Widget) else None
        return None

    def _layout(self, width: int) -> None:
        narrow = width < WIDE_MIN
        if narrow and not self.narrow:
            # Keep the focused pane visible so focus, drafts and selection survive the resize.
            self.current_view = self._pane_of(self.focused) or self.current_view
        self.narrow = narrow
        self.screen.set_class(narrow, "-narrow")
        for name in VIEWS:
            self.query_one(f"#{name}").display = not narrow or name == self.current_view

    def action_next_view(self) -> None:
        """Move to the next pane in on-screen order (tasks, conversation, inspector), wrapping."""
        order = ("tasks", "conversation", "inspector")
        current = self._pane_of(self.focused) or self.current_view
        index = order.index(current) if current in order else -1
        self.action_view(order[(index + 1) % len(order)])

    def action_view(self, name: str) -> None:
        self.current_view = name
        self._layout(self.size.width)
        self.query_one(f"#{name}").focus()

    # --- actions availability -------------------------------------------------------------------

    _MAIN_ONLY = frozenset(
        {
            "send",
            "toggle_mode",
            "view",
            "next_view",
            "plan",
            "deliver",
            "sessions",
            "stop",
            "coordinator",
        }
    )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """The main-screen shortcuts are disabled while a dialog is open."""
        if action in self._MAIN_ONLY:
            return not isinstance(self.screen, ModalScreen)
        return True

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)  # pyright: ignore[reportUnknownMemberType]
        commands: list[tuple[str, str, Callable[[], Any]]] = [
            (
                "Send composer",
                f"Answer, steer or send a request{keys.hint('send')}",
                self.action_send,
            ),
            (
                "Review plan",
                f"Review the pending plan and approve it{keys.hint('plan')}",
                self.action_plan,
            ),
            (
                "Review delivery",
                f"Prepare, verify and apply delivery{keys.hint('deliver')}",
                self.action_deliver,
            ),
            (
                "Sessions",
                f"Open, create or delete sessions{keys.hint('sessions')}",
                self.action_sessions,
            ),
            (
                "Stop execution",
                f"Cancel and drain active work{keys.hint('stop')}",
                self.action_stop,
            ),
            (
                "Select coordinator",
                f"Qualified coordinator modes{keys.hint('coordinator')}",
                self.action_coordinator,
            ),
            ("Continue round", "Explicitly continue an interrupted round", self.action_continue),
            ("Resume turn", "Continue an interrupted coordinator turn", self.action_resume_turn),
            ("Restore round", "Restore the superseded round", self.action_restore),
            ("Coordinator review", "Ask the coordinator to review the round", self.action_review),
            ("Pause admission", "Close the gate: no new task starts", self.action_pause),
            ("Resume admission", "Reopen the gate", self.action_resume_admission),
            ("Cancel task", "Cancel the selected task", self.action_cancel_task),
            ("Cancel round", "Cancel the whole round and wait for it", self.action_cancel_round),
            (
                "Show conversation",
                keys.label("view_conversation"),
                lambda: self.action_view("conversation"),
            ),
            ("Show tasks", keys.label("view_tasks"), lambda: self.action_view("tasks")),
            ("Show inspector", keys.label("view_inspector"), lambda: self.action_view("inspector")),
            ("Next view", keys.label("next_view"), self.action_next_view),
            ("Shortcut help", keys.label("help"), self.action_help),
        ]
        for title, help_text, callback in commands:
            yield SystemCommand(title, help_text, callback)

    # --- service calls --------------------------------------------------------------------------

    def _op(
        self,
        label: str,
        call: Callable[[], Awaitable[T]],
        done: Callable[[T], None] | None = None,
    ) -> None:
        """Await a service operation without blocking the UI; errors are shown, not raised."""

        async def run() -> None:
            self._busy.add(label)
            try:
                result = await call()
            except Exception as exc:  # noqa: BLE001 - every failure is shown to the operator
                self.note(f"{label}: {exc}", error=True)
                return
            finally:
                self._busy.discard(label)
                self._schedule_refresh()
            if done is not None:
                try:
                    done(result)
                except Exception as exc:  # noqa: BLE001 - a projection bug must stay visible
                    self.note(f"{label}: {exc}", error=True)

        self.run_worker(run, group="ops", exit_on_error=False)

    def note(self, text: str, *, error: bool = False) -> None:
        self._notes += 1
        self.query_one("#conversation", Conversation).note(
            f"local:{self._notes}", text, error=error
        )
        self.notify(text, severity="error" if error else "information", timeout=4)

    def _schedule_refresh(self) -> None:
        self.run_worker(self._refresh, group="refresh", exclusive=True, exit_on_error=False)

    async def _refresh(self) -> None:
        if self.host.closed:
            return
        view = await self.host.call(self.service.view())
        previous = self.view_state
        self.view_state = view
        if previous is not None and previous.session_id != view.session_id:
            self.query_one("#conversation", Conversation).reset()
            self._detail_for = None
        if previous is None or previous.stamp != view.stamp:
            self._render(view)
        else:
            self._render_status()

    def _render(self, view: SessionView) -> None:
        self.query_one("#conversation", Conversation).show(view)
        tasks = self.query_one("#tasks", TaskList)
        tasks.show(view.nodes)
        selected = tasks.selected
        if selected is None:
            self.query_one("#inspector", Inspector).show_session(view)
        else:
            self._load_detail(selected)
        self._render_status()
        self._render_actions(view)

    def _render_status(self) -> None:
        view = self.view_state
        projection = project(view, stopping=self._stopping)
        status = projection.text
        if view is not None and view.session_id:
            status += f" | session {view.session_id}"
            if view.base.primary_unchanged:
                status += " | primary branch unchanged"
        self.query_one("#status", Static).update(status)
        self.query_one("#mode", Static).update(self.mode.label)

    def _render_actions(self, view: SessionView) -> None:
        actions = tuple(pending_actions(view))
        if actions == self._actions:
            return
        self._actions = actions
        # A fixed set of buttons whose visibility changes: nothing is removed under focus.
        shown = {a.id: a.label for a in actions}
        focused = self.focused
        for action_id in ACTION_IDS:
            button = self.query_one(f"#act-{action_id}", Button)
            button.display = action_id in shown
            if action_id in shown:
                button.label = shown[action_id]
            elif focused is button:
                self.query_one("#composer", TextArea).focus()

    @property
    def projection(self) -> str:
        return project(self.view_state, stopping=self._stopping).state

    @property
    def mode(self) -> ComposerMode:
        tasks = self.query_one("#tasks", TaskList)
        return composer_mode(
            self.view_state, prefer_request=self._prefer_request, selected=tasks.selected
        )

    # --- task selection -------------------------------------------------------------------------

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if isinstance(event.option_list, TaskList) and event.option_id is not None:
            self._load_detail(event.option_id)
            self._render_status()

    def _load_detail(self, node_id: str) -> None:
        self._detail_for = node_id

        def shown(detail: NodeDetail) -> None:
            if self._detail_for == detail.node_id:
                self.query_one("#inspector", Inspector).show_node(detail)

        self._op("inspect", lambda: self.host.call(self.service.node_detail(node_id)), shown)

    # --- composer -------------------------------------------------------------------------------

    def action_toggle_mode(self) -> None:
        self._prefer_request = not self._prefer_request
        self._render_status()

    def action_send(self) -> None:
        composer = self.query_one("#composer", TextArea)
        text = composer.text
        mode = self.mode
        if mode.kind == "answer" and mode.target is not None:
            target = mode.target
            if len(target.questions) == 1 and text.strip() and not target.questions[0].options:
                self._answer(target.source, target.node_id, target.revision,
                             {target.questions[0].id: text.strip()})  # fmt: skip
                return

            def answered(answers: dict[str, str | list[str]] | None) -> None:
                if answers is not None:
                    self._answer(target.source, target.node_id, target.revision, answers)

            self.push_screen(AnswerScreen(target, text.strip()), answered)
            return
        if not text.strip():
            self.note("type a request first", error=True)
            return
        if mode.kind == "steer":
            self._steer(text)
            return

        view = self.view_state
        if view is not None and view.coordinator is None:
            # Explain instead of creating an empty session that cannot coordinate.
            self._op("request", lambda: self.host.call(self.service.submit(text)))
            return

        async def request() -> TurnResult:
            if self.service.session_id is None:
                await self.host.call(self.service.open(None))
            return await self.host.call(self.service.submit(text))

        self._op("request", request, self._turn_done)

    def _clear_composer(self) -> None:
        self.query_one("#composer", TextArea).clear()
        self._prefer_request = False

    def _turn_done(self, result: TurnResult) -> None:
        self._clear_composer()
        if result.status == "awaiting_approval" and result.plan_revision is not None:
            self.note(
                f"plan revision {result.plan_revision} proposed; review it{keys.hint('plan')}"
            )
            self.action_plan()
        elif result.status == "awaiting_answers":
            self.note("the coordinator asked questions; answer them in the composer")
        elif result.status == "failed":
            self.note(f"the coordinator turn failed: {result.error}", error=True)
        elif result.summary:
            self.note(f"coordinator {result.action}: {result.summary}")

    def _answer(
        self,
        source: str,
        node_id: str | None,
        revision: int,
        answers: dict[str, str | list[str]],
    ) -> None:
        if source == "node" and node_id is not None:
            self._op(
                f"answer {node_id}",
                lambda: self.host.call(self.service.answer_node(node_id, revision, answers)),
                lambda _command: self._answered(node_id),
            )
            return
        self._op(
            "answer",
            lambda: self.host.call(self.service.answer_coordinator(revision, answers)),
            self._turn_done,
        )

    def _answered(self, node_id: str) -> None:
        self._clear_composer()
        self.note(f"answer recorded for {node_id}; independent work continues")

    def _steer(self, text: str) -> None:
        def paused(_: None) -> None:
            view = self.view_state
            active = [n.node_id for n in view.nodes if n.state == "running"] if view else []
            self._render_status()
            self.push_screen(ReviseScreen(active), chosen)

        def chosen(choice: str | None) -> None:
            if choice in ("finish", "cancel"):
                self.note(
                    "checkpointing: "
                    + ("cancelling active work" if choice == "cancel" else "letting it finish")
                )
                self._op(
                    "checkpoint and revise",
                    lambda: self.host.call(
                        self.service.checkpoint_and_revise(text, cancel_active=choice == "cancel")
                    ),
                    lambda result: self._turn_done(result) if result else self._clear_composer(),
                )
            else:
                self._op(
                    "resume admission", lambda: self.host.call(self.service.resume_admission())
                )

        self.note("admission is pausing: no new task will start")
        self._op("pause admission", lambda: self.host.call(self.service.pause_admission()), paused)

    # --- commands -------------------------------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_plan(self) -> None:
        def show(plan: Any) -> None:
            self.push_screen(PlanScreen(plan), approved)

        def approved(revision: int | None) -> None:
            if revision is None:
                return
            self._op(
                f"approve revision {revision}",
                lambda: self.host.call(self.service.approve(revision)),
                lambda run_id: self.note(f"revision {revision} approved; round {run_id} started"),
            )

        view = self.view_state
        revision = view.pending_plan.revision if view and view.pending_plan else None
        self._op("plan", lambda: self.host.call(self.service.plan(revision)), show)

    def action_deliver(self) -> None:
        self.push_screen(DeliveryScreen(), lambda _result: self._schedule_refresh())

    def action_sessions(self) -> None:
        def show(sessions: Any) -> None:
            self.push_screen(SessionsScreen(sessions, self.service.session_id), picked)

        def picked(choice: tuple[str, str] | None) -> None:
            if choice is None:
                return
            verb, session_id = choice
            view = self.view_state
            busy = view is not None and (
                view.round_running or view.coordinating or view.checkpointing
            )
            if verb == "open":
                if busy and session_id != (view.session_id if view else None):
                    self.note(
                        f"stop execution{keys.hint('stop')} before switching sessions; "
                        "inspecting tasks does not need that",
                        error=True,
                    )
                    return
                target = None if session_id == "new" else session_id
                self._op("open session", lambda: self.host.call(self.service.open(target)))
                return

            def planned(plan: SessionDeletionPlan) -> None:
                if plan.problems:
                    self.note(
                        f"session {session_id} cannot be deleted now: " + "; ".join(plan.problems),
                        error=True,
                    )
                    return
                undelivered = bool(plan.undelivered)

                def confirmed(ok: bool | None) -> None:
                    if ok:
                        self._op(
                            f"delete {session_id}",
                            lambda: self.host.call(
                                self.service.delete(session_id, include_undelivered=undelivered)
                            ),
                            lambda done: self.note(
                                f"session {session_id} deleted with {len(done.runs)} run(s) "
                                f"and {len(done.refs)} session ref(s)"
                            ),
                        )

                label = "Delete session and undelivered rounds" if undelivered else "Delete session"
                self.push_screen(ConfirmScreen(deletion_text(plan), label), confirmed)

            self._op(
                "plan deletion",
                lambda: self.host.call(self.service.deletion_plan(session_id)),
                planned,
            )

        self._op("sessions", lambda: self.host.call(self.service.sessions()), show)

    def action_coordinator(self) -> None:
        def show(options: Any) -> None:
            self.push_screen(CoordinatorScreen(options), chosen)

        def chosen(agent: str | None) -> None:
            if agent is not None:
                self._op(
                    "select coordinator",
                    lambda: self.host.call(self.service.select_coordinator(agent)),
                    lambda option: self.note(
                        f"coordinator: {option.agent}"
                        + (f" ({option.mode})" if option.mode else "")
                    ),
                )

        view = self.view_state
        if view is not None:
            show(view.coordinators)

    def action_stop(self) -> None:
        view = self.view_state
        if view is None or not (view.round_running or view.coordinating or view.checkpointing):
            self.note("nothing is executing")
            return

        def confirmed(ok: bool | None) -> None:
            if not ok:
                return
            self._stopping = True
            self._render_status()

            async def stop() -> None:
                try:
                    await self.host.call(self.service.stop())
                finally:
                    self._stopping = False

            self._op("stop", stop, lambda _: self.note("stopped; the session is kept"))

        self.push_screen(
            ConfirmScreen(
                "Stop execution?\n\nAdmission closes, active work is cancelled and drained, "
                "and the round stays interrupted for an explicit continuation.",
                "Stop",
            ),
            confirmed,
        )

    async def action_quit(self) -> None:
        view = self.view_state
        busy = view is not None and (
            view.round_running or view.coordinating or view.checkpointing or view.applying
        )

        def leave(ok: bool | None) -> None:
            if not ok:
                return
            self._stopping = True
            self._render_status()

            async def close() -> None:
                with contextlib.suppress(Exception):
                    await self.host.call(self.service.shutdown())
                self.exit(0)

            self.run_worker(close, group="quit")

        if busy:
            self.push_screen(
                ConfirmScreen(
                    "Quit?\n\nExecution is stopped and drained, and a delivery being applied "
                    "finishes, before the application closes (there is no detach).",
                    "Stop and quit",
                ),
                leave,
            )
        else:
            leave(True)

    def action_continue(self) -> None:
        self._op(
            "continue round",
            lambda: self.host.call(self.service.continue_round()),
            lambda run_id: self.note(f"round {run_id} continues"),
        )

    def action_resume_turn(self) -> None:
        view = self.view_state
        turns = [t for t in view.turns if t.status == "interrupted"] if view else []
        if not turns:
            self.note("no interrupted coordinator turn")
            return
        turn_id = turns[-1].turn_id
        self._op(
            "resume turn",
            lambda: self.host.call(self.service.resume_turn(turn_id)),
            self._turn_done,
        )

    def action_restore(self) -> None:
        view = self.view_state
        rounds = [r for r in view.rounds if r.round_state == "superseded"] if view else []
        if not rounds:
            self.note("no superseded round to restore")
            return
        run_id = rounds[-1].run_id
        self._op(
            "restore round",
            lambda: self.host.call(self.service.restore_round(run_id)),
            lambda _: self.note(f"round {run_id} restored"),
        )

    def action_review(self) -> None:
        self._op("review", lambda: self.host.call(self.service.review_round()), self._turn_done)

    def action_pause(self) -> None:
        self._op("pause admission", lambda: self.host.call(self.service.pause_admission()))

    def action_resume_admission(self) -> None:
        self._op("resume admission", lambda: self.host.call(self.service.resume_admission()))

    def action_cancel_task(self) -> None:
        node_id = self.query_one("#tasks", TaskList).selected
        if node_id is None:
            self.note(f"select a task first{keys.hint('next_view')}")
            return
        self._op(f"cancel {node_id}", lambda: self.host.call(self.service.cancel_node(node_id)))

    def action_cancel_round(self) -> None:
        self._op("cancel round", lambda: self.host.call(self.service.cancel_round()))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers: dict[str, Callable[[], Any]] = {
            "act-plan": self.action_plan,
            "act-stop": self.action_stop,
            "act-continue": self.action_continue,
            "act-resume-turn": self.action_resume_turn,
            "act-deliver": self.action_deliver,
            "act-coordinator": self.action_coordinator,
            "act-sessions": self.action_sessions,
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            handler()


def deletion_text(plan: SessionDeletionPlan) -> str:
    """The exact confirmation for deleting a session and its runs (design section 6)."""
    lines = [
        f"Delete session {plan.session_id} and its runs?",
        "",
        "This permanently deletes:",
        f"- the session's records and its {len(plan.session_refs)} checkpoint ref(s)",
    ]
    if not plan.runs:
        lines.append("- no runs (the session has none)")
    for run in plan.runs:
        mark = "delivered" if run.delivery == "delivered" else "NOT DELIVERED"
        if run.delivery == "unknown":
            mark = "APPLIED STATE UNKNOWN"
        lines.append(
            f"- run {run.run_id} [{mark}: {run.detail}]: its record, {len(run.refs)} relay "
            f"ref(s), {run.worktrees} worktree(s)" + (" and scratch" if run.scratch else "")
        )
    if plan.undelivered:
        lines += [
            "",
            f"{len(plan.undelivered)} round(s) were not delivered (or cannot be checked): their "
            "work is lost and cannot be applied or resumed afterwards.",
        ]
    lines += ["", "Your own branches and working tree are not changed."]
    return "\n".join(lines)


def _is_git_repo(repo_root: Path) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=repo_root, capture_output=True, text=True
    )
    return proc.returncode == 0


def run_tui(repo_root: Path, *, session_id: str | None = None) -> int:
    """Run the TUI in the foreground; execution is drained before this returns."""
    import sys

    from relay.core.registry import load_config

    if not _is_git_repo(repo_root):
        print(f"relay tui: {repo_root} is not a git repository; run git init", file=sys.stderr)
        return 2
    try:
        config = load_config(repo_root).config
    except (ValueError, OSError) as exc:
        print(f"relay tui: invalid configuration: {exc}", file=sys.stderr)
        return 2
    service = SessionApp(repo_root, config=config)
    host = ServiceHost().start()
    try:
        host.submit(service.start()).result()
        app = RelayApp(host, service, session_id=session_id)
        code = app.run()
    finally:
        host.close(service.shutdown, still_running=lambda: service.executing)
    return int(code or 0)
