"""End-to-end tests for the foreground ``relay run`` loop (design sections 6, 8.2, 9, 10, 14).

Slice A of task 1.19: one node, one attempt, in the foreground. Every test builds a real temp
git repository and drives :func:`relay.core.run.run_foreground` with the `fake` adapter, so no
network or real agent CLI is involved. The loop's responsibilities under test: routing, the
attempt state machine (capture -> verify -> publish -> integration), the durable run store,
``result.spec.yaml``, ``run.finished``, the interim lock, and worktree cleanup.

The CLI ``--json`` and invalid-input paths are covered at the bottom against
``relay.cli.commands.run`` (the ``run`` command loads configuration, so its ``load_config`` is
patched to keep the tests independent of the machine's user config file).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from harness import command_line, git, init_repo
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.exitcodes import Exit
from relay.cli.main import main
from relay.core import runlock as rl
from relay.core.events import read_events
from relay.core.gitbase import read_ref
from relay.core.publish import attempt_ref, integration_branch, node_ref
from relay.core.registry import Config, LoadedConfig, snapshot, validate_merged
from relay.core.run import RunResult, run_foreground
from relay.core.specs import ResultSpec
from relay.core.specs.task import Acceptance, RunCheck, TaskSpec
from relay.core.store import RunStore
from relay.core.worktree import WorktreeManager

RUN = "2026-10-22-a1b2"
NODE = "n1"


def config() -> Config:
    """A merged configuration with one eligible `fake` agent."""
    return validate_merged(
        {
            "version": 1,
            "defaults": {"permissions": "full", "fallback": ["fake"]},
            "agents": {
                "fake": {
                    "cmd": "fake",
                    "categories": ["implement"],
                    "models": {"fast": "fake-model"},
                }
            },
        }
    )


def spec(
    *, acceptance: list[Acceptance] | None = None, max_attempts: int | None = None
) -> TaskSpec:
    """A single-node write task pinned to `fake`, with a passing executable check by default."""
    if acceptance is None:
        acceptance = [RunCheck(run="echo ok", expect_exit=0)]
    task = TaskSpec(
        run_id=RUN, id=NODE, category="implement", goal="Do the thing", acceptance=acceptance
    )
    task.routing.agent = "fake"
    if max_attempts is not None:
        task.budget.max_attempts = max_attempts
    return task


def invoke(
    repo: Path,
    task: TaskSpec | None = None,
    *,
    scenario: Scenario | None = None,
    run_id: str = RUN,
    adapter: FakeAdapter | None = None,
) -> RunResult:
    """Run one node in the foreground with the `fake` adapter and return the result."""
    adapters = {"fake": adapter if adapter is not None else FakeAdapter(scenario or Scenario())}
    return asyncio.run(
        run_foreground(
            repo,
            task if task is not None else spec(),
            config=config(),
            adapters=adapters,
            run_id=run_id,
        )
    )


def worktree_path(repo: Path, run_id: str = RUN) -> Path:
    """Where the node's worktree lives under the auto-resolved root (POSIX layout)."""
    return WorktreeManager(repo, run_id).node_path(NODE)


def event_types(repo: Path) -> list[str]:
    store = RunStore.open(repo, RUN)
    return [event.type for event in read_events(store.events_path)]


# --- the happy path ---------------------------------------------------------------------------


