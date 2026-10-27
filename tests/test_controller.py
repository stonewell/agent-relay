"""Session controller: live answers, durable pause/drain and shutdown drainage (task 2.22)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from harness import init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.controller import AnswerRejected, SessionController
from relay.core.registry import Config, validate_merged
from relay.core.sessions import SessionConflict, SessionOwner, SessionStore
from relay.core.specs import Graph


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
            "verification_skip_reason": "controller test",
            "policy": {"max_depth": 10},
            "nodes": [{"category": "implement", "goal": "Implement", **n} for n in nodes],
        }
    )


def approved_owner(repo: Path, spec: Graph) -> SessionOwner:
    owner = SessionStore.create(repo).acquire_owner()
    revision = owner.save_plan(
        spec.model_dump(mode="json", by_alias=True, exclude_unset=True), explanation="test"
    )
    digest = owner.store.plan(revision)["digest"]
    owner.approve(
        revision, digest, idempotency_key="k", expected_session_revision=owner.record.revision
    )
    return owner


def controller(owner: SessionOwner, adapter: FakeAdapter) -> SessionController:
    return SessionController(owner, config=config(), adapters={"claude": adapter, "codex": adapter})


async def until(predicate: Callable[[], bool], timeout_s: float = 20.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.03)


def node_state(ctl: SessionController, key: str) -> str | None:
    node = ctl.snapshot()["nodes"].get(key)
    return node["state"] if node else None


def run(coro: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coro())


def test_live_answer_while_an_independent_node_runs(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        spec = graph(
            [
                {"id": "a", "budget": {"max_attempts": 2}, "owns": ["a/**"]},
                {"id": "b", "owns": ["b/**"]},
                {"id": "c", "depends_on": ["a"], "owns": ["c/**"]},
            ]
        )
        adapter = FakeAdapter(
            {
                "a": [
                    Scenario(
                        status="needs_input",
                        questions=[{"id": "q1", "text": "Which?"}],
                        write={"a/partial.py": "kept"},
                    ),
                    Scenario(write={"a/a.py": "a"}),
                ],
                "b": Scenario(sleep_s=4.0, write={"b/b.py": "b"}),
                "c": Scenario(write={"c/c.py": "c"}),
            }
        )
        owner = approved_owner(repo, spec)
        ctl = controller(owner, adapter)
        await ctl.start_round()
        await until(lambda: node_state(ctl, "a") == "blocked")
        assert node_state(ctl, "b") == "running"  # independent work continues

        with pytest.raises(AnswerRejected, match="stale"):
            await ctl.answer("cmd-stale", "a", 7, {"q1": "x"})
        with pytest.raises(AnswerRejected, match="not waiting"):
            await ctl.answer("cmd-b", "b", 1, {"q1": "x"})

        first = await ctl.answer("cmd-1", "a", 1, {"q1": "simple"})
        assert first.applied
        assert not (await ctl.answer("cmd-1", "a", 1, {"q1": "simple"})).applied  # duplicate
        with pytest.raises(SessionConflict):
            await ctl.answer("cmd-1", "a", 1, {"q1": "different"})  # conflicting reuse

        await until(lambda: node_state(ctl, "c") == "done")
        result = await ctl.finish_round()
        assert result.exit_code == 0, result.to_json()
        assert node_state(ctl, "a") == "done" and node_state(ctl, "b") == "done"
        await ctl.shutdown()
        assert not owner.held

    run(scenario)


def test_pause_stops_new_dispatch_and_resume_continues(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        spec = graph([{"id": "a"}, {"id": "b", "depends_on": ["a"]}])
        adapter = FakeAdapter(
            {"a": Scenario(sleep_s=1.5, write={"a.py": "a"}), "b": Scenario(write={"b.py": "b"})}
        )
        owner = approved_owner(repo, spec)
        ctl = controller(owner, adapter)
        await ctl.start_round()
        await until(lambda: node_state(ctl, "a") == "running")

        assert (await ctl.pause("p1")).applied
        assert owner.record.admission == "closed"  # durable, not just in memory
        await ctl.drain(timeout_s=20)
        assert node_state(ctl, "a") == "done"
        await asyncio.sleep(0.5)
        assert node_state(ctl, "b") == "pending"  # not dispatched while the gate is closed

        assert (await ctl.resume("r1")).applied
        assert owner.record.admission == "open"
        await until(lambda: node_state(ctl, "b") == "done")
        assert (await ctl.finish_round()).exit_code == 0
        await ctl.shutdown()

    run(scenario)


def test_answer_recorded_while_paused_is_not_dispatched(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        spec = graph([{"id": "a", "budget": {"max_attempts": 2}}])
        adapter = FakeAdapter(
            {
                "a": [
                    Scenario(status="needs_input", questions=[{"id": "q1", "text": "?"}]),
                    Scenario(write={"a.py": "a"}),
                ]
            }
        )
        owner = approved_owner(repo, spec)
        ctl = controller(owner, adapter)
        await ctl.start_round()
        await until(lambda: node_state(ctl, "a") == "blocked")
        await ctl.pause("p")
        await ctl.answer("ans", "a", 1, {"q1": "yes"})
        await asyncio.sleep(0.6)
        assert node_state(ctl, "a") == "pending"  # answer kept, nothing dispatched
        assert ctl.snapshot()["nodes"]["a"]["attempts"] == 1
        await ctl.resume("r")
        await until(lambda: node_state(ctl, "a") == "done")
        await ctl.finish_round()
        await ctl.shutdown()

    run(scenario)


def test_cancel_round_waits_for_workers_to_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = approved_owner(repo, graph([{"id": "a"}]))
        ctl = controller(owner, FakeAdapter(Scenario(hang=True)))
        await ctl.start_round()
        await until(lambda: node_state(ctl, "a") == "running")
        await ctl.cancel_round("c1")
        assert not ctl.running
        assert ctl.snapshot()["run_state"] == "cancelled"
        await ctl.shutdown()

    run(scenario)


def test_shutdown_interrupts_and_keeps_ownership_until_drained(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = approved_owner(repo, graph([{"id": "a"}]))
        ctl = controller(owner, FakeAdapter(Scenario(hang=True)))
        await ctl.start_round()
        await until(lambda: node_state(ctl, "a") == "running")

        closing = asyncio.create_task(ctl.shutdown())
        await asyncio.sleep(0.05)
        closing.cancel()  # a second cancellation must not abandon the drain
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not ctl.running  # the run really finished before the cancellation surfaced
        assert not owner.held
        store = SessionStore.open(repo, owner.store.session_id)
        assert store.read().state == "interrupted"
        from relay.core.store import RunStore

        run_id = store.read().linked_runs[0]
        assert RunStore.open(repo, run_id).read_run().state == "interrupted"

    run(scenario)


def test_start_requires_an_approved_launch_and_is_single_round(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        bare = SessionStore.create(repo).acquire_owner()
        ctl = controller(bare, FakeAdapter(Scenario()))
        with pytest.raises(SessionConflict):
            await ctl.start_round()
        await ctl.shutdown()

        owner = approved_owner(repo, graph([{"id": "a"}]))
        ctl2 = controller(owner, FakeAdapter(Scenario(sleep_s=1.0)))
        await ctl2.start_round()
        with pytest.raises(SessionConflict):
            await ctl2.start_round()
        await ctl2.finish_round()
        await ctl2.shutdown()

    run(scenario)
