"""Checkpoints, linked rounds, explicit bases and unapplied follow-ups (task 2.25)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from harness import git, init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.checkpoints import (
    CheckpointError,
    checkpoint_ref,
    integration_status,
    is_deliverable,
    load_checkpoint,
    record_integration,
    resolve_retained_base,
    seal_checkpoint,
    source_ref,
    validate_checkpoint,
)
from relay.core.controller import SessionController
from relay.core.gitbase import read_ref
from relay.core.registry import Config, validate_merged
from relay.core.runlock import RunLock, RunLockSessionBusy
from relay.core.sessions import SessionConflict, SessionOwner, SessionStore
from relay.core.specs import Graph
from relay.core.store import RunStore


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
            "verification_skip_reason": "checkpoint test",
            "policy": {"max_depth": 10},
            "nodes": [{"category": "implement", "goal": "Implement", **n} for n in nodes],
        }
    )


def approve(
    owner: SessionOwner, spec: Graph, *, base: dict[str, Any] | None = None, key: str = "k"
) -> None:
    revision = owner.save_plan(
        spec.model_dump(mode="json", by_alias=True, exclude_unset=True),
        explanation="test",
        base=base,
    )
    owner.approve(
        revision,
        owner.store.plan(revision)["digest"],
        idempotency_key=key,
        expected_session_revision=owner.record.revision,
    )


def controller(owner: SessionOwner, adapter: FakeAdapter) -> SessionController:
    return SessionController(owner, config=config(), adapters={"claude": adapter, "codex": adapter})


async def until(predicate: Callable[[], bool], timeout_s: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.03)


def state(ctl: SessionController, key: str) -> str | None:
    node = ctl.snapshot()["nodes"].get(key)
    return node["state"] if node else None


def run(coro: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(coro())


def two_node_graph() -> Graph:
    return graph(
        [
            {"id": "a", "owns": ["a/**"]},
            {"id": "b", "owns": ["b/**"]},
            {"id": "c", "depends_on": ["b"], "owns": ["c/**"]},
        ]
    )


def adapter_a_done_b_hanging() -> FakeAdapter:
    return FakeAdapter(
        {
            "a": Scenario(write={"a/x.py": "a"}),
            "b": Scenario(hang=True, write={"b/partial.py": "unfinished"}),
            "c": Scenario(write={"c/c.py": "c"}),
        }
    )


# --- sealing and superseding ------------------------------------------------------------------


def test_revise_seals_verified_outputs_and_supersedes_the_round(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, two_node_graph())
        ctl = controller(owner, adapter_a_done_b_hanging())
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done" and state(ctl, "b") == "running")

        manifest = await ctl.revise("rev-1", cancel_active=True, timeout_s=30)
        assert manifest is not None and manifest.state == "sealed"
        assert [c.node_id for c in manifest.contributions] == ["a"]
        excluded = {e.node_id: e.reason for e in manifest.excluded}
        assert "diagnostic" in excluded["b"] and "never ran" in excluded["c"]
        assert manifest.combined_commit == manifest.contributions[0].output_commit
        ref = checkpoint_ref(owner.store.session_id, manifest.checkpoint_id)
        assert read_ref(repo, ref) == manifest.combined_commit
        source = source_ref(owner.store.session_id, manifest.checkpoint_id, "a")
        assert read_ref(repo, source) == manifest.contributions[0].output_commit

        record = RunStore.open(repo, run_id).read_run()
        assert record.state == "interrupted" and record.interrupted_reason == "checkpoint"
        session = owner.record
        assert session.admission == "open"  # reopened so a revised plan can be approved
        assert [r.state for r in session.rounds] == ["superseded"]
        assert session.rounds[0].checkpoint_id == manifest.checkpoint_id
        assert owner.record.checkpoints == [manifest.checkpoint_id]
        with pytest.raises(RunLockSessionBusy, match="superseded"):
            RunLock.acquire(repo, run_id, takeover=True, session_owner=owner)
        assert not ctl.running
        await ctl.shutdown()

    run(scenario)


def test_checkpoint_is_immutable_evidence_and_detects_tampering(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, two_node_graph())
        ctl = controller(owner, adapter_a_done_b_hanging())
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done")
        manifest = await ctl.revise("r", cancel_active=True, timeout_s=30)
        assert manifest is not None
        assert validate_checkpoint(repo, owner.store, manifest) == []

        store = RunStore.open(repo, run_id)
        result = store.node_dir("a") / "result.spec.yaml"
        result.write_text(result.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        problems = validate_checkpoint(repo, owner.store, manifest)
        assert any("result changed" in p for p in problems)
        with pytest.raises(CheckpointError, match="result changed"):
            resolve_retained_base(owner, {"kind": "checkpoint", "checkpoint_id": "c1"})
        await ctl.shutdown()

    run(scenario)


def test_conflicting_contributions_keep_sources_and_no_combined_base(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        spec = graph([{"id": "a"}, {"id": "b"}])
        approve(owner, spec)
        adapter = FakeAdapter(
            {
                "a": Scenario(write={"shared.txt": "from a\n"}),
                "b": Scenario(write={"shared.txt": "from b\n"}),
            }
        )
        ctl = controller(owner, adapter)
        await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done" and state(ctl, "b") == "done")
        manifest = await ctl.revise("r", timeout_s=30)
        assert manifest is not None and manifest.state == "conflicted"
        assert manifest.combined_commit is None and manifest.combined_ref is None
        assert manifest.conflicts and manifest.conflicts[0].paths == ["shared.txt"]
        assert set(manifest.conflicts[0].sources) == {"a", "b"}
        ref = checkpoint_ref(owner.store.session_id, manifest.checkpoint_id)
        sid, cid = owner.store.session_id, manifest.checkpoint_id
        assert read_ref(repo, source_ref(sid, cid, "a"))
        assert read_ref(repo, source_ref(sid, cid, "b"))
        assert read_ref(repo, ref) is None  # no merged base is claimed
        assert not is_deliverable(owner.store, manifest.checkpoint_id)
        with pytest.raises(CheckpointError, match="bounded repair"):
            resolve_retained_base(owner, {"kind": "checkpoint", "checkpoint_id": "c1"})
        await ctl.shutdown()

    run(scenario)


def test_seal_requires_a_settled_round(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, two_node_graph())
        ctl = controller(owner, adapter_a_done_b_hanging())
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "b") == "running")
        with pytest.raises(CheckpointError, match="still running"):
            seal_checkpoint(owner, RunStore.open(repo, run_id))
        await ctl.shutdown()

    run(scenario)


# --- explicit bases and follow-ups ------------------------------------------------------------


def test_follow_up_round_pins_the_checkpoint_not_the_moving_head(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, two_node_graph())
        ctl = controller(owner, adapter_a_done_b_hanging())
        await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done")
        manifest = await ctl.revise("r", cancel_active=True, timeout_s=30)
        assert manifest is not None and manifest.combined_commit is not None

        # The user's branch moves on; the follow-up must not follow it.
        (repo / "later.txt").write_text("moved on\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "later work")
        head = git(repo, "rev-parse", "HEAD")
        assert head != manifest.combined_commit

        follow = graph([{"id": "d", "owns": ["d/**"]}])
        approve(
            owner,
            follow,
            base={"kind": "checkpoint", "checkpoint_id": manifest.checkpoint_id},
            key="k2",
        )
        adapter = FakeAdapter({"d": Scenario(write={"d/d.py": "d"})})
        ctl2 = controller(owner, adapter)
        run_id = await ctl2.start_round()
        result = await ctl2.finish_round()
        assert result.exit_code == 0, result.to_json()

        record = RunStore.open(repo, run_id).read_run()
        assert record.base is not None and record.base.kind == "checkpoint"
        assert record.base.commit == manifest.combined_commit
        assert read_ref(repo, f"refs/relay/{run_id}/base") == manifest.combined_commit
        tree = git(repo, "ls-tree", "-r", "--name-only", str(result.integration_commit))
        assert "a/x.py" in tree and "d/d.py" in tree  # earlier partial work preserved
        assert "later.txt" not in tree  # HEAD was never consulted
        assert owner.record.rounds[-1].base == {
            "kind": "checkpoint",
            "checkpoint_id": manifest.checkpoint_id,
        }
        await ctl2.shutdown()

    run(scenario)


def test_unapplied_integration_can_be_the_next_base(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, graph([{"id": "a", "owns": ["a/**"]}]))
        ctl = controller(owner, FakeAdapter({"a": Scenario(write={"a/x.py": "a"})}))
        first = await ctl.start_round()
        result = await ctl.finish_round()
        assert result.exit_code == 0
        await ctl.shutdown()

        owner2 = SessionStore.open(repo, owner.store.session_id).acquire_owner()
        approve(
            owner2,
            graph([{"id": "e", "owns": ["e/**"]}]),
            base={"kind": "integration", "source_run": first},
            key="k2",
        )
        ctl2 = controller(owner2, FakeAdapter({"e": Scenario(write={"e/e.py": "e"})}))
        second = await ctl2.start_round()
        again = await ctl2.finish_round()
        assert again.exit_code == 0
        tree = git(repo, "ls-tree", "-r", "--name-only", str(again.integration_commit))
        assert "a/x.py" in tree and "e/e.py" in tree
        assert read_ref(repo, f"refs/relay/{second}/base") == result.integration_commit
        # the user's branch is still unchanged
        assert git(repo, "rev-parse", "HEAD") == git(repo, "rev-parse", "main")
        await ctl2.shutdown()

    run(scenario)


def test_a_moved_integration_branch_is_not_a_valid_base(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, graph([{"id": "a", "owns": ["a/**"]}]))
        ctl = controller(owner, FakeAdapter({"a": Scenario(write={"a/x.py": "a"})}))
        run_id = await ctl.start_round()
        await ctl.finish_round()
        await ctl.shutdown()
        git(
            repo,
            "update-ref",
            f"refs/heads/relay/{run_id}/integration",
            git(repo, "rev-parse", "main"),
        )
        owner2 = SessionStore.open(repo, owner.store.session_id).acquire_owner()
        with pytest.raises(CheckpointError, match="moved"):
            resolve_retained_base(owner2, {"kind": "integration", "source_run": run_id})
        owner2.close()

    run(scenario)


# --- delivery gate -----------------------------------------------------------------------------


def test_checkpoint_is_not_deliverable_until_integration_checks_pass(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, two_node_graph())
        ctl = controller(owner, adapter_a_done_b_hanging())
        await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done")
        manifest = await ctl.revise("r", cancel_active=True, timeout_s=30)
        assert manifest is not None
        cid = manifest.checkpoint_id
        assert integration_status(owner.store, cid) is None
        assert not is_deliverable(owner.store, cid)  # sealed, checks not run

        with pytest.raises(CheckpointError, match="explicit operator"):
            record_integration(owner, cid, "skipped", "no reason given")
        record_integration(owner, cid, "failed", "tests failed")
        assert not is_deliverable(owner.store, cid)
        record_integration(owner, cid, "skipped", "operator accepts the risk", operator_skip=True)
        evidence = integration_status(owner.store, cid)
        assert evidence is not None and evidence.operator_skip and is_deliverable(owner.store, cid)
        record_integration(owner, cid, "passed", "ci green")
        assert is_deliverable(owner.store, cid)
        # the sealed manifest itself never changed
        assert load_checkpoint(owner.store, cid).model_dump() == manifest.model_dump()
        await ctl.shutdown()

    run(scenario)


def test_verify_callback_outcome_is_recorded_as_evidence(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, two_node_graph())
        ctl = controller(owner, adapter_a_done_b_hanging())
        await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done")

        def failing(_commit: str) -> None:
            raise RuntimeError("integration tests failed")

        manifest = await ctl.revise("r", cancel_active=True, timeout_s=30, verify=failing)
        assert manifest is not None
        evidence = integration_status(owner.store, manifest.checkpoint_id)
        assert evidence is not None and evidence.status == "failed"
        assert "integration tests failed" in evidence.detail
        assert not is_deliverable(owner.store, manifest.checkpoint_id)
        await ctl.shutdown()

    run(scenario)


# --- restoring ---------------------------------------------------------------------------------


def test_superseded_round_restores_only_explicitly(tmp_path: Path) -> None:
    async def scenario() -> None:
        repo = init_repo(tmp_path / "repo")
        owner = SessionStore.create(repo).acquire_owner()
        approve(owner, graph([{"id": "a", "owns": ["a/**"]}, {"id": "b", "owns": ["b/**"]}]))
        adapter = FakeAdapter(
            {
                "a": Scenario(write={"a/x.py": "a"}),
                "b": Scenario(sleep_s=30.0, write={"b/y.py": "b"}),
            }
        )
        ctl = controller(owner, adapter)
        run_id = await ctl.start_round()
        await until(lambda: state(ctl, "a") == "done" and state(ctl, "b") == "running")
        manifest = await ctl.revise("r", cancel_active=True, timeout_s=30)
        assert manifest is not None

        with pytest.raises(SessionConflict, match="not a superseded round"):
            owner.restore_round("2026-01-01-ffff")
        await ctl.restore_round(run_id)
        assert [r.state for r in owner.record.rounds] == ["active"]
        with pytest.raises(SessionConflict, match="already active"):
            await ctl.restore_round(run_id)
        await ctl.shutdown()

    run(scenario)
