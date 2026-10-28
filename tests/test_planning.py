"""Coordinator planning, review and approval loop (task 2.24)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from harness import git, init_repo
from relay.adapters.coordination import CoordinatorCall, CoordinatorInvalid
from relay.core.coordinator import CoordinationLimits, CoordinatorProfile, Qualification
from relay.core.planning import PlanningEngine, PlanningSetup, validate_plan
from relay.core.registry import Config, validate_merged
from relay.core.sessions import SessionOwner, SessionStore
from relay.core.specs import CoordinatorResponse, ResultSpec
from relay.core.store import RunStore

PROFILE = CoordinatorProfile("claude", "9.9.9", "test", "subscription", "digest")
QUAL = Qualification(PROFILE, "tool-free", "qualified", "test")
CHECK = {"run": "python -c pass", "expect_exit": 0}


def plan_graph(**node: Any) -> dict[str, Any]:
    base = {
        "id": "n1",
        "category": "implement",
        "goal": "do it",
        "owns": ["src/**"],
        "acceptance": [CHECK],
    }
    return {"integration_acceptance": [CHECK], "nodes": [{**base, **node}]}


def respond(action: dict[str, Any]) -> CoordinatorCall:
    response = CoordinatorResponse.model_validate({"spec_version": 1, "response": action})
    return CoordinatorCall(response, "claude", "m", "9.9.9", "digest", 1, 0.01)


def propose(graph: dict[str, Any] | None = None, why: str = "two steps") -> dict[str, Any]:
    return {"action": "propose_plan", "explanation": why, "graph": graph or plan_graph()}


class Scripted:
    """A fake coordinator: each call pops the next scripted step."""

    def __init__(self, *steps: Any) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, **_kw: Any) -> CoordinatorCall:
        self.prompts.append(prompt)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if isinstance(step, (int, float)):
            await asyncio.sleep(step)
            return respond({"action": "finish", "summary": "late"})
        return respond(step)


def config() -> Config:
    return validate_merged(
        {
            "version": 1,
            "defaults": {"concurrency": 2, "fallback": ["claude", "codex"]},
            "agents": {
                key: {"cmd": "fake", "concurrency": 2, "models": {"balanced": "default"}}
                for key in ("claude", "codex")
            },
        }
    )


LEVELS: dict[str, Any] = {"claude": ("full", "readonly"), "codex": ("full", "readonly")}


def make(
    repo: Path, script: Scripted, **limits: int
) -> tuple[PlanningEngine, SessionOwner, Scripted]:
    owner = SessionStore.create(repo).acquire_owner()
    setup = PlanningSetup(
        coordinate=script,
        qualification=QUAL,
        config=config(),
        base_commit=git(repo, "rev-parse", "HEAD"),
        available={"claude": True, "codex": True},
        levels=LEVELS,
        cwd=repo,
        limits=CoordinationLimits(**limits) if limits else CoordinationLimits(),
    )
    return PlanningEngine(owner, setup), owner, script


def run(coro: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coro())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = init_repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
    git(root, "add", "-f", "-A")
    git(root, "commit", "-q", "-m", "src")
    return root


# --- proposals and approval --------------------------------------------------------------------


def test_proposed_plan_is_saved_but_never_runs_or_approves(repo: Path) -> None:
    async def scenario() -> None:
        engine, owner, script = make(repo, Scripted(propose()))
        result = await engine.submit("Add a debounce")
        assert result.status == "awaiting_approval" and result.plan_revision == 1
        assert result.steps_used == 1 and result.plan_digest is not None
        assert owner.record.plan_revision == 1
        assert owner.record.intent is None  # nothing approved, nothing launched
        assert RunStore.list_runs(repo) == []
        assert "Add a debounce" in script.prompts[0] and "untrusted data" in script.prompts[0]
        turn = owner.store.read_record("turns", "t1")
        assert turn is not None and turn["status"] == "awaiting_approval"
        call = owner.store.read_record("calls", "t1-c1")
        assert call is not None and call["status"] == "finished" and len(call["manifests"]) == 1
        intent = owner.approve(
            1,
            result.plan_digest,
            idempotency_key="k",
            expected_session_revision=owner.record.revision,
        )
        assert intent.plan_revision == 1  # approval is an owner action on the exact digest
        owner.close()

    run(scenario)


def test_revised_plan_gets_a_new_revision_and_sees_the_current_one(repo: Path) -> None:
    async def scenario() -> None:
        engine, owner, script = make(
            repo, Scripted(propose(), propose(plan_graph(goal="smaller"), "narrowed"))
        )
        first = await engine.submit("Add a debounce")
        second = await engine.submit("Make it smaller")
        assert (first.plan_revision, second.plan_revision) == (1, 2)
        assert "current plan revision 1" in script.prompts[1]
        assert owner.store.plan(1)["graph"] != owner.store.plan(2)["graph"]  # old one kept
        assert owner.store.plan(2)["parent"] == 1
        owner.close()

    run(scenario)


# --- questions ---------------------------------------------------------------------------------


def test_clarification_round_trip_is_a_new_turn(repo: Path) -> None:
    async def scenario() -> None:
        ask = {
            "action": "ask_user",
            "questions": [
                {"id": "q1", "text": "Which?", "kind": "choice", "options": ["a", "b"]},
                {"id": "q2", "text": "Why?", "required": False},
            ],
        }
        engine, owner, script = make(repo, Scripted(ask, propose()))
        asked = await engine.submit("Build it")
        assert asked.status == "awaiting_answers" and asked.question_revision == 1
        assert [q.id for q in asked.questions] == ["q1", "q2"]

        with pytest.raises(Exception, match="requires an answer"):
            await engine.answer(1, {})
        with pytest.raises(Exception, match="unknown question"):
            await engine.answer(1, {"q1": "a", "zz": "x"})
        with pytest.raises(Exception, match="options"):
            await engine.answer(1, {"q1": "c"})
        with pytest.raises(Exception, match="no coordinator question set"):
            await engine.answer(9, {"q1": "a"})

        done = await engine.answer(1, {"q1": "a"})
        assert done.status == "awaiting_approval" and done.turn_id == "t2"
        assert "Answers to question set 1" in script.prompts[1] and "q1: a" in script.prompts[1]
        with pytest.raises(Exception, match="already answered"):
            await engine.answer(1, {"q1": "a"})
        owner.close()

    run(scenario)


# --- context -----------------------------------------------------------------------------------


def test_context_request_adds_pinned_files_and_costs_a_step(repo: Path) -> None:
    async def scenario() -> None:
        request = {
            "action": "request_context",
            "reason": "need the handler",
            "selections": [
                {"kind": "repo", "path": "src/a.py"},
                {"kind": "repo", "path": ".env"},  # never delivered; omitted, not fatal
            ],
        }
        engine, owner, script = make(repo, Scripted(request, propose()))
        result = await engine.submit("Change the handler")
        assert result.status == "awaiting_approval"
        assert result.steps_used == 3  # call + context action + call
        assert "def handler" in script.prompts[1] and "TOKEN=1" not in script.prompts[1]
        manifests = owner.store.list_records("calls")
        assert [c["step"] for c in manifests] == [1, 3]
        owner.close()

    run(scenario)


def test_step_limit_ends_the_turn(repo: Path) -> None:
    async def scenario() -> None:
        request = {
            "action": "request_context",
            "reason": "r",
            "selections": [{"kind": "repo", "path": "src/a.py"}],
        }
        engine, owner, _ = make(repo, Scripted(request, request, request), max_steps=3)
        result = await engine.submit("loop")
        assert result.status == "failed" and "step limit" in (result.error or "")
        assert owner.record.plan_revision == 0
        owner.close()

    run(scenario)


def test_artifact_selection_outside_the_session_is_refused(repo: Path) -> None:
    async def scenario() -> None:
        stranger = RunStore.create(repo)
        request = {
            "action": "request_context",
            "reason": "r",
            "selections": [
                {
                    "kind": "artifact",
                    "run_id": stranger.run_id,
                    "node_id": "n1",
                    "attempt": 1,
                    "artifact": "artifacts/x.txt",
                    "sha256": "a" * 64,
                }
            ],
        }
        engine, owner, script = make(repo, Scripted(request, propose()))
        await engine.submit("go")
        assert "not part of this session" in script.prompts[1]
        owner.close()

    run(scenario)


# --- repair and validation ---------------------------------------------------------------------


def test_one_schema_repair_is_allowed_and_spends_a_step(repo: Path) -> None:
    async def scenario() -> None:
        bad = CoordinatorInvalid("response does not match the schema: action")
        engine, owner, script = make(repo, Scripted(bad, propose()))
        result = await engine.submit("go")
        assert result.status == "awaiting_approval" and result.steps_used == 2
        assert "rejected" in script.prompts[1]
        owner.close()

    run(scenario)


def test_second_malformed_response_ends_the_turn(repo: Path) -> None:
    async def scenario() -> None:
        bad = CoordinatorInvalid("nope")
        engine, owner, _ = make(repo, Scripted(bad, bad, propose()))
        result = await engine.submit("go")
        assert result.status == "failed" and "twice" in (result.error or "")
        assert owner.record.plan_revision == 0
        owner.close()

    run(scenario)


def test_invalid_plan_is_sent_back_once(repo: Path) -> None:
    async def scenario() -> None:
        skipping = plan_graph()
        skipping["verify"] = "none"
        skipping["verification_skip_reason"] = "trust me"
        engine, owner, script = make(repo, Scripted(propose(skipping), propose()))
        result = await engine.submit("go")
        assert result.status == "awaiting_approval" and result.plan_revision == 1
        assert "cannot opt out of verification" in script.prompts[1]
        owner.close()

    run(scenario)


def test_plan_without_checks_or_eligible_agents_is_rejected() -> None:
    cfg = config()
    available = {"claude": True, "codex": True}
    from relay.core.specs import Graph

    no_checks = Graph.model_validate(
        {"nodes": [{"id": "n1", "category": "implement", "goal": "x", "owns": ["src/**"]}]}
    )
    errors = validate_plan(no_checks, config=cfg, available=available, levels=LEVELS)
    assert any("no executable acceptance" in e for e in errors)

    reader = Graph.model_validate(
        {
            "verify": "default",
            "nodes": [{"id": "r1", "category": "review", "goal": "look", "access": "read"}],
        }
    )
    full_only: dict[str, Any] = {"claude": ("full",), "codex": ("full",)}
    errors = validate_plan(reader, config=cfg, available=available, levels=full_only)
    assert any("no eligible agent" in e for e in errors)
    assert validate_plan(reader, config=cfg, available=available, levels=LEVELS) == []


# --- limits and recovery -----------------------------------------------------------------------


def test_duration_limit_cancels_and_drains_the_call(repo: Path) -> None:
    async def scenario() -> None:
        engine, owner, _ = make(repo, Scripted(30), max_duration_s=1)
        result = await asyncio.wait_for(engine.submit("slow"), timeout=40)
        assert result.status == "failed" and "time limit" in (result.error or "")
        call = owner.store.read_record("calls", "t1-c1")
        assert call is not None and call["status"] == "failed"
        owner.close()

    run(scenario)


def test_interrupted_turn_keeps_its_consumed_budget(repo: Path) -> None:
    async def scenario() -> None:
        engine, owner, script = make(repo, Scripted(propose()), max_steps=3)
        owner.append_message("operator", "hello", provenance="operator-tty")
        # An owner died mid-turn after spending two steps; its call record never finished.
        owner.write_record(
            "turns",
            "t1",
            {
                "turn_id": "t1",
                "message_id": "m1",
                "status": "active",
                "steps_used": 2,
                "active_s": 1.0,
                "repaired": False,
                "calls": ["t1-c1"],
            },
        )
        owner.write_record(
            "calls",
            "t1-c1",
            {"call_id": "t1-c1", "turn_id": "t1", "status": "started", "worker": None},
        )
        assert engine.recover() == {"t1": "interrupted"}
        assert script.prompts == []  # reopening never repeats a call
        result = await engine.resume_turn("t1")
        assert result.status == "awaiting_approval" and result.steps_used == 3  # 2 carried + 1
        with pytest.raises(Exception, match="not interrupted"):
            await engine.resume_turn("t1")
        owner.close()

    run(scenario)


CANCEL_POINTS = {
    # (subdir or event, predicate on the written data): where the cancellation arrives
    "turn-started-event": ("event", "turn.started"),
    "context-selected-event": ("event", "context.selected"),
    "call-record-written": ("calls", "started"),
    "turn-record-before-call": ("turns", "with-call"),
    "coordinator-started-event": ("event", "coordinator.started"),
}


@pytest.mark.parametrize("point", sorted(CANCEL_POINTS))
def test_a_cancellation_at_any_durable_step_leaves_the_turn_interrupted(
    repo: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    """The `_invoke` gap: a cancel after the call record is written but before the call runs.

    The cancellation arrives while the durable write is still in flight (its thread sleeps),
    so a plain `asyncio.to_thread` would let the write land after the `interrupted` record.
    No reopen or `recover_turns` is needed afterwards, and the coordinator is never called.
    """
    import threading
    import time

    from relay.core.sessions import plan_session_deletion

    async def scenario() -> None:
        engine, owner, script = make(repo, Scripted(propose()))
        loop = asyncio.get_running_loop()
        kind, match = CANCEL_POINTS[point]
        fired = threading.Event()
        holder: list[asyncio.Task[Any]] = []

        def hit() -> None:
            if not fired.is_set():
                fired.set()
                loop.call_soon_threadsafe(holder[0].cancel)
                time.sleep(0.3)  # the write is still in flight when the cancel is delivered

        real_write, real_event = owner.write_record, owner.event

        def write_record(subdir: str, name: str, data: dict[str, Any]) -> None:
            if kind == subdir and (
                (match == "started" and data.get("status") == "started")
                or (match == "with-call" and data.get("calls"))
            ):
                hit()
            real_write(subdir, name, data)

        def event(type_: str, data: dict[str, Any] | None = None) -> None:
            if kind == "event" and type_ == match:
                hit()
            real_event(type_, data)

        monkeypatch.setattr(owner, "write_record", write_record)
        monkeypatch.setattr(owner, "event", event)
        holder.append(asyncio.ensure_future(engine.submit("go")))
        with pytest.raises(asyncio.CancelledError):
            await holder[0]
        assert fired.is_set() and script.prompts == []  # cancelled before any coordinator call
        turn = owner.store.read_record("turns", "t1")
        assert turn is not None and turn["status"] == "interrupted"
        assert all(c["status"] != "started" for c in owner.store.list_records("calls"))
        owner.close()
        problems = " ".join(plan_session_deletion(repo, owner.store.session_id).problems)
        assert "coordinator" not in problems  # deletable without reopening first

        reopened = SessionOwner.acquire(repo, owner.store.session_id)
        engine.owner = reopened
        result = await engine.resume_turn("t1")  # resumable, budget carried over
        assert result.status == "awaiting_approval"
        reopened.close()

    run(scenario)


def test_an_unexpected_coordinator_error_fails_the_turn_durably(repo: Path) -> None:
    async def scenario() -> None:
        engine, owner, _ = make(repo, Scripted(RuntimeError("profile changed underneath")))
        with pytest.raises(RuntimeError, match="profile changed"):
            await engine.submit("go")
        turn = owner.store.read_record("turns", "t1")
        assert turn is not None and turn["status"] == "failed"
        assert "profile changed" in turn["outcome"]["error"]
        call = owner.store.read_record("calls", "t1-c1")
        assert call is not None and call["status"] == "failed"
        owner.close()

    run(scenario)


def _notes(exc: BaseException | None) -> list[str]:
    """Every note on `exc` and the exceptions it was raised from (a task re-wraps cancellation)."""
    found: list[str] = []
    while exc is not None:
        found += getattr(exc, "__notes__", [])
        exc = exc.__context__
    return found


def test_a_failed_interrupted_record_never_replaces_the_cancellation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relay.core.sessions import SessionError

    async def scenario() -> None:
        engine, owner, _ = make(repo, Scripted(0.5))
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        interrupted_writes: list[str] = []
        real_write, real_event = owner.write_record, owner.event

        def write_record(subdir: str, name: str, data: dict[str, Any]) -> None:
            if subdir == "calls" and data.get("status") == "interrupted":
                interrupted_writes.append(name)
                raise OSError("disk full")
            real_write(subdir, name, data)

        def event(type_: str, data: dict[str, Any] | None = None) -> None:
            if type_ == "coordinator.started":
                loop.call_soon_threadsafe(started.set)
            if type_ == "coordinator.finished":
                raise SessionError("the session event log is closed")
            real_event(type_, data)

        monkeypatch.setattr(owner, "write_record", write_record)
        monkeypatch.setattr(owner, "event", event)
        task = asyncio.ensure_future(engine.submit("go"))
        await started.wait()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as info:
            await task  # not OSError or SessionError from the bookkeeping
        assert interrupted_writes == ["t1-c1"]  # the interrupted call record is written once
        notes = " ".join(_notes(info.value))
        assert "call record" in notes and "disk full" in notes
        assert "coordinator.finished event" in notes and "event log is closed" in notes
        turn = owner.store.read_record("turns", "t1")
        assert turn is not None and turn["status"] == "interrupted"  # still written
        owner.close()

    run(scenario)


class Stubborn(Scripted):
    """A coordinator that ignores the process-cancellation signal until released."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.task_cancelled = False

    async def __call__(self, prompt: str, **_kw: Any) -> CoordinatorCall:
        self.prompts.append(prompt)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.task_cancelled = True
            raise
        return respond({"action": "finish", "summary": "late"})