def test_success_end_to_end(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    result = invoke(repo, scenario=Scenario(write={"src/hello.txt": "hello\n"}))

    assert result.exit_code == 0
    assert result.run_id == RUN
    assert result.node_id == NODE
    assert result.node_status == "done"
    assert result.output_commit is not None
    assert result.integration_commit is not None

    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.state == "done"
    assert record.final_commit == result.integration_commit
    node = record.nodes[NODE]
    assert node.state == "done"
    assert node.attempts == 1
    assert node.output_commit == result.output_commit

    # The orchestrator owns `result.spec.yaml`: ids, changes and verification are authoritative.
    result_spec = store.read_spec(
        f"{store.read_run().nodes[NODE].dir}/result.spec.yaml", ResultSpec
    )
    assert result_spec.status == "done"
    assert result_spec.agent == "fake"
    assert result_spec.model == "fake-model"
    assert result_spec.changes is not None
    assert result_spec.changes.files == ["src/hello.txt"]
    assert result_spec.changes.input_commit is not None
    assert result_spec.changes.output_commit == result.output_commit
    assert result_spec.verification is not None
    assert result_spec.verification.acceptance == "passed"
    assert [detail.run for detail in result_spec.verification.details] == ["echo ok"]

    # The task spec and the merged-config snapshot are durable alongside the result.
    assert store.path(f"{store.read_run().nodes[NODE].dir}/task.spec.yaml").is_file()
    assert store.path("config.snapshot.json").is_file()

    # Canonical event order: routing, the four attempt phases, verify, publish, integration,
    # then the two finish records.
    events = read_events(store.events_path)
    assert [event.type for event in events] == [
        "run.started",
        "node.routed",
        "node.started",
        "attempt.phase",
        "attempt.phase",
        "attempt.phase",
        "verify.result",
        "attempt.phase",
        "integration.published",
        "node.finished",
        "run.finished",
    ]
    phases = [event.data["phase"] for event in events if event.type == "attempt.phase"]
    assert phases == ["agent", "capture", "verify", "publish"]
    assert events[0].data["run_id"] == RUN
    assert events[-1].data == {
        "state": "done",
        "integration_commit": result.integration_commit,
    }

    # The durable output ref and the integration branch both point at the published commits.
    assert read_ref(repo, node_ref(RUN, NODE)) == result.output_commit
    assert read_ref(repo, integration_branch(RUN)) == result.integration_commit

    # The lock is released, the node worktree and the capture candidate are decommissioned, and
    # only the main worktree remains.
    assert not rl.lock_path(repo, RUN).exists()
    assert not worktree_path(repo).exists()
    assert not store.path(f"{NODE}/candidate").exists()
    listed = git(repo, "worktree", "list", "--porcelain")
    assert len([line for line in listed.splitlines() if line.startswith("worktree ")]) == 1


# --- failures ---------------------------------------------------------------------------------


def test_failed_agent_publishes_nothing_and_exits_1(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    result = invoke(repo, scenario=Scenario(status="failed", summary="agent gave up"))

    assert result.exit_code == 1
    assert result.node_status == "failed"

    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.state == "failed"
    assert record.nodes[NODE].state == "failed"

    # Nothing was published: no output ref, no integration branch, no result spec.
    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert read_ref(repo, integration_branch(RUN)) is None
    assert not store.path(f"{store.read_run().nodes[NODE].dir}/result.spec.yaml").exists()

    # The lock is released, but the failed node's worktree is kept for debugging (section 6).
    assert not rl.lock_path(repo, RUN).exists()
    assert worktree_path(repo).exists()
    assert "run.finished" in event_types(repo)


def test_failed_verification_publishes_nothing_and_exits_5(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    task = spec(acceptance=[RunCheck(run="false", expect_exit=0)])
    result = invoke(repo, task, scenario=Scenario(write={"src/bad.txt": "bad\n"}))

    assert result.exit_code == 5
    assert result.node_status == "failed"

    store = RunStore.open(repo, RUN)
    assert store.read_run().state == "failed"
    assert store.read_run().nodes[NODE].state == "failed"

    # Failed verification publishes nothing (design section 10 item 5).
    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert read_ref(repo, integration_branch(RUN)) is None
    assert not store.path(f"{store.read_run().nodes[NODE].dir}/result.spec.yaml").exists()

    # The lock is released; the failed node's worktree is kept for debugging.
    assert not rl.lock_path(repo, RUN).exists()
    assert worktree_path(repo).exists()


# --- retries (task 1.19 slice B1) -------------------------------------------------------------


def test_retry_after_agent_failure_then_success(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter(
        [
            Scenario(status="failed", summary="first attempt failed"),
            Scenario(write={"src/hello.txt": "hello\n"}, summary="second attempt"),
        ]
    )
    result = invoke(repo, spec(max_attempts=2), adapter=adapter)

    assert result.exit_code == 0
    assert result.node_status == "done"
    assert len(adapter.invocations) == 2

    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.state == "done"
    assert record.nodes[NODE].state == "done"
    assert record.nodes[NODE].attempts == 2

    events = read_events(store.events_path)
    phases = [
        (event.data["attempt"], event.data["phase"])
        for event in events
        if event.type == "attempt.phase"
    ]
    assert phases == [
        (1, "agent"),
        (2, "agent"),
        (2, "capture"),
        (2, "verify"),
        (2, "publish"),
    ]
    assert [
        event.data["decision"]["attempt"] for event in events if event.type == "node.routed"
    ] == [1, 2]
    # One node-start record per attempt; only the successful attempt finishes the node.
    assert len([event for event in events if event.type == "node.started"]) == 2
    assert [event.data["status"] for event in events if event.type == "node.finished"] == ["done"]

    # The failed attempt is recorded before attempt 2 is routed and started.
    types = [event.type for event in events]
    failed = types.index("warning")
    assert types.index("node.routed", failed) < types.index("node.started", failed)

    # Both routing decisions are projected, one per attempt.
    routes = [json.loads(line) for line in store.routes_path.read_text().splitlines()]
    assert [(entry["attempt"], entry["event"]) for entry in routes] == [
        (1, "routed"),
        (2, "routed"),
    ]

    # Only the successful attempt has an output ref, and it is decommissioned.
    assert result.output_commit is not None
    assert read_ref(repo, attempt_ref(RUN, NODE, 2)) == result.output_commit
    assert read_ref(repo, attempt_ref(RUN, NODE, 1)) is None
    assert not rl.lock_path(repo, RUN).exists()
    assert not worktree_path(repo).exists()
    assert not store.path(f"{NODE}/candidate").exists()


def test_retry_after_verification_failure_then_success(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    task = spec(
        acceptance=[
            RunCheck(
                run=command_line(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; raise SystemExit(Path('src/bad.txt').exists())",
                    ]
                ),
                expect_exit=0,
            )
        ],
        max_attempts=2,
    )
    adapter = FakeAdapter(
        [
            Scenario(write={"src/bad.txt": "bad\n"}, summary="bad attempt"),
            Scenario(
                delete=["src/bad.txt"],
                write={"src/good.txt": "good\n"},
                summary="good attempt",
            ),
        ]
    )
    result = invoke(repo, task, adapter=adapter)

    assert result.exit_code == 0
    assert len(adapter.invocations) == 2

    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.state == "done"
    assert record.nodes[NODE].state == "done"
    assert record.nodes[NODE].attempts == 2

    verify_events = [
        event for event in read_events(store.events_path) if event.type == "verify.result"
    ]
    assert [(event.data["attempt"], event.data["outcome"]) for event in verify_events] == [
        (1, "failed"),
        (2, "passed"),
    ]

    # Failed verification published nothing; the successful retry did.
    assert result.output_commit is not None
    assert read_ref(repo, attempt_ref(RUN, NODE, 2)) == result.output_commit
    assert read_ref(repo, attempt_ref(RUN, NODE, 1)) is None
    result_spec = store.read_spec(
        f"{store.read_run().nodes[NODE].dir}/result.spec.yaml", ResultSpec
    )
    assert result_spec.verification is not None
    assert result_spec.verification.acceptance == "passed"

    assert not rl.lock_path(repo, RUN).exists()
    assert not worktree_path(repo).exists()


def test_retry_after_schema_failure_then_success(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter([Scenario(malformed_result=True), Scenario(summary="fixed")])
    result = invoke(repo, spec(max_attempts=2), adapter=adapter)

    assert result.exit_code == 0
    assert len(adapter.invocations) == 2

    store = RunStore.open(repo, RUN)
    warnings = [event for event in read_events(store.events_path) if event.type == "warning"]
    assert len(warnings) == 1
    assert warnings[0].data["attempt"] == 1
    assert "result rejected" in warnings[0].data["message"]


def test_max_attempts_exhausted_agent_failure_exits_1(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    result = invoke(
        repo,
        spec(max_attempts=2),
        scenario=Scenario(status="failed", summary="never works"),
    )

    assert result.exit_code == 1
    assert result.node_status == "failed"
    assert result.reasons and "attempt 2" in result.reasons[-1]
    assert result.error == result.reasons[-1]

    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.state == "failed"
    assert record.nodes[NODE].state == "failed"
    assert record.nodes[NODE].attempts == 2

    # No published output from any failed attempt.
    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert read_ref(repo, integration_branch(RUN)) is None
    assert not store.path(f"{store.read_run().nodes[NODE].dir}/result.spec.yaml").exists()
    assert len([event for event in read_events(store.events_path) if event.type == "warning"]) == 2

    # The lock is released and the failed worktree is retained for debugging.
    assert not rl.lock_path(repo, RUN).exists()
    assert worktree_path(repo).exists()


def test_max_attempts_exhausted_verification_failure_exits_5(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    task = spec(acceptance=[RunCheck(run="false", expect_exit=0)], max_attempts=2)
    result = invoke(repo, task, scenario=Scenario(write={"src/bad.txt": "bad\n"}))

    assert result.exit_code == 5
    assert result.node_status == "failed"
    assert result.reasons and "verification failed" in result.reasons[-1]

    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.state == "failed"
    assert record.nodes[NODE].state == "failed"
    assert record.nodes[NODE].attempts == 2

    assert read_ref(repo, node_ref(RUN, NODE)) is None
    assert read_ref(repo, integration_branch(RUN)) is None
    assert not store.path(f"{store.read_run().nodes[NODE].dir}/result.spec.yaml").exists()
    assert not store.path(f"{NODE}/candidate").exists()

    assert not rl.lock_path(repo, RUN).exists()
    assert worktree_path(repo).exists()


def test_earlier_attempt_results_are_archived_and_never_overwritten(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    adapter = FakeAdapter(
        [
            Scenario(status="failed", summary="attempt one", output="file"),
            Scenario(status="failed", summary="attempt two", output="file"),
            Scenario(status="done", summary="attempt three", output="file"),
        ]
    )
    result = invoke(repo, spec(max_attempts=3), adapter=adapter)

    assert result.exit_code == 0
    store = RunStore.open(repo, RUN)
    record = store.read_run()
    assert record.nodes[NODE].attempts == 3

    node_dir = record.nodes[NODE].dir
    archived = [
        store.read_spec(f"{node_dir}/attempts/01/result.spec.yaml", ResultSpec),
        store.read_spec(f"{node_dir}/attempts/02/result.spec.yaml", ResultSpec),
    ]
    assert [entry.summary for entry in archived] == ["attempt one", "attempt two"]
    assert store.read_spec(f"{node_dir}/result.spec.yaml", ResultSpec).summary == "attempt three"
    from relay.cli.commands.inspect_common import load_result, load_run

    loaded = load_result(load_run(repo, RUN), NODE)
    assert loaded is not None
    assert loaded[0].verification is not None
    assert loaded[0].changes is not None
    assert loaded[0].changes.output_commit == result.output_commit
    assert not store.path(f"{node_dir}/attempts/01/result-2.spec.yaml").exists()
    assert not store.path(f"{node_dir}/attempts/02/result-2.spec.yaml").exists()
    assert not worktree_path(repo).exists()


def test_pinned_run_id_with_a_held_lock_is_refused(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    RunStore.create(repo, run_id=RUN)  # the run directory already exists ...
    held = rl.RunLock.acquire(repo, RUN)  # ... and someone else holds its lock
    try:
        result = invoke(repo, run_id=RUN)
    finally:
        held.release()

    # Slice A refuses cleanly via the store (the lock is held, so the id cannot be reused);
    # the live-owner refusal path itself is covered at unit level in `test_runlock.py`.
    assert result.exit_code == 1
    assert "used before" in (result.error or "")


# --- the CLI: --json and invalid input --------------------------------------------------------


@pytest.fixture
def isolated_load_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the run command's config source so tests never read the machine's user config."""

    def fake(project_root: Path | None = None, **kwargs: object) -> LoadedConfig:
        cfg = config()
        text, digest = snapshot(cfg)
        return LoadedConfig(config=cfg, sources=(), snapshot_json=text, config_hash=digest)

    monkeypatch.setattr("relay.cli.commands.run.load_config", fake)


def test_run_result_to_json_shape() -> None:
    result = RunResult(
        exit_code=0,
        run_id=RUN,
        node_id=NODE,
        node_status="done",
        output_commit="a" * 40,
        integration_commit="b" * 40,
        reasons=("some reason",),
    )
    assert result.to_json() == {
        "exit_code": 0,
        "run_id": RUN,
        "node_id": NODE,
        "node_status": "done",
        "output_commit": "a" * 40,
        "integration_commit": "b" * 40,
        "reasons": ["some reason"],
    }
    assert result.error is None


def test_cli_run_json_reports_invalid_input_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    isolated_load_config: None,
) -> None:
    assert main(["-C", str(tmp_path), "run", "   ", "--json"]) == Exit.INVALID
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"].startswith("relay run:")


def test_cli_run_rejects_bad_agent_permissions_cleanly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    isolated_load_config: None,
) -> None:
    assert main(["-C", str(tmp_path), "run", "Do the thing", "--agent-permissions", "nope"]) == (
        Exit.INVALID
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent permission 'nope' must be NAME=LEVEL" in captured.err
    assert "Traceback" not in captured.err
