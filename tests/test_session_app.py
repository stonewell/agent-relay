"""Core additions for the TUI (task 2.28): explicit continuation, confirmed session deletion,
lock-before-sweep, retained plan bases, the application service and its host, and projections.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from harness import command_line, git, init_repo
from relay.adapters.coordination import CoordinatorCall
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.controller import SessionController
from relay.core.planning import PlanningEngine, PlanningSetup, recover_turns
from relay.core.registry import Config, validate_merged
from relay.core.runlock import lock_path
from relay.core.session_app import (
    BaseView,
    CoordinatorBackend,
    NodeView,
    QuestionSetView,
    ServiceHost,
    SessionApp,
    SessionView,
)
from relay.core.sessions import (
    SessionBusy,
    SessionConflict,
    SessionError,
    SessionOwner,
    SessionStore,
    delete_session,
    plan_session_deletion,
    session_lock_path,
)
from relay.core.specs import CoordinatorResponse, Graph
from relay.core.store import RunStore, runs_dir
from relay.tui.projection import composer_mode, node_label, pending_actions, project

PASS = command_line([sys.executable, "-c", "pass"])


def config() -> Config:
    return validate_merged(
        {
            "version": 1,
            "defaults": {"concurrency": 3, "fallback": ["claude", "codex"]},
            "agents": {
                key: {"cmd": "fake", "concurrency": 3, "models": {"balanced": "default"}}
                for key in ("claude", "codex")
            },
        }
    )


def graph(nodes: list[dict[str, Any]]) -> Graph:
    return Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "session app test",
            "nodes": [{"category": "implement", "goal": "Implement", **n} for n in nodes],
        }
    )


def approved(repo: Path, spec: Graph) -> SessionOwner:
    owner = SessionStore.create(repo).acquire_owner()
    revision = owner.save_plan(
        spec.model_dump(mode="json", by_alias=True, exclude_unset=True), explanation="t"
    )
    owner.approve(
        revision,
        owner.store.plan(revision)["digest"],
        idempotency_key="k",
        expected_session_revision=owner.record.revision,
    )
    return owner


def controller(owner: SessionOwner, adapter: FakeAdapter) -> SessionController:
    return SessionController(owner, config=config(), adapters={"claude": adapter, "codex": adapter})


async def until(predicate: Callable[[], bool], timeout_s: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.03)


def run(coro: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coro())


def state(ctl: SessionController, node: str) -> str | None:
    found = ctl.snapshot()["nodes"].get(node)
    return found["state"] if found else None


# --- explicit continuation ---------------------------------------------------------------------


def test_continue_round_is_explicit_after_a_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = approved(repo, graph([{"id": "a", "budget": {"max_attempts": 2}}]))
        sid = owner.store.session_id
        ctl = controller(owner, FakeAdapter({"a": Scenario(hang=True)}))
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "running")
        await ctl.shutdown()  # stop: drains, leaves the round interrupted
        assert RunStore.open(repo, run_id).read_run().state == "interrupted"

        reopened = SessionOwner.acquire(repo, sid)
        assert reopened.record.state == "interrupted"
        await asyncio.sleep(0.3)
        assert RunStore.open(repo, run_id).read_run().state == "interrupted"  # never by itself
        ctl2 = controller(reopened, FakeAdapter({"a": Scenario(write={"a.py": "a"})}))
        with pytest.raises(SessionConflict, match="not an active round"):
            await ctl2.continue_round("2026-01-01-0000")
        assert await ctl2.continue_round(run_id) == run_id
        assert reopened.record.state == "active"
        with pytest.raises(SessionConflict, match="already active"):
            await ctl2.continue_round(run_id)
        await until(lambda: state(ctl2, "a") == "done")
        assert (await ctl2.finish_round()).exit_code == 0
        with pytest.raises(SessionConflict, match="not an interrupted round"):
            await ctl2.continue_round(run_id)
        await ctl2.shutdown()

    run(scenario)


def test_a_superseded_round_cannot_be_continued_only_restored(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = approved(repo, graph([{"id": "a"}]))
        ctl = controller(owner, FakeAdapter({"a": Scenario(hang=True)}))
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "running")
        await ctl.revise("rev", cancel_active=True, timeout_s=30)
        with pytest.raises(SessionConflict, match="not an active round"):
            await ctl.continue_round(run_id)
        await ctl.shutdown()

    run(scenario)


# --- deletion ----------------------------------------------------------------------------------


def relay_refs(repo: Path, run_ids: list[str], sid: str) -> list[str]:
    refs = git(repo, "for-each-ref", "--format=%(refname)").splitlines()
    owned = tuple(
        [f"refs/relay/{r}/" for r in run_ids]
        + [f"refs/heads/relay/{r}/" for r in run_ids]
        + [f"refs/relay-sessions/{sid}/"]
    )
    return [ref for ref in refs if ref.startswith(owned)]


def worktrees_of(repo: Path, run_ids: list[str]) -> list[str]:
    listed = git(repo, "worktree", "list", "--porcelain").splitlines()
    return [
        line for line in listed if line.startswith("worktree ") and any(r in line for r in run_ids)
    ]


def build_two_rounds(repo: Path) -> tuple[str, list[str]]:
    """Round 1 is revised into a checkpoint (b cancelled); round 2 finishes, unapplied."""

    async def scenario() -> tuple[str, list[str]]:
        owner = approved(repo, graph([{"id": "a"}, {"id": "b"}]))
        sid = owner.store.session_id
        adapter = FakeAdapter({"a": Scenario(write={"a.py": "a"}), "b": Scenario(hang=True)})
        ctl = controller(owner, adapter)
        first = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done" and state(ctl, "b") == "running")
        manifest = await ctl.revise("rev", cancel_active=True, timeout_s=30)
        assert manifest is not None
        revision = owner.save_plan(
            graph([{"id": "c"}]).model_dump(mode="json", by_alias=True, exclude_unset=True),
            explanation="t",
            base={"kind": "checkpoint", "checkpoint_id": manifest.checkpoint_id},
        )
        owner.approve(
            revision,
            owner.store.plan(revision)["digest"],
            idempotency_key="k2",
            expected_session_revision=owner.record.revision,
        )
        ctl2 = controller(owner, FakeAdapter({"c": Scenario(write={"c.py": "c"})}))
        second = await ctl2.start_round()
        assert (await ctl2.finish_round()).exit_code == 0
        await ctl2.shutdown()
        await ctl.shutdown()
        return sid, [first, second]

    return asyncio.run(scenario())


def test_delete_session_removes_its_runs_refs_and_worktrees_but_not_user_branches(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    sid, run_ids = build_two_rounds(repo)
    main_before = git(repo, "rev-parse", "main")
    assert relay_refs(repo, run_ids, sid)

    busy = SessionOwner.acquire(repo, sid)
    with pytest.raises(SessionBusy):
        delete_session(repo, sid, include_undelivered=True)
    busy.close()

    plan = plan_session_deletion(repo, sid)
    assert [r.run_id for r in plan.runs] == run_ids and not plan.problems
    assert {r.delivery for r in plan.runs} == {"undelivered"}
    with pytest.raises(SessionConflict, match="not delivered"):
        delete_session(repo, sid)  # undelivered rounds need explicit confirmation
    assert sid in SessionStore.list_sessions(repo)
    assert all((runs_dir(repo) / r).is_dir() for r in run_ids)  # nothing changed

    done = delete_session(repo, sid, include_undelivered=True)
    assert list(done.runs) == run_ids and done.refs
    assert relay_refs(repo, run_ids, sid) == []  # no orphan refs
    assert worktrees_of(repo, run_ids) == []
    assert not any((runs_dir(repo) / r).exists() for r in run_ids)
    assert sid not in SessionStore.list_sessions(repo)
    assert git(repo, "rev-parse", "main") == main_before  # the user's branch is untouched
    assert session_lock_path(repo, sid).exists()  # the session id stays reserved


def test_a_delivered_session_deletes_without_the_undelivered_confirmation(
    tmp_path: Path,
) -> None:
    from relay.core.delivery import install_delivery, prepare_run_delivery

    async def scenario() -> tuple[str, str]:
        repo = init_repo(tmp_path / "repo")
        spec = Graph.model_validate(
            {
                "commands": {"test": PASS},
                "nodes": [
                    {"id": "a", "category": "implement", "goal": "g", "owns": ["a/**"],
                     "acceptance": [{"run": PASS}]}
                ],
            }
        )  # fmt: skip
        owner = approved(repo, spec)
        ctl = controller(owner, FakeAdapter({"a": Scenario(write={"a/x.py": "x"})}))
        run_id = await ctl.start_round()
        assert (await ctl.finish_round()).exit_code == 0
        prepared = prepare_run_delivery(repo, run_id)
        install_delivery(repo, prepared, confirmed_digest=prepared.digest)
        sid = owner.store.session_id
        await ctl.shutdown()
        return sid, run_id

    sid, run_id = asyncio.run(scenario())
    repo = tmp_path / "repo"
    plan = plan_session_deletion(repo, sid)
    assert [(r.delivery, r.detail) for r in plan.runs] == [("delivered", "contained in main")]
    main_before = git(repo, "rev-parse", "main")
    delete_session(repo, sid)
    assert relay_refs(repo, [run_id], sid) == []
    assert git(repo, "rev-parse", "main") == main_before
    assert (repo / "a" / "x.py").is_file()  # delivered work stays in the user's branch


def test_delete_refuses_while_a_run_owner_is_alive(tmp_path: Path) -> None:
    from relay.core.platform.procid import current_identity
    from relay.core.runlock import _identity_payload  # pyright: ignore[reportPrivateUsage]

    repo = init_repo(tmp_path / "repo")
    sid, run_ids = build_two_rounds(repo)
    lock_path(repo, run_ids[1]).write_text(_identity_payload(current_identity()), "utf-8")
    with pytest.raises(SessionConflict, match="in progress"):
        delete_session(repo, sid, include_undelivered=True)
    assert all((runs_dir(repo) / r).is_dir() for r in run_ids)
    assert relay_refs(repo, run_ids, sid)
    assert "in progress" in " ".join(plan_session_deletion(repo, sid).problems)


def test_delete_refuses_a_run_shared_with_another_session(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    sid, run_ids = build_two_rounds(repo)
    other = SessionStore.create(repo)
    record = other.read()
    record.linked_runs = [run_ids[0]]
    other._write(record)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SessionConflict, match="also referenced by session"):
        delete_session(repo, sid, include_undelivered=True)
    assert all((runs_dir(repo) / r).is_dir() for r in run_ids)


def test_a_crash_mid_deletion_resumes_and_leaves_no_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relay.core import run_purge

    repo = init_repo(tmp_path / "repo")
    sid, run_ids = build_two_rounds(repo)
    real = run_purge.execute_run_purge
    calls: list[str] = []

    def crash_after_first(repo_root: Path, plan: run_purge.RunPurge) -> None:
        calls.append(plan.run_id)
        if len(calls) == 2:
            raise KeyboardInterrupt  # the process dies between the two runs
        real(repo_root, plan)

    monkeypatch.setattr(run_purge, "execute_run_purge", crash_after_first)
    with pytest.raises(KeyboardInterrupt):
        delete_session(repo, sid, include_undelivered=True)
    monkeypatch.setattr(run_purge, "execute_run_purge", real)

    assert not (runs_dir(repo) / run_ids[0]).exists() and (runs_dir(repo) / run_ids[1]).is_dir()
    assert (SessionStore(repo, sid).dir / "deleting").is_file()
    with pytest.raises(SessionError, match="was interrupted"):
        SessionOwner.acquire(repo, sid)  # no new work until the deletion is finished
    svc_summary = asyncio.run(
        SessionApp(repo, config=config(), coordinators={}).sessions()
    )
    assert [(x.session_id, x.state) for x in svc_summary] == [(sid, "deleting")]

    delete_session(repo, sid)  # resumes from the marker; no new confirmation is needed
    assert relay_refs(repo, run_ids, sid) == []
    assert worktrees_of(repo, run_ids) == []
    assert not any((runs_dir(repo) / r).exists() for r in run_ids)
    assert sid not in SessionStore.list_sessions(repo)


def test_deletion_text_states_exactly_what_is_deleted(tmp_path: Path) -> None:
    from relay.tui.app import deletion_text

    repo = init_repo(tmp_path / "repo")
    sid, run_ids = build_two_rounds(repo)
    text = deletion_text(plan_session_deletion(repo, sid))
    for run_id in run_ids:
        assert f"run {run_id} [NOT DELIVERED" in text
    assert "checkpoint ref(s)" in text and "relay ref(s)" in text and "worktree(s)" in text
    assert "cannot be applied or resumed afterwards" in text
    assert "Your own branches and working tree are not changed." in text


def test_delete_refuses_unconfirmed_execution(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = approved(repo, graph([{"id": "a"}]))  # approved launch never reconciled
    sid = owner.store.session_id
    owner.close()
    with pytest.raises(SessionConflict, match="has not been reconciled"):
        delete_session(repo, sid)
    assert sid in SessionStore.list_sessions(repo)


def test_an_interrupted_deletion_blocks_ownership_until_finished(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    sid = SessionStore.create(repo).session_id
    (SessionStore(repo, sid).dir / "deleting").write_text("x\n", encoding="utf-8")
    with pytest.raises(SessionError, match="deletion of session .* was interrupted"):
        SessionOwner.acquire(repo, sid)
    delete_session(repo, sid)
    assert sid not in SessionStore.list_sessions(repo)


def test_acquiring_a_busy_session_never_sweeps_the_owners_temp_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = SessionStore.create(repo).acquire_owner()
    in_flight = owner.store.dir / ".tmp-session.json.123.abcd"
    in_flight.write_text("{}", encoding="utf-8")
    with pytest.raises(SessionBusy):
        SessionOwner.acquire(repo, owner.store.session_id)
    assert in_flight.exists()
    owner.close()
    SessionOwner.acquire(repo, owner.store.session_id).close()  # the new owner sweeps it
    assert not in_flight.exists()


# --- planning base -----------------------------------------------------------------------------


def respond(action: dict[str, Any]) -> CoordinatorCall:
    response = CoordinatorResponse.model_validate({"spec_version": 1, "response": action})
    return CoordinatorCall(response, "claude", "m", "9.9.9", "digest", 1, 0.01)


def proposal() -> dict[str, Any]:
    node = {
        "id": "n1",
        "category": "implement",
        "goal": "g",
        "owns": ["src/**"],
        "acceptance": [{"run": PASS}],
    }
    return {
        "action": "propose_plan",
        "explanation": "e",
        "graph": {"integration_acceptance": [{"run": PASS}], "nodes": [node]},
    }


class Scripted:
    def __init__(self, *steps: dict[str, Any]) -> None:
        self.steps = list(steps)

    async def __call__(self, prompt: str, **_kw: Any) -> CoordinatorCall:
        return respond(self.steps.pop(0))


def test_a_retained_plan_base_is_saved_and_covered_by_the_digest(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        levels: dict[str, Any] = {"claude": ("full", "readonly"), "codex": ("full", "readonly")}
        setup = PlanningSetup(
            coordinate=Scripted(proposal(), proposal()),
            agent="claude",
            config=config(),
            base_commit=git(repo, "rev-parse", "HEAD"),
            available={"claude": True, "codex": True},
            levels=levels,
            cwd=repo,
        )
        engine = PlanningEngine(owner, setup)
        first = await engine.submit("one")
        engine.plan_base = {"kind": "checkpoint", "checkpoint_id": "c1"}
        second = await engine.submit("two")
        assert owner.store.plan(1)["base"] is None
        assert owner.store.plan(2)["base"] == {"kind": "checkpoint", "checkpoint_id": "c1"}
        assert first.plan_digest != second.plan_digest  # same graph, different base
        owner.close()

    run(scenario)


# --- the service and its host ------------------------------------------------------------------


def test_cancelling_the_caller_never_cancels_the_operation() -> None:
    host = ServiceHost().start()
    finished: list[bool] = []

    async def operation() -> int:
        await asyncio.sleep(0.3)
        finished.append(True)
        return 1

    async def caller() -> None:
        task = asyncio.create_task(host.call(operation()))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.5)

    try:
        asyncio.run(caller())
        assert finished == [True]
    finally:
        host.close()
    with pytest.raises(RuntimeError):
        host.submit(operation())


# --- projections -------------------------------------------------------------------------------


def view(**changes: Any) -> SessionView:
    base: dict[str, Any] = {
        "session_id": "2026-10-27-s0001",
        "owned": True,
        "readonly_reason": None,
        "session_state": "active",
        "admission": "open",
        "session_revision": 1,
        "plan_revision": 0,
        "coordinating": False,
        "checkpointing": False,
        "stopping": False,
        "round_running": False,
        "run_id": None,
        "messages": (),
        "question_sets": (),
        "nodes": (),
        "rounds": (),
        "turns": (),
        "pending_plan": None,
        "recovery": (),
        "coordinators": (),
        "coordinator": None,
        "base": BaseView("head", "HEAD", None, False),
        "deliverable": None,
        "notices": (),
    }
    base.update(changes)
    return SessionView(**base)


def node(node_id: str, state: str, reason: str | None = None) -> NodeView:
    return NodeView(node_id, state, reason, (), "write", (), None, None, 1, 1, None)


def test_a_waiting_node_does_not_make_execution_look_idle() -> None:
    mixed = view(
        round_running=True,
        nodes=(node("a", "blocked", "needs_input"), node("b", "running"), node("c", "done")),
    )
    projection = project(mixed)
    assert projection.state == "Executing"
    assert "1 running" in projection.detail and "1 awaiting answers" in projection.detail
    only_waiting = view(round_running=True, nodes=(node("a", "blocked", "needs_input"),))
    assert project(only_waiting).state == "Awaiting answers"
    paused = view(round_running=True, admission="closed", nodes=(node("b", "running"),))
    assert "admission paused" in project(paused).detail


def test_projection_states_and_text_labels() -> None:
    assert project(None).state == "No session"
    assert project(view(stopping=True)).state == "Stopping"
    assert project(view(), stopping=True).state == "Stopping"
    assert project(view(checkpointing=True)).state == "Checkpointing"
    assert project(view(coordinating=True)).state == "Coordinating"
    assert project(view(recovery=("round x is interrupted",))).state == "Interrupted"
    assert project(view(deliverable="run x")).state == "Ready for review/delivery"
    assert project(view()).state == "Idle"
    assert node_label(node("a", "blocked", "needs_input")) == "blocked: needs input"
    assert node_label(node("a", "failed")) == "FAILED"


def test_composer_distinguishes_answers_from_steering() -> None:
    question = QuestionSetView("node", 1, "a", "open", ())
    executing = view(round_running=True, question_sets=(question,))
    assert composer_mode(executing).kind == "answer"
    assert composer_mode(executing, prefer_request=True).kind == "steer"
    stale = QuestionSetView("coordinator", 1, None, "stale", ())
    assert composer_mode(view(question_sets=(stale,))).kind == "request"
    assert all(a.id != "plan" for a in pending_actions(view(round_running=True)))
    assert any(a.id == "stop" for a in pending_actions(view(round_running=True)))
    assert any(a.id == "coordinator" for a in pending_actions(view()))


def test_limit_exhaustion_shows_the_retained_answer_and_needs_a_policy_change(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter(
        {"n1": Scenario(status="needs_input", questions=[{"id": "q1", "text": "Name?"}])}
    )
    svc = SessionApp(
        repo,
        config=config(),
        adapters={"claude": adapter, "codex": adapter},
        coordinators={"claude": CoordinatorBackend("claude", Scripted())},
        poll_s=0.05,
    )
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        owner = svc._owner  # pyright: ignore[reportPrivateUsage]
        assert owner is not None
        spec = proposal()["graph"]
        revision = host.submit(
            asyncio.to_thread(
                owner.save_plan, spec, explanation="t", proposer="claude"
            )
        ).result()
        host.submit(svc.approve(revision)).result(30)

        def view_now() -> SessionView:
            return host.submit(svc.view()).result(30)

        for _ in range(400):
            sets = [q for q in view_now().question_sets if q.node_id == "n1"]
            if sets and sets[0].state == "open":
                break
            asyncio.run(asyncio.sleep(0.05))
        else:
            raise AssertionError(f"node never asked: {view_now().nodes} {view_now().notices}")
        host.submit(svc.answer_node("n1", sets[0].revision, {"q1": "alpha"})).result(30)
        for _ in range(400):
            current = view_now()
            sets = [q for q in current.question_sets if q.node_id == "n1"]
            if sets and sets[0].state == "stale":
                break
            asyncio.run(asyncio.sleep(0.05))
        else:
            raise AssertionError("limit exhaustion was not projected")
        assert sets[0].retained == ("q1: alpha",)
        assert "explicit policy change" in sets[0].note
        node_view = [n for n in current.nodes if n.node_id == "n1"][0]
        assert node_view.blocked_reason == "attempts_exhausted" and node_view.attempts == 1
        assert len(adapter.invocations) == 1  # never silently extended
        assert sid == svc.session_id
    finally:
        host.close(svc.shutdown)


# --- review regressions --------------------------------------------------------------------------


def make_service(repo: Path, adapter: FakeAdapter, script: Scripted) -> SessionApp:
    return SessionApp(
        repo,
        config=config(),
        adapters={"claude": adapter, "codex": adapter},
        coordinators={"claude": CoordinatorBackend("claude", script)},
        poll_s=0.05,
    )


def wait(predicate: Callable[[], bool], timeout_s: float = 30.0) -> None:
    import time

    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.03)


def test_continue_round_takes_over_a_crashed_owner(tmp_path: Path) -> None:
    """After an owner death run.json still says `running`; Continue must take it over."""
    from relay.core.platform.procid import ProcessIdentity
    from relay.core.runlock import _identity_payload, lock_path  # pyright: ignore[reportPrivateUsage]

    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = approved(repo, graph([{"id": "a", "budget": {"max_attempts": 2}}]))
        sid = owner.store.session_id
        ctl = controller(owner, FakeAdapter({"a": Scenario(hang=True)}))
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "running")
        await ctl.shutdown()
        # Simulate the crash afterwards: `running` record, session left active, stale run lock.
        store = RunStore.open(repo, run_id)
        store.update_run(lambda r: setattr(r, "state", "running"))
        session = SessionStore(repo, sid)
        record = session.read()
        record.state = "active"
        session._write(record)  # pyright: ignore[reportPrivateUsage]
        dead = ProcessIdentity(pid=os.getpid(), start=-1)  # pid reused: a different process
        lock_path(repo, run_id).write_text(_identity_payload(dead), encoding="utf-8")

        reopened = SessionOwner.acquire(repo, sid)
        assert reopened.record.state == "interrupted"
        ctl2 = controller(reopened, FakeAdapter({"a": Scenario(write={"a.py": "a"})}))
        assert await ctl2.continue_round(run_id) == run_id
        await until(lambda: state(ctl2, "a") == "done")
        assert (await ctl2.finish_round()).exit_code == 0
        await ctl2.shutdown()

    run(scenario)


def test_continue_round_refuses_a_running_round_with_a_live_owner(tmp_path: Path) -> None:
    from relay.core.platform.procid import current_identity
    from relay.core.runlock import _identity_payload, lock_path  # pyright: ignore[reportPrivateUsage]

    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = approved(repo, graph([{"id": "a"}]))
        ctl = controller(owner, FakeAdapter({"a": Scenario(hang=True)}))
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "running")
        await ctl.shutdown()
        RunStore.open(repo, run_id).update_run(lambda r: setattr(r, "state", "running"))
        lock_path(repo, run_id).write_text(_identity_payload(current_identity()), "utf-8")
        reopened = SessionOwner.acquire(repo, owner.store.session_id)
        ctl2 = controller(reopened, FakeAdapter())
        with pytest.raises(SessionConflict, match="live or unverifiable"):
            await ctl2.continue_round(run_id)
        await ctl2.shutdown()

    run(scenario)


def test_projection_offers_continue_for_a_crashed_round() -> None:
    from relay.core.session_app import RoundView

    crashed = view(rounds=(RoundView("r1", 1, "active", "running", None, None, None),))
    assert any(a.id == "continue" for a in pending_actions(crashed))


def test_shutdown_waits_for_an_apply_in_progress(tmp_path: Path) -> None:
    import threading
    import time

    from relay.core.delivery import Verdict

    repo = init_repo(tmp_path / "repo")
    svc = make_service(repo, FakeAdapter(), Scripted())
    finished = threading.Event()
    held_during: list[bool] = []

    def slow_install(_prepared: object, _digest: str) -> dict[str, Any]:
        time.sleep(0.8)
        owner = svc._owner  # pyright: ignore[reportPrivateUsage]
        held_during.append(owner is not None and owner.held)
        finished.set()
        return {"target": "t", "commit": "c", "changed": True, "installed": True}

    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        svc._install = slow_install  # type: ignore[method-assign]
        svc._prepared["d"] = (object(), Verdict("passed", "ok"))  # type: ignore[assignment]  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        applying = host.submit(svc.apply_delivery("d"))
        wait(lambda: host.submit(svc.view()).result(30).applying)
        with pytest.raises(SessionConflict, match="being applied"):
            host.submit(svc.open(None)).result(30)
        host.submit(svc.shutdown()).result(30)
        assert finished.is_set() and held_during == [True]  # ownership held throughout
        assert applying.result(30)["changed"]
        SessionOwner.acquire(repo, sid).close()  # released only afterwards
    finally:
        host.close(svc.shutdown)


def test_open_and_delete_refuse_while_a_coordinator_turn_runs(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    class Slow(Scripted):
        async def __call__(self, prompt: str, **kw: Any) -> CoordinatorCall:
            await asyncio.sleep(1.0)
            return await super().__call__(prompt, **kw)

    svc = make_service(repo, FakeAdapter(), Slow(proposal()))
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        turn = host.submit(svc.submit("plan it"))
        wait(lambda: host.submit(svc.view()).result(30).coordinating)
        with pytest.raises(SessionConflict, match="coordinator is working"):
            host.submit(svc.open(None)).result(30)
        with pytest.raises(SessionConflict, match="coordinator is working"):
            host.submit(svc.delete(sid)).result(30)
        assert turn.result(30).status == "awaiting_approval"
    finally:
        host.close(svc.shutdown)


def test_release_cancels_a_running_turn_before_giving_up_ownership(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")

    class Hang(Scripted):
        """Runs until the engine's process cancellation fires, like a supervised CLI."""

        async def __call__(self, prompt: str, **kw: Any) -> CoordinatorCall:
            from relay.core.platform.proc import (
                _CANCELLATION,  # pyright: ignore[reportPrivateUsage]
                ProcessCancelled,
            )

            cancel = _CANCELLATION.get()
            assert cancel is not None
            while not cancel.is_set():
                await asyncio.sleep(0.02)
            raise ProcessCancelled("cancelled")

    svc = make_service(repo, FakeAdapter(), Hang())
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        turn = host.submit(svc.submit("plan it"))
        calls = SessionStore(repo, sid)
        wait(lambda: any(c["status"] == "started" for c in calls.list_records("calls")))
        host.submit(svc.shutdown()).result(30)
        assert turn.done() and turn.cancelled()  # cancelled before release, not left running
        # The coordinator call was drained before release, so reopening recovers the turn as
        # interrupted instead of blocking it on a process that might still be alive.
        reopened = SessionOwner.acquire(repo, sid)
        try:
            assert set(recover_turns(reopened).values()) <= {"interrupted"}
            assert [t["status"] for t in reopened.store.list_records("turns")] == ["interrupted"]
        finally:
            reopened.close()
    finally:
        host.close(svc.shutdown)