def test_shutdown_never_hangs_on_a_coordinator_that_ignores_cancellation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relay.core import planning

    monkeypatch.setattr(planning, "CANCEL_GRACE_S", 0.3, raising=False)

    async def scenario() -> None:
        stubborn = Stubborn()
        engine, owner, _ = make(repo, stubborn)
        loop = asyncio.get_running_loop()
        loop.call_later(6.0, stubborn.release.set)  # an unbounded drain would wait for this
        task = asyncio.ensure_future(engine.submit("go"))
        await stubborn.entered.wait()
        began = loop.time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert loop.time() - began < 3.0
        assert stubborn.task_cancelled  # the grace period ran out and the task was cancelled
        call = owner.store.read_record("calls", "t1-c1")
        assert call is not None and call["status"] == "interrupted"
        assert "ignored cancellation" in call["cancel_forced"]
        turn = owner.store.read_record("turns", "t1")
        assert turn is not None and turn["status"] == "interrupted"
        owner.close()

    run(scenario)


def test_resume_cannot_reset_an_exhausted_budget(repo: Path) -> None:
    async def scenario() -> None:
        engine, owner, script = make(repo, Scripted(propose()), max_steps=2)
        owner.append_message("operator", "hello", provenance="operator-tty")
        owner.write_record(
            "turns",
            "t1",
            {
                "turn_id": "t1",
                "message_id": "m1",
                "status": "interrupted",
                "steps_used": 2,
                "active_s": 0.0,
                "repaired": False,
                "calls": [],
            },
        )
        result = await engine.resume_turn("t1")
        assert result.status == "failed" and "step limit" in (result.error or "")
        assert script.prompts == []
        owner.close()

    run(scenario)


