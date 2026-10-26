"""Foreground graph integration with real git and scripted worker processes."""

import asyncio
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from harness import command_line, git, init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.events import read_events
from relay.core.graph import GraphResult, run_graph
from relay.core.registry import Config, validate_merged
from relay.core.specs import Graph
from relay.core.store import RunStore

RUN = "2026-10-26-a1b2"


def config() -> Config:
    return validate_merged(
        {
            "version": 1,
            "defaults": {"concurrency": 2, "fallback": ["claude", "codex"]},
            "agents": {
                key: {"cmd": "fake", "concurrency": 1, "models": {"balanced": "default"}}
                for key in ("claude", "codex")
            },
        }
    )


def graph(nodes: list[dict[str, Any]], **kwargs: Any) -> Graph:
    return Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "Test snapshot plumbing",
            "policy": {"max_depth": 10},
            "nodes": [{"category": "implement", "goal": "Implement", **node} for node in nodes],
            **kwargs,
        }
    )


def invoke(repo: Path, spec: Graph, adapter: FakeAdapter, **kwargs: Any) -> GraphResult:
    return asyncio.run(
        run_graph(
            repo, spec, config=config(), adapters={"claude": adapter, "codex": adapter}, **kwargs
        )
    )


def test_independent_writers_and_dependency_snapshot(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = graph(
        [
            {"id": "a", "owns": ["a.py"], "routing": {"agent": "claude"}},
            {"id": "b", "owns": ["b.py"], "routing": {"agent": "codex"}},
            {"id": "c", "depends_on": ["a", "b"], "owns": ["c.py"]},
        ]
    )
    seen: dict[str, str] = {}

    def scenario(task: Any, ctx: Any) -> Scenario:
        if task.id == "c":
            seen["a"] = (ctx.worktree / "a.py").read_text()
            seen["b"] = (ctx.worktree / "b.py").read_text()
        return Scenario(write={f"{task.id}.py": task.id}, sleep_s=0.1)

    outcome = invoke(repo, spec, FakeAdapter(scenario), run_id=RUN)
    assert outcome.exit_code == 0, outcome
    assert seen == {"a": "a", "b": "b"}
    assert git(repo, "show", f"{outcome.integration_commit}:c.py") == "c"
    assert not (repo / "a.py").exists()
    events = read_events(RunStore(repo, RUN).events_path)
    assert sum(e.type == "run.finished" for e in events) == 1
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


def test_failed_branch_blocks_dependents_and_keeps_independent_output(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c"}])
    adapter = FakeAdapter({"a": Scenario(status="failed"), "c": Scenario(write={"c.py": "ok"})})
    outcome = invoke(repo, spec, adapter, run_id=RUN)
    assert outcome.exit_code == 1, outcome
    assert outcome.nodes["b"].state == "blocked"
    assert outcome.nodes["c"].state == "done"
    assert outcome.integration_commit is None


def test_completed_nodes_reuse(tmp_path: Path, monkeypatch: Any) -> None:
    from relay.core.worktree import WorktreeManager

    for key in list(os.environ):
        if key.endswith(("_KEY", "_TOKEN")):
            monkeypatch.delenv(key)
    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a"}])
    adapter = FakeAdapter(Scenario(write={"a.py": "ok"}))
    outcome = invoke(repo, spec, adapter, run_id=RUN)
    assert outcome.exit_code == 0, outcome
    count = len(adapter.invocations)
    store = RunStore(repo, RUN)
    manager = WorktreeManager(repo, RUN)
    # Model owner death after the durable completed result, before checkout removal.
    checkout = manager.create("a", str(store.read_run().nodes["a"].output_commit))
    store.update_run(lambda r: setattr(r, "state", "interrupted"))
    resumed = invoke(repo, spec, adapter, resume_run=RUN)
    assert resumed.exit_code == 0, resumed
    assert len(adapter.invocations) == count
    assert not checkout.path.exists()
    assert not store.read_run().nodes["a"].recovery_pending
    assert store.read_run().nodes["a"].attempts == 1


def test_conflict_resolution_preserves_original_parents(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = graph(
        [
            {"id": "a", "owns": ["shared.py"]},
            {"id": "b", "owns": ["shared.py"]},
            {"id": "c", "depends_on": ["a", "b"], "owns": ["c.py"]},
            {"id": "d", "category": "review", "depends_on": ["b", "c"]},
        ]
    )

    def scenario(task: Any, ctx: Any) -> Scenario:
        if task.id.startswith("resolve-"):
            return Scenario(write={"shared.py": "resolved"})
        if task.id == "c":
            assert (ctx.worktree / "shared.py").read_text() == "resolved"
            return Scenario(write={"c.py": "ok"})
        if task.id == "d":
            assert (ctx.worktree / "shared.py").read_text() == "resolved"
            return Scenario()
        return Scenario(write={"shared.py": task.id})

    outcome = invoke(repo, spec, FakeAdapter(scenario), run_id=RUN)
    assert outcome.exit_code == 0, outcome.to_json()
    record = RunStore(repo, RUN).read_run()
    commits = [record.nodes[key].output_commit for key in ("a", "b")]
    for commit in commits:
        git(repo, "merge-base", "--is-ancestor", str(commit), str(outcome.integration_commit))
    assert len([key for key in record.nodes if key.startswith("resolve-")]) == 1
    git(repo, "worktree", "prune")
    git(repo, "gc", "--prune=now")
    assert git(repo, "show", f"{outcome.integration_commit}:shared.py") == "resolved"


def test_conflict_resolution_is_bounded_and_scope_checked(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a"}, {"id": "b"}, {"id": "c", "depends_on": ["a", "b"]}])

    def scenario(task: Any, ctx: Any) -> Scenario:
        if task.id.startswith("resolve-"):
            return Scenario(write={"other.py": "unauthorized", "shared.py": "resolved"})
        return Scenario(write={"shared.py": task.id})

    outcome = invoke(repo, spec, FakeAdapter(scenario), run_id=RUN)
    assert outcome.exit_code != 0
    record = RunStore(repo, RUN).read_run()
    resolvers = [value for key, value in record.nodes.items() if key.startswith("resolve-")]
    assert len(resolvers) == 1 and resolvers[0].attempts == 1
    assert outcome.integration_commit is None


def test_cross_agent_fallback(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a", "budget": {"max_attempts": 2}}])
    failed = FakeAdapter(Scenario(status="failed", write={"partial.py": "kept"}))
    success = FakeAdapter(Scenario(write={"done.py": "ok"}))
    outcome = asyncio.run(
        run_graph(
            repo, spec, config=config(), adapters={"claude": failed, "codex": success}, run_id=RUN
        )
    )
    assert outcome.exit_code == 0, outcome.to_json()
    assert len(failed.invocations) == len(success.invocations) == 1
    assert git(repo, "show", f"{outcome.integration_commit}:partial.py") == "kept"
    routes = [
        e.data["decision"]
        for e in read_events(RunStore(repo, RUN).events_path)
        if e.type == "node.routed"
    ]
    assert routes[-1]["event"] == "fallback"
    assert routes[-1]["attempt"] == 2


def test_single_node_foreground_fallback(tmp_path: Path) -> None:
    from relay.core.run import run_foreground
    from relay.core.specs.task import Budget, TaskSpec

    repo = init_repo(tmp_path / "repo")
    task = TaskSpec(
        run_id=RUN,
        id="a",
        category="implement",
        goal="Implement",
        verify="none",
        verification_skip_reason="Test retry plumbing",
        budget=Budget(max_attempts=2),
    )
    outcome = asyncio.run(
        run_foreground(
            repo,
            task,
            config=config(),
            run_id=RUN,
            adapters={
                "claude": FakeAdapter(Scenario(status="failed")),
                "codex": FakeAdapter(Scenario(write={"done.py": "ok"})),
            },
        )
    )
    assert outcome.exit_code == 0, outcome.to_json()


def test_graph_timeout_retries_with_total_limit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a", "budget": {"max_attempts": 2, "timeout_s": 1}}])
    outcome = asyncio.run(
        run_graph(
            repo,
            spec,
            config=config(),
            run_id=RUN,
            adapters={
                "claude": FakeAdapter(Scenario(hang=True, write={"partial.py": "kept"})),
                "codex": FakeAdapter(Scenario(write={"done.py": "ok"})),
            },
        )
    )
    assert outcome.exit_code == 0, outcome.to_json()
    assert RunStore(repo, RUN).read_run().nodes["a"].attempts == 2


@pytest.mark.parametrize("resume", [False, True])
def test_escalation_survives_foreground_resume(tmp_path: Path, resume: bool) -> None:
    repo = init_repo(tmp_path / "repo")
    cfg = config()
    cfg.agents["claude"].models = {"balanced": "small", "deep": "large"}
    spec = graph(
        [
            {
                "id": "a",
                "routing": {"agent": "claude"},
                "budget": {"max_attempts": 1 if resume else 2},
            }
        ]
    )
    adapter = FakeAdapter([Scenario(status="failed"), Scenario(write={"done.py": "ok"})])
    initial = asyncio.run(
        run_graph(repo, spec, config=cfg, run_id=RUN, adapters={"claude": adapter})
    )
    if resume:
        assert initial.exit_code == 1
        record = RunStore(repo, RUN).read_run()
        assert record.graph is not None
        initial = asyncio.run(
            run_graph(
                repo,
                record.graph,
                config=cfg,
                resume_run=RUN,
                max_attempts=2,
                adapters={"claude": adapter},
            )
        )
    assert initial.exit_code == 0, initial.to_json()
    routes = [
        event.data["decision"]
        for event in read_events(RunStore(repo, RUN).events_path)
        if event.type == "node.routed"
    ]
    assert [route["model"] for route in routes] == ["small", "large"]
    assert routes[-1]["event"] == "escalated"
    assert routes[-1]["from"]["model"] == "small"


def test_final_integration_verifies_combined_tree_before_publication(tmp_path: Path) -> None:
    import sys

    from harness import command_line

    repo = init_repo(tmp_path / "repo")
    check = command_line(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; assert not (Path('a.py').exists() and Path('b.py').exists())",
        ],
    )
    spec = graph(
        [{"id": "a", "owns": ["a.py"]}, {"id": "b", "owns": ["b.py"]}],
        verify="default",
        commands={"test": check},
    )
    adapter = FakeAdapter({"a": Scenario(write={"a.py": "a"}), "b": Scenario(write={"b.py": "b"})})
    outcome = invoke(repo, spec, adapter, run_id=RUN)
    assert outcome.exit_code == 5, outcome.to_json()
    assert outcome.integration_commit is None
    from relay.core.gitbase import read_ref

    assert read_ref(repo, f"refs/heads/relay/{RUN}/integration") is None


def test_graph_needs_input_reply_resume_preserves_edits(tmp_path: Path) -> None:
    from relay.core.answers import record_answers
    from relay.core.specs.answers import Submission

    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a"}, {"id": "b", "depends_on": ["a"]}])
    adapter = FakeAdapter(
        {
            "a": [
                Scenario(
                    status="needs_input",
                    write={"partial.py": "kept"},
                    questions=[{"id": "q1", "text": "Which behavior?"}],
                ),
                Scenario(write={"done.py": "ok"}),
            ]
        }
    )
    outcome = invoke(repo, spec, adapter, run_id=RUN)
    assert outcome.exit_code == 6, outcome.to_json()
    store = RunStore(repo, RUN)
    record_answers(
        store,
        "a",
        Submission(
            submission_id="answer-1",
            question_revision=1,
            provenance="operator-cli",
            submitted_at="2026-10-26T00:00:00Z",
            answers={"q1": "Use the simple behavior"},
        ),
    )
    # Answers do not silently reset the total attempt budget.
    blocked = invoke(repo, spec, adapter, resume_run=RUN)
    assert blocked.exit_code == 6
    resumed = invoke(repo, spec, adapter, resume_run=RUN, max_attempts=2)
    assert resumed.exit_code == 0, resumed.to_json()
    assert git(repo, "show", f"{resumed.integration_commit}:partial.py") == "kept"
    assert store.read_run().nodes["a"].attempts == 2


def test_cancel_request_stops_running_workers(tmp_path: Path) -> None:
    from relay.core.store import atomic_write

    async def run() -> None:
        repo = init_repo(tmp_path / "repo")
        spec = graph([{"id": "a"}])
        pending = asyncio.create_task(
            run_graph(
                repo,
                spec,
                config=config(),
                adapters={"claude": FakeAdapter(Scenario(hang=True))},
                run_id=RUN,
            )
        )
        store = RunStore(repo, RUN)
        for _ in range(300):
            if store.run_json.exists():
                record = store.read_run()
                if "a" in record.nodes and record.nodes["a"].worker is not None:
                    break
            await asyncio.sleep(0.02)
        else:
            pending.cancel()
            raise AssertionError("worker did not start")
        directory = store.path("cancel-requests")
        directory.mkdir(exist_ok=True)
        atomic_write(directory / "request.json", "{}")
        result = await asyncio.wait_for(pending, timeout=10)
        assert result.exit_code == 4, result.to_json()
        assert store.read_run().nodes["a"].worker is None

    asyncio.run(run())


@pytest.mark.parametrize("changed", ["fingerprint", "artifact", "ref"])
def test_cached_resolver_requires_matching_execution_and_published_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from relay.core.publish import node_ref
    from relay.core.specs import ResultSpec, TaskSpec, load_spec
    from relay.core.worktree import WorktreeManager

    repo = init_repo(tmp_path / "repo")
    spec = graph([{"id": "a"}, {"id": "b"}])
    adapter = FakeAdapter(
        lambda task, ctx: Scenario(
            write={"shared.py": "resolved" if task.id.startswith("resolve-") else task.id}
        )
    )
    first = invoke(repo, spec, adapter, run_id=RUN)
    assert first.exit_code == 0, first.to_json()
    count = len(adapter.invocations)
    store = RunStore(repo, RUN)
    key = next(key for key in store.read_run().nodes if key.startswith("resolve-"))
    checkout = WorktreeManager(repo, RUN).create(
        key, str(store.read_run().nodes[key].output_commit)
    )
    store.update_run(lambda r: setattr(r, "state", "interrupted"))
    reused = invoke(repo, spec, adapter, resume_run=RUN)
    assert reused.exit_code == 0, reused.to_json()
    assert len(adapter.invocations) == count
    assert not checkout.path.exists()
    assert not store.read_run().nodes[key].recovery_pending
    if changed == "fingerprint":
        original = FakeAdapter.reuse_fingerprint

        def fingerprint(self: FakeAdapter, task: TaskSpec) -> str | None:
            return None if task.id == key else original(self, task)

        monkeypatch.setattr(FakeAdapter, "reuse_fingerprint", fingerprint)
    elif changed == "artifact":
        result_path = store.node_dir(key) / "result.spec.yaml"
        result = load_spec(ResultSpec, result_path)
        result.summary = "changed result"
        store.write_spec(f"{store.node_dir(key).name}/result.spec.yaml", result)
    else:
        git(repo, "update-ref", node_ref(RUN, key), git(repo, "rev-parse", "HEAD"))
    rejected = invoke(repo, spec, adapter, resume_run=RUN)
    assert rejected.exit_code != 0
    assert "one attempt" in str(rejected.error)
    assert store.read_run().nodes[key].attempts == 1
    assert len(adapter.invocations) == count


def test_changed_upstream_invalidates_descendants_but_reuses_independent_node(
    tmp_path: Path, monkeypatch: Any
) -> None:
    for key in list(os.environ):
        if key.endswith(("_KEY", "_TOKEN")):
            monkeypatch.delenv(key)
    repo = init_repo(tmp_path / "repo")
    spec = graph(
        [
            {"id": "a", "budget": {"max_attempts": 2}},
            {"id": "b", "depends_on": ["a"], "budget": {"max_attempts": 2}},
            {"id": "c"},
        ]
    )
    adapter = FakeAdapter(lambda task, ctx: Scenario(write={f"{task.id}.py": "same output"}))
    assert invoke(repo, spec, adapter, run_id=RUN).exit_code == 0
    original = len(adapter.invocations)
    changed = spec.model_copy(deep=True)
    changed.nodes[0].goal = "Different instructions, same deliverable"
    resumed = invoke(repo, changed, adapter, resume_run=RUN)
    assert resumed.exit_code == 0, resumed.to_json()
    assert len(adapter.invocations) == original + 2
    record = RunStore(repo, RUN).read_run()
    assert record.nodes["a"].attempts == record.nodes["b"].attempts == 2
    assert record.nodes["c"].attempts == 1


@pytest.mark.parametrize("phase", ["node", "integration"])
@pytest.mark.parametrize("runner_shutdown", [False, True])
def test_interrupt_drains_workers_before_releasing_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, runner_shutdown: bool
) -> None:
    from relay.core.platform.procid import IdentityState, ProcessIdentity, check_identity
    from relay.core.runlock import OwnerState, inspect_owner
    from relay.core.worktree import DecommissionResult, WorktreeManager

    repo = init_repo(tmp_path / "repo")
    marker = tmp_path / "verifier-started"
    script = tmp_path / "blocking-verifier.py"
    script.write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    check = command_line([sys.executable, str(script)])
    node_verification = (
        {"verify": "default"}
        if phase == "node"
        else {"verify": "none", "verification_skip_reason": "Exercise final verification"}
    )
    spec = graph(
        [{"id": "a", **node_verification}],
        commands={"test": check},
        verify="none" if phase == "node" else "default",
    )
    # Pause real cleanup after cancellation to make the ownership race deterministic.
    # The subprocess, its termination, capture and durable observer writes all remain real.
    stopping, cleaning, release = threading.Event(), threading.Event(), threading.Event()
    original = WorktreeManager.decommission_candidate
    cleanups: list[tuple[str, bool]] = []

    def cleanup(manager: WorktreeManager, path: Path) -> DecommissionResult:
        cleanups.append((str(path), stopping.is_set()))
        if stopping.is_set():
            cleaning.set()
            assert release.wait(10), "test did not release verification cleanup"
        return original(manager, path)

    monkeypatch.setattr(WorktreeManager, "decommission_candidate", cleanup)

    async def run() -> None:
        pending = asyncio.create_task(
            run_graph(
                repo,
                spec,
                config=config(),
                run_id=RUN,
                adapters={"claude": FakeAdapter(Scenario(write={"feature.txt": "preserved"}))},
            )
        )
        store = RunStore(repo, RUN)
        try:
            for _ in range(1000):
                if marker.exists():
                    break
                assert not pending.done(), "graph finished before verifier started"
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("verifier did not start")
            worker = store.read_run().nodes["a"].worker
            assert worker is not None
            identity = ProcessIdentity(worker.pid, worker.start)
            stopping.set()
            pending.cancel()
            assert await asyncio.to_thread(cleaning.wait, 5), f"missing cleanup: {cleanups}"
            pending.cancel()  # A second cancellation cannot break the first drain.
            if runner_shutdown:
                # Runner.close after a second SIGINT cancels every task, including
                # inner worker wrappers that asyncio.shield alone cannot protect.
                for running in asyncio.all_tasks():
                    if running is not asyncio.current_task():
                        running.cancel()
            await asyncio.sleep(0.05)
            assert not pending.done()
            assert check_identity(identity) is IdentityState.DEAD
            assert inspect_owner(repo, RUN).state is OwnerState.ALIVE
            assert store.read_run().state == "running"
            assert not any(e.type == "run.finished" for e in read_events(store.events_path))
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, timeout=10)
            assert inspect_owner(repo, RUN).state is OwnerState.ABSENT
            record = store.read_run()
            assert record.state == "interrupted"
            assert record.nodes["a"].worker is None
            assert record.nodes["a"].attempts == 1
            events = read_events(store.events_path)
            assert [e.data["state"] for e in events if e.type == "run.finished"] == ["interrupted"]
            assert events[-1].type == "run.finished"
            durable = (store.run_json.read_bytes(), store.events_path.read_bytes())
            await asyncio.sleep(0.1)
            assert durable == (store.run_json.read_bytes(), store.events_path.read_bytes())
            if phase == "node":
                assert (
                    WorktreeManager(repo, RUN).node_path("a") / "feature.txt"
                ).read_text() == "preserved"
            else:
                assert (
                    git(repo, "show", f"{record.nodes['a'].output_commit}:feature.txt")
                    == "preserved"
                )
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())
