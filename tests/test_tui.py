"""Keyboard-only Pilot walkthroughs of the Textual TUI (task 2.28; design 14.1, 15).

The coordinator is a scripted fake and workers use the fake adapter, so nothing calls a live
CLI or model. Every interaction is a key press: no test clicks.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot  # noqa: E402
from textual.widgets import Button, OptionList, Static, TextArea  # noqa: E402

from harness import command_line, git, init_repo  # noqa: E402
from relay.adapters.coordination import CoordinatorCall  # noqa: E402
from relay.adapters.fake import FakeAdapter  # noqa: E402
from relay.adapters.fake_scenario import Scenario  # noqa: E402
from relay.core.coordinator import CoordinatorProfile, Qualification  # noqa: E402
from relay.core.registry import Config, validate_merged  # noqa: E402
from relay.core.session_app import CoordinatorBackend, ServiceHost, SessionApp  # noqa: E402
from relay.core.sessions import SessionStore  # noqa: E402
from relay.core.specs import CoordinatorResponse  # noqa: E402
from relay.core.store import RunStore  # noqa: E402
from relay.tui import keys  # noqa: E402
from relay.tui.app import RelayApp  # noqa: E402
from relay.tui.screens import (  # noqa: E402
    AnswerScreen,
    ConfirmScreen,
    CoordinatorScreen,
    DeliveryScreen,
    HelpScreen,
    PlanScreen,
    ReviseScreen,
    SessionsScreen,
    help_text,
)

PROFILE = CoordinatorProfile("claude", "9.9.9", "test", "subscription", "digest")
QUAL = Qualification(PROFILE, "tool-free", "qualified", "test")
PASS = command_line([sys.executable, "-c", "pass"])
CHECK = {"run": PASS}
NARROW = (80, 24)
WIDE = (120, 40)


# The walkthroughs drive the TUI with these function keys; they also exercise `tui.keys`.
# The shipped defaults are covered by the `test_default_*` tests below.
LEGACY_KEYS = {
    "view_conversation": "f2",
    "view_tasks": "f3",
    "view_inspector": "f4",
    "deliver": "f6",
    "sessions": "f7",
    "stop": "f8",
    "coordinator": "f9",
}


def config(keys: dict[str, str] | None = None) -> Config:
    return validate_merged(
        {
            "version": 1,
            "tui": {"keys": LEGACY_KEYS if keys is None else keys},
            "defaults": {"concurrency": 3, "fallback": ["claude", "codex"]},
            "agents": {
                key: {"cmd": "fake", "concurrency": 3, "models": {"balanced": "default"}}
                for key in ("claude", "codex")
            },
        }
    )


def node(node_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "category": "implement",
        "goal": f"implement {node_id}",
        "owns": [f"{node_id}/**"],
        "acceptance": [CHECK],
        **extra,
    }


def plan(*nodes: dict[str, Any], why: str = "plan", change: str | None = None) -> dict[str, Any]:
    action: dict[str, Any] = {
        "action": "propose_plan",
        "explanation": why,
        "graph": {"integration_acceptance": [CHECK], "nodes": list(nodes)},
    }
    if change is not None:
        action["change_summary"] = change
    return action


def ask(text: str = "Which variant?") -> dict[str, Any]:
    return {"action": "ask_user", "questions": [{"id": "q1", "text": text}]}


class Scripted:
    """A fake coordinator: each call returns the next scripted action."""

    def __init__(self, *steps: dict[str, Any]) -> None:
        self.steps = list(steps)
        self.calls = 0

    async def __call__(self, prompt: str, **_kw: Any) -> CoordinatorCall:
        self.calls += 1
        step = self.steps.pop(0)
        response = CoordinatorResponse.model_validate({"spec_version": 1, "response": step})
        return CoordinatorCall(response, "claude", "m", "9.9.9", "digest", 1, 0.01)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "repo")


def service(
    repo: Path,
    adapter: FakeAdapter,
    script: Scripted | None,
    *,
    qualified: bool = True,
    keys: dict[str, str] | None = None,
) -> SessionApp:
    backends = {
        "claude": CoordinatorBackend("claude", PROFILE, script),
        "codex": CoordinatorBackend("codex", None, None, "Codex has no qualifying profile"),
    }
    return SessionApp(
        repo,
        config=config(keys),
        adapters={"claude": adapter, "codex": adapter},
        coordinators=backends,
        records=(QUAL,) if qualified else (),
        poll_s=0.05,
    )


Scenario_ = Callable[[RelayApp, Pilot[int]], Coroutine[Any, Any, None]]


def drive(
    repo: Path,
    svc: SessionApp,
    size: tuple[int, int],
    body: Scenario_,
    *,
    session_id: str | None = None,
) -> None:
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)

        async def main() -> None:
            app = RelayApp(host, svc, session_id=session_id, poll_s=0.05)
            async with app.run_test(size=size) as pilot:
                await body(app, pilot)

        asyncio.run(main())
    finally:
        host.close(svc.shutdown)


async def until(pilot: Pilot[int], predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            app = pilot.app
            assert isinstance(app, RelayApp)
            view = app.view_state
            notes = [str(w.content) for w in app.query(".note")]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
            raise AssertionError(
                f"condition not reached; projection {app.projection!r}, notes {notes}, "
                f"nodes {view.nodes if view else None}; service {debug_state(app.service)}; "
                f"workers {[(w.group, w.state.name, repr(w.error)) for w in app.workers]}"
            )
        await pilot.pause(0.05)


def debug_state(svc: SessionApp) -> str:
    monitor = svc._monitor  # pyright: ignore[reportPrivateUsage]
    controller = svc._controller  # pyright: ignore[reportPrivateUsage]
    state = "none" if monitor is None else ("done" if monitor.done() else "alive")
    if monitor is not None and monitor.done() and not monitor.cancelled():
        state += f" error={monitor.exception()!r}"
    running = controller.running if controller is not None else None
    return f"flags={svc._flags} monitor={state} running={running}"  # pyright: ignore[reportPrivateUsage]


async def type_text(pilot: Pilot[int], text: str) -> None:
    await pilot.press(*text)


async def send(pilot: Pilot[int], text: str) -> None:
    await type_text(pilot, text)
    await pilot.press("ctrl+s")


async def focus(pilot: Pilot[int], widget_id: str, limit: int = 12) -> None:
    """Tab until the widget with `widget_id` has focus (keyboard only)."""
    for _ in range(limit):
        focused = pilot.app.focused
        if focused is not None and focused.id == widget_id:
            return
        await pilot.press("tab")
    raise AssertionError(f"could not focus {widget_id}; focused {pilot.app.focused!r}")


async def press_button(pilot: Pilot[int], widget_id: str) -> None:
    await focus(pilot, widget_id)
    button = pilot.app.focused
    assert isinstance(button, Button) and not button.disabled, button
    await pilot.press("enter")


async def highlight(pilot: Pilot[int], list_id: str, option_id: str, limit: int = 12) -> None:
    """Move the highlight of an option list with the arrow keys until `option_id`."""
    options = pilot.app.screen.query_one(f"#{list_id}", OptionList)
    await pilot.press("home")
    for _ in range(limit):
        option = options.highlighted_option
        if option is not None and option.id == option_id:
            return
        await pilot.press("down")
    raise AssertionError(f"option {option_id} not found")


async def plan_text(app: RelayApp, pilot: Pilot[int]) -> str:
    screen = app.screen
    assert isinstance(screen, PlanScreen)
    await until(pilot, lambda: bool(screen.query("#plan-text")))
    return str(screen.query_one("#plan-text", Static).content)


def node_state(app: RelayApp, node_id: str) -> str | None:
    view = app.view_state
    if view is None:
        return None
    found = [n for n in view.nodes if n.node_id == node_id]
    return found[0].state if found else None


def composer(app: RelayApp) -> TextArea:
    return app.query_one("#composer", TextArea)


# --- walkthroughs ------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [NARROW, WIDE], ids=["80x24", "120x40"])
def test_full_session_walkthrough(repo: Path, size: tuple[int, int]) -> None:
    """Start, answer, approve, live node answer, steer/revise, then review and apply delivery."""
    adapter = FakeAdapter(
        {
            "a": [
                Scenario(status="needs_input", questions=[{"id": "q1", "text": "Name?"}]),
                Scenario(write={"a/a.py": "a = 1\n"}),
            ],
            "b": Scenario(hang=True, write={"b/partial.py": "unfinished\n"}),
            "c": Scenario(write={"c/c.py": "c = 1\n"}),
        }
    )
    script = Scripted(
        ask(),
        plan(node("a", budget={"max_attempts": 2}), node("b"), why="two parallel tasks"),
        plan(node("c"), why="finish with c", change="drop b, add c on the checkpoint"),
    )
    svc = service(repo, adapter, script)

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        assert app.projection == "No session"
        assert app.focused is composer(app)
        # Start a session with a request; the coordinator asks a question.
        await send(pilot, "Add two modules")
        await until(pilot, lambda: app.projection == "Awaiting answers")
        assert app.mode.kind == "answer"
        # Answer it; the coordinator proposes a plan and the plan review opens.
        await send(pilot, "simple")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen))
        text = await plan_text(app, pilot)
        for label in ("depends on", "scope (owns)", "candidates", "effective permissions",
                      "checks", "limits", "Pinned base", "selected context"):  # fmt: skip
            assert label in text, label
        assert app.view_state is not None and app.view_state.round_running is False
        await press_button(pilot, "approve")
        await until(pilot, lambda: node_state(app, "a") == "blocked")
        await until(pilot, lambda: node_state(app, "b") == "running")
        # A waiting node does not make unrelated execution look idle.
        assert app.projection == "Executing"
        assert app.mode.kind == "answer" and app.mode.target is not None
        assert app.mode.target.node_id == "a"
        await send(pilot, "alpha")
        await until(pilot, lambda: node_state(app, "a") == "done")
        assert node_state(app, "b") == "running"
        # Steering: Checkpoint and revise pauses admission, then cancel active work.
        assert app.mode.kind == "steer"
        await send(pilot, "Drop b and add c instead")
        await until(pilot, lambda: isinstance(app.screen, ReviseScreen))
        # Reading never sweeps the owner's files (SessionStore.open would).
        assert SessionStore(repo, str(svc.session_id)).read().admission == "closed"
        await press_button(pilot, "cancel")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen), timeout=60)
        text = await plan_text(app, pilot)
        assert "revision 2" in text and "drop b, add c" in text and "checkpoint c1" in text
        assert "primary branch unchanged" in str(app.query_one("#status").content)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownArgumentType, reportUnknownMemberType]
        await press_button(pilot, "approve")
        await until(pilot, lambda: node_state(app, "c") == "done", timeout=60)
        await until(pilot, lambda: app.projection == "Ready for review/delivery", timeout=60)
        before = git(repo, "rev-parse", "main")
        # Delivery review and apply through the shared service.
        await pilot.press("f6")
        await until(pilot, lambda: isinstance(app.screen, DeliveryScreen))
        screen = app.screen
        assert isinstance(screen, DeliveryScreen)
        await until(pilot, lambda: screen.review is not None, timeout=60)
        review = screen.review
        assert review is not None and review.applicable, review
        assert review.outcome == "passed" and set(review.cumulative_paths) == {"a/a.py", "c/c.py"}
        assert git(repo, "rev-parse", "main") == before  # nothing changed before Apply
        await press_button(pilot, "apply")
        await until(pilot, lambda: screen.result is not None, timeout=60)
        assert git(repo, "rev-parse", "main") != before
        assert (repo / "a" / "a.py").is_file() and (repo / "c" / "c.py").is_file()
        assert not (repo / "b" / "partial.py").exists()  # cancelled work never delivered
        await pilot.press("escape")

    drive(repo, svc, size, body)


@pytest.mark.parametrize("custom", [False, True], ids=["pick-option", "other"])
def test_choice_question_is_answered_from_a_list(repo: Path, custom: bool) -> None:
    question = {
        "action": "ask_user",
        "questions": [
            {"id": "q1", "text": "Which cache?", "kind": "choice", "options": ["redis", "lru"]}
        ],
    }
    script = Scripted(question, plan(node("a")))
    prompts: list[str] = []
    original = script.__call__

    async def recording(prompt: str, **kw: Any) -> CoordinatorCall:
        prompts.append(prompt)
        return await original(prompt, **kw)

    svc = service(repo, FakeAdapter({"a": Scenario(hang=True)}), recording)  # pyright: ignore[reportArgumentType]

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await send(pilot, "Add a cache")
        await until(pilot, lambda: app.projection == "Awaiting answers")
        await pilot.press("ctrl+s")  # empty composer opens the answer screen
        await until(pilot, lambda: isinstance(app.screen, AnswerScreen))
        picker = app.screen.query_one("#choice-0", OptionList)
        await until(pilot, lambda: picker.option_count == 3)
        if custom:
            await pilot.press("end", "enter")  # Enter on Other moves to its text field
            await type_text(pilot, "memcached")
        else:
            await pilot.press("down")  # redis -> lru
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen))
        await pilot.press("enter")  # Approve is focused: Enter approves the revision
        await until(pilot, lambda: node_state(app, "a") == "running")
        await pilot.press("ctrl+q")

    drive(repo, svc, WIDE, body)
    assert "memcached" in prompts[-1] if custom else "lru" in prompts[-1]


def test_review_results_are_shown_when_a_read_only_round_finishes(repo: Path) -> None:
    """A review changes no files, so its summary is the result: it must reach the screen."""
    adapter = FakeAdapter({"r": Scenario(summary="FINDING: run.py:1088 rejects custom answers")})
    svc = service(repo, adapter, Scripted(plan(node("r", category="review", owns=[]))))

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await send(pilot, "Review it")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen))
        await press_button(pilot, "approve")
        await until(pilot, lambda: node_state(app, "r") == "done", timeout=60)
        await until(
            pilot,
            lambda: any("FINDING: run.py:1088" in text for _, text in (app.view_state.notices if app.view_state else ())),  # noqa: E501
        )
        detail = await svc.node_detail("r")
        assert "FINDING: run.py:1088" in detail.report
        # A finished round does not pin the session: switching to a new one works.
        first = app.view_state.session_id if app.view_state else None
        await pilot.press("f7")
        await until(pilot, lambda: isinstance(app.screen, SessionsScreen))
        await highlight(pilot, "sessions", "new")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: app.view_state is not None and app.view_state.session_id not in (None, first),
        )
        await pilot.press("ctrl+q")

    drive(repo, svc, WIDE, body)


def test_stop_shows_stopping_and_reopen_never_dispatches(repo: Path) -> None:
    adapter = FakeAdapter({"a": [Scenario(hang=True), Scenario(write={"a/a.py": "a\n"})]})
    script = Scripted(plan(node("a", budget={"max_attempts": 2})))
    svc = service(repo, adapter, script)
    seen: list[str] = []
    session: list[str] = []

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await send(pilot, "Add a")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen))
        await press_button(pilot, "approve")
        await until(pilot, lambda: node_state(app, "a") == "running")
        await pilot.press("f8")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        assert app.focused is not None and app.focused.id == "cancel"  # safe default
        await press_button(pilot, "confirm")
        seen.append(app.projection)
        await until(pilot, lambda: app.projection == "Interrupted", timeout=60)
        view = app.view_state
        assert view is not None and view.recovery and view.owned
        session.append(str(view.session_id))
        await pilot.press("ctrl+q")

    drive(repo, svc, WIDE, body)
    assert seen == ["Stopping"]
    sid = session[0]
    record = SessionStore.open(repo, sid).read()
    run_id = record.rounds[0].run_id
    assert RunStore.open(repo, run_id).read_run().state == "interrupted"
    calls = len(adapter.invocations)

    # Reopen: state and recovery requirements show; nothing dispatches by itself.
    second = FakeAdapter({"a": Scenario(write={"a/a.py": "a\n"})})
    svc2 = service(repo, second, Scripted())

    async def reopen(app: RelayApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.view_state is not None and app.view_state.owned)
        await until(pilot, lambda: app.projection == "Interrupted")
        await pilot.pause(1.0)
        assert len(adapter.invocations) == calls and not second.invocations
        assert RunStore.open(repo, run_id).read_run().state == "interrupted"
        live = str(app.query_one("#live").content)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownArgumentType, reportUnknownMemberType]
        assert "RECOVERY REQUIRED" in live and "Continue round" in live
        # The explicit continuation is a focusable control.
        await press_button(pilot, "act-continue")
        await until(pilot, lambda: app.projection == "Ready for review/delivery", timeout=60)
        assert len(adapter.invocations) == calls and len(second.invocations) == 1
        await pilot.press("ctrl+q")

    drive(repo, svc2, NARROW, reopen, session_id=sid)


def test_resize_keeps_focus_draft_and_selection(repo: Path) -> None:
    adapter = FakeAdapter({"a": Scenario(hang=True), "b": Scenario(hang=True)})
    svc = service(repo, adapter, Scripted(plan(node("a"), node("b"))))

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await send(pilot, "Two tasks")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen))
        await press_button(pilot, "approve")
        await until(pilot, lambda: node_state(app, "b") == "running")
        await type_text(pilot, "draft text")
        # 80x24: only one main view is shown; F3 switches to tasks.
        assert app.narrow and app.current_view == "conversation"
        assert not app.query_one("#tasks").display
        await pilot.press("f3")
        assert app.query_one("#tasks").display and not app.query_one("#conversation").display
        await pilot.press("down")
        tasks = app.query_one("#tasks")
        selected = tasks.highlighted  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType]
        assert app.focused is tasks
        await pilot.resize_terminal(*WIDE)
        await pilot.pause(0.2)
        assert not app.narrow
        assert all(app.query_one(f"#{v}").display for v in ("tasks", "conversation", "inspector"))
        assert app.focused is tasks and tasks.highlighted == selected  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        assert composer(app).text == "draft text"
        await pilot.resize_terminal(*NARROW)
        await pilot.pause(0.2)
        assert app.narrow and app.current_view == "tasks" and app.focused is tasks
        assert composer(app).text == "draft text"
        # Navigating never changes execution.
        assert node_state(app, "a") == "running" and node_state(app, "b") == "running"
        await pilot.press("f4")
        assert app.query_one("#inspector").display
        await until(pilot, lambda: "Exact handoff delivered" in app.query_one("#inspector").text)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
        # Switching away from an executing session needs a stop first; inspecting does not.
        current = app.view_state.session_id if app.view_state else None
        await pilot.press("f7")
        await until(pilot, lambda: isinstance(app.screen, SessionsScreen))
        await highlight(pilot, "sessions", "new")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("stop execution (F8)" in str(w.content) for w in app.query(".note")),  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownArgumentType]
        )
        assert app.view_state is not None and app.view_state.session_id == current
        assert node_state(app, "a") == "running"
        await pilot.press("ctrl+q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await press_button(pilot, "confirm")
        await until(pilot, lambda: app.projection == "Stopping" or not app.is_running)

    drive(repo, svc, NARROW, body)


def test_ctrl_c_never_quits_or_stops_execution(repo: Path) -> None:
    """In a Windows console Ctrl+C reaches Textual as a key, not SIGINT (task 2.29).

    It must neither quit nor stop a round: only Ctrl+Q (with confirmation while busy) or F8
    do, and both drain execution before ownership is released.
    """
    adapter = FakeAdapter({"a": Scenario(hang=True)})
    svc = service(repo, adapter, Scripted(plan(node("a"))))

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await send(pilot, "Add a")
        await until(pilot, lambda: isinstance(app.screen, PlanScreen))
        await press_button(pilot, "approve")
        await until(pilot, lambda: node_state(app, "a") == "running")
        await pilot.press("f3")  # outside the composer, where Ctrl+C is not copy
        await pilot.press("ctrl+c")
        await pilot.pause(0.5)
        assert app.is_running and not isinstance(app.screen, ConfirmScreen)
        view = app.view_state
        assert view is not None and view.owned and view.round_running
        assert node_state(app, "a") == "running"
        await pilot.press("ctrl+q")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await press_button(pilot, "confirm")
        await until(pilot, lambda: app.projection == "Stopping" or not app.is_running)

    drive(repo, svc, WIDE, body)
    record = SessionStore.open(repo, SessionStore.list_sessions(repo)[0]).read()
    assert RunStore.open(repo, record.rounds[0].run_id).read_run().state == "interrupted"


def test_unavailable_coordinator_is_explained_not_fatal(repo: Path) -> None:
    svc = service(repo, FakeAdapter(), Scripted(), qualified=False)

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await until(pilot, lambda: app.view_state is not None)
        assert "coordination unavailable" in app.mode.label
        await send(pilot, "Do something")
        await until(
            pilot,
            lambda: any(
                "coordination is unavailable" in str(w.content)  # pyright: ignore[reportAttributeAccessIssue, reportUnknownArgumentType, reportUnknownMemberType]
                for w in app.query(".note")
            ),
        )
        assert composer(app).text == "Do something"  # the draft is kept
        await pilot.press("f9")
        await until(pilot, lambda: isinstance(app.screen, CoordinatorScreen))
        listed = app.screen.query_one("#coordinators")
        prompts = [str(o.prompt) for o in listed.options]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType, reportUnknownVariableType, reportUnknownArgumentType]
        assert any("claude [unavailable]" in p and "qualification record" in p for p in prompts)
        assert any("codex [unavailable]" in p for p in prompts)
        await pilot.press("escape")
        await pilot.press("ctrl+q")

    drive(repo, svc, NARROW, body)


def test_session_picker_switches_and_deletes_with_confirmation(repo: Path) -> None:
    old = SessionStore.create(repo).session_id
    svc = service(repo, FakeAdapter(), Scripted())

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        await pilot.press("f7")
        await until(pilot, lambda: isinstance(app.screen, SessionsScreen))
        await highlight(pilot, "sessions", old)
        await pilot.press("enter")
        await until(pilot, lambda: app.view_state is not None and app.view_state.session_id == old)
        await pilot.press("f7")
        await until(pilot, lambda: isinstance(app.screen, SessionsScreen))
        await highlight(pilot, "sessions", "new")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: app.view_state is not None and app.view_state.session_id not in (None, old),
        )
        await pilot.press("f7")
        await until(pilot, lambda: isinstance(app.screen, SessionsScreen))
        await highlight(pilot, "sessions", old)
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await press_button(pilot, "confirm")
        await until(pilot, lambda: old not in SessionStore.list_sessions(repo))
        await pilot.press("ctrl+q")

    drive(repo, svc, NARROW, body)
    assert old not in SessionStore.list_sessions(repo)


# --- shortcut configuration ----------------------------------------------------------------------


def test_default_keys_use_one_function_key_and_a_few_chords(repo: Path) -> None:
    svc = service(repo, FakeAdapter(), Scripted(), keys={})

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        bound = {action for _, action, _ in keys.bindings()}
        assert bound == {"quit", "send", "toggle_mode", "help", "next_view", "plan", "stop"}
        assert all(chord == "f1" or chord.startswith("ctrl+") for chord, _, _ in keys.bindings())
        # Ctrl+N walks tasks -> conversation -> inspector -> tasks.
        order = ["tasks", "conversation", "inspector"]
        app.action_view("tasks")
        seen: list[str | None] = []
        for _ in range(4):
            await pilot.press("ctrl+n")
            seen.append(app._pane_of(app.focused))  # pyright: ignore[reportPrivateUsage]
        assert seen == ["conversation", "inspector", "tasks", "conversation"]
        assert set(seen) <= set(order)
        await pilot.press("f1")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen))
        text = help_text()
        assert "Ctrl+R" in text and "Ctrl+G" in text and "F2" not in text
        assert "Palette only:" in text

    drive(repo, svc, WIDE, body)


def test_unbound_actions_stay_reachable_from_the_palette(repo: Path) -> None:
    svc = service(repo, FakeAdapter(), Scripted(), keys={"stop": ""})

    async def body(app: RelayApp, pilot: Pilot[int]) -> None:
        assert "stop" not in {a for _, a, _ in keys.bindings()}
        titles = {c.title for c in app.get_system_commands(app.screen)}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        assert {"Stop execution", "Sessions", "Review delivery", "Select coordinator"} <= titles

    drive(repo, svc, WIDE, body)


def test_bad_key_configuration_is_rejected() -> None:
    for bad in ({"nope": "ctrl+x"}, {"stop": "Ctrl R"}, {"stop": "enter"}, {"stop": "ctrl+r"}):
        with pytest.raises(Exception, match="tui key|unknown"):
            config(bad)