# --- review ------------------------------------------------------------------------------------


def test_review_of_a_sealed_round_uses_recorded_evidence(repo: Path) -> None:
    async def scenario() -> None:
        review = {
            "action": "review",
            "summary": "looks fine",
            "findings": [
                {
                    "severity": "info",
                    "text": "ok",
                    "evidence": [{"node_id": "n1", "kind": "result", "ref": "summary"}],
                }
            ],
        }
        engine, owner, script = make(repo, Scripted(review))
        store = RunStore.create(repo)
        store.add_node("n1")
        store.write_spec(
            "01-n1/result.spec.yaml",
            ResultSpec(id="n1", status="done", agent="fake", summary="Debounce added"),
        )
        store.update_run(lambda r: setattr(r, "state", "done"))
        result = await engine.review_round(store)
        assert result.status == "finished" and result.action == "review"
        assert "Debounce added" in script.prompts[0]
        running = RunStore.create(repo)
        with pytest.raises(Exception, match="running round"):
            await engine.review_round(running)
        owner.close()

    run(scenario)


def test_the_claude_adapter_satisfies_the_coordinate_protocol() -> None:
    from relay.adapters.claude import ClaudeAdapter
    from relay.core.planning import Coordinate
    from relay.core.registry import Agent

    adapter_call: Coordinate = ClaudeAdapter(Agent(cmd="claude"), parent_env={}).coordinate
    assert callable(adapter_call)