def test_shutdown_in_the_call_record_gap_needs_no_reopen_to_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quit while the call record is being written, before the coordinator runs (task 2.29)."""
    import threading
    import time

    repo = init_repo(tmp_path / "repo")
    writing = threading.Event()
    real = SessionOwner.write_record

    def slow(self: SessionOwner, subdir: str, name: str, data: dict[str, Any]) -> None:
        if subdir == "calls" and data.get("status") == "started" and not writing.is_set():
            writing.set()
            time.sleep(0.5)  # shutdown's cancellation arrives during this write
        real(self, subdir, name, data)

    monkeypatch.setattr(SessionOwner, "write_record", slow)
    script = Scripted(proposal())
    svc = make_service(repo, FakeAdapter(), script)
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        turn = host.submit(svc.submit("plan it"))
        assert writing.wait(30)
        host.submit(svc.shutdown()).result(30)
        assert turn.cancelled() and script.steps  # the coordinator was never called
        store = SessionStore(repo, sid)
        assert [t["status"] for t in store.list_records("turns")] == ["interrupted"]
        assert [c["status"] for c in store.list_records("calls")] == ["interrupted"]
        assert plan_session_deletion(repo, sid).problems == ()
        delete_session(repo, sid)  # straight after quitting: nothing was left `active`
        assert sid not in SessionStore.list_sessions(repo)
    finally:
        host.close(svc.shutdown)


@pytest.mark.parametrize("record", ["turn-blocked", "turn-active", "call-started"])
def test_delete_refuses_unconfirmed_coordinator_work(tmp_path: Path, record: str) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = SessionStore.create(repo).acquire_owner()
    sid = owner.store.session_id
    if record.startswith("turn"):
        status = record.split("-")[1]
        owner.write_record("turns", "t1", {"turn_id": "t1", "status": status, "calls": []})
    else:
        owner.write_record("calls", "t1-c1", {"call_id": "t1-c1", "status": "started"})
    owner.close()
    with pytest.raises(SessionConflict, match="coordinator"):
        delete_session(repo, sid)
    assert sid in SessionStore.list_sessions(repo)


def test_a_refused_deletion_changes_nothing(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = approved(repo, graph([{"id": "a"}]))  # a pending launch: deletion is refused
    sid = owner.store.session_id
    owner.close()
    before = (SessionStore(repo, sid).dir / "session.json").read_bytes()
    events = SessionStore(repo, sid).events_path
    size = events.stat().st_size
    with pytest.raises(SessionConflict):
        delete_session(repo, sid)
    assert (SessionStore(repo, sid).dir / "session.json").read_bytes() == before
    assert events.stat().st_size == size
    SessionOwner.acquire(repo, sid).close()  # the lock was released
    events.rename(events.with_name("moved.jsonl"))  # no handle left open (Windows would refuse)


def test_delete_never_sweeps_a_live_owners_temp_files(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = SessionStore.create(repo).acquire_owner()
    in_flight = owner.store.dir / ".tmp-session.json.9.abcd"
    in_flight.write_text("{}", encoding="utf-8")
    with pytest.raises(SessionBusy):
        delete_session(repo, owner.store.session_id)
    assert in_flight.exists()
    owner.close()


def test_a_partial_deletion_stays_listed_until_finished(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    store = SessionStore.create(repo)
    sid = store.session_id
    (store.dir / "plans").mkdir()
    (store.dir / "plans" / "1.json").write_text("{}", encoding="utf-8")
    (store.dir / "deleting").write_text("x\n", encoding="utf-8")  # a crash mid-deletion
    svc = make_service(repo, FakeAdapter(), Scripted())
    summaries = asyncio.run(svc.sessions())
    assert [(s.session_id, s.state) for s in summaries] == [(sid, "deleting")]
    delete_session(repo, sid)
    assert not store.dir.exists()


def test_approval_refuses_a_plan_without_a_recorded_proposer(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    svc = make_service(repo, FakeAdapter(), Scripted())
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        host.submit(svc.open(None)).result(30)
        owner = svc._owner  # pyright: ignore[reportPrivateUsage]
        assert owner is not None
        spec = proposal()["graph"]
        first = host.submit(asyncio.to_thread(owner.save_plan, spec, explanation="t")).result()
        with pytest.raises(SessionConflict, match="records no proposing coordinator"):
            host.submit(svc.approve(first)).result(30)
        second = host.submit(
            asyncio.to_thread(
                owner.save_plan, spec, explanation="t", proposer="claude"
            )
        ).result()
        assert owner.store.plan(first)["digest"] != owner.store.plan(second)["digest"]
    finally:
        host.close(svc.shutdown)


def test_coordinator_answers_and_turn_resumes_wait_while_a_round_executes(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "repo")
    ask = {"action": "ask_user", "questions": [{"id": "q1", "text": "Which?"}]}
    adapter = FakeAdapter({"n1": Scenario(hang=True)})
    svc = make_service(repo, adapter, Scripted(ask))
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        host.submit(svc.open(None)).result(30)
        assert host.submit(svc.submit("go")).result(30).status == "awaiting_answers"
        owner = svc._owner  # pyright: ignore[reportPrivateUsage]
        assert owner is not None
        revision = host.submit(
            asyncio.to_thread(
                owner.save_plan,
                proposal()["graph"],
                explanation="t",
                proposer="claude",
            )
        ).result()
        host.submit(svc.approve(revision)).result(30)
        current = host.submit(svc.view()).result(30)
        assert current.round_running
        assert composer_mode(current).kind == "steer"  # the open coordinator set must wait
        with pytest.raises(SessionConflict, match="executing"):
            host.submit(svc.answer_coordinator(1, {"q1": "x"})).result(30)
        with pytest.raises(SessionConflict, match="executing"):
            host.submit(svc.resume_turn("t1")).result(30)
    finally:
        host.close(svc.shutdown, still_running=lambda: svc.executing)


def test_context_requests_never_sweep_a_runs_temp_files(tmp_path: Path) -> None:
    from relay.core.specs.coordination import RequestContext

    repo = init_repo(tmp_path / "repo")
    owner = approved(repo, graph([{"id": "a"}]))
    run_store = owner.start_run()
    in_flight = run_store.run_dir / ".tmp-run.json.9.abcd"
    in_flight.write_text("{}", encoding="utf-8")
    levels: dict[str, Any] = {"claude": ("full",), "codex": ("full",)}
    engine = PlanningEngine(
        owner,
        PlanningSetup(
            coordinate=Scripted(),
            agent="claude",
            config=config(),
            base_commit=git(repo, "rev-parse", "HEAD"),
            available={"claude": True, "codex": True},
            levels=levels,
            cwd=repo,
        ),
    )
    action = RequestContext.model_validate(
        {
            "reason": "r",
            "selections": [
                {
                    "kind": "artifact",
                    "run_id": run_store.run_id,
                    "node_id": "a",
                    "attempt": 1,
                    "artifact": "artifacts/x.txt",
                    "sha256": "0" * 64,
                }
            ],
        }
    )
    assert len(engine._context_items(action)) == 1  # pyright: ignore[reportPrivateUsage]
    assert in_flight.exists()
    owner.close()


def test_stop_and_shutdown_racing_leave_the_session_released(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    svc = make_service(repo, FakeAdapter(), Scripted())
    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        stopping = host.submit(svc.stop())
        closing = host.submit(svc.shutdown())
        stopping.result(30)
        closing.result(30)
        assert svc.session_id is None
        SessionOwner.acquire(repo, sid).close()  # nobody kept ownership
        with pytest.raises(SessionError, match="closing"):
            host.submit(svc.open(None)).result(30)
    finally:
        host.close(svc.shutdown)


def test_a_failing_cancellation_still_releases_ownership(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    svc = make_service(repo, FakeAdapter(), Scripted())

    async def broken() -> None:
        raise RuntimeError("cancellation failed")

    host = ServiceHost().start()
    try:
        host.submit(svc.start()).result(30)
        sid = host.submit(svc.open(None)).result(30)
        svc._cancel_tasks = broken  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="cancellation failed"):
            host.submit(svc.shutdown()).result(30)
        SessionOwner.acquire(repo, sid).close()
    finally:
        host.close()


def test_host_close_keeps_the_loop_while_work_still_runs() -> None:
    import threading
    import time

    host = ServiceHost().start()
    running = threading.Event()
    running.set()
    threading.Timer(0.6, running.clear).start()
    started = time.monotonic()
    host.close(still_running=running.is_set)
    assert time.monotonic() - started >= 0.5
