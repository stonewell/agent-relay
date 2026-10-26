"""A killed graph owner leaves recoverable, identity-tracked worker state."""

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import command_line, git, init_repo
from relay.adapters.fake import FakeAdapter
from relay.core.events import read_events
from relay.core.graph import run_graph
from relay.core.platform.procid import IdentityState, ProcessIdentity, check_identity
from relay.core.recovery import stop_old_worker
from relay.core.registry import Config
from relay.core.store import RunStore, read_text
from relay.core.worktree import WorktreeManager

RUN = "2026-10-26-abcd"
OWNER = """
import asyncio,sys
from pathlib import Path
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.graph import run_graph
from relay.core.registry import validate_merged
from relay.core.specs import Graph
config=validate_merged({'version':1,'agents':{'fake':{'cmd':'fake','models':{'balanced':'default'}}}})
graph=Graph.model_validate({'verify':'none','verification_skip_reason':'Exercise owner recovery',
    'nodes':[{'id':'a','category':'implement','goal':'Work','budget':{'max_attempts':2},'routing':{'agent':'fake'}}]})
asyncio.run(run_graph(Path(sys.argv[1]),graph,config=config,run_id='2026-10-26-abcd',
    adapters={'fake':FakeAdapter(Scenario(write={'feature.txt':'preserved'},hang=True))}))
"""

VERIFY_OWNER = """
import asyncio,json,sys
from pathlib import Path
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core.graph import run_graph
from relay.core.registry import validate_merged
from relay.core.specs import Graph
config=validate_merged({'version':1,'agents':{'fake':{'cmd':'fake','models':{'balanced':'default'}}}})
graph=Graph.model_validate(json.loads(sys.argv[2]))
asyncio.run(run_graph(Path(sys.argv[1]),graph,config=config,run_id='2026-10-26-abcd',
    adapters={'fake':FakeAdapter(Scenario(write={'feature.txt':'preserved'}))}))
"""


def test_killed_graph_owner_resumes_preserved_work(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", OWNER, str(repo)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    store = RunStore(repo, RUN)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            assert owner.poll() is None, "graph owner exited before the crash point"
            if store.run_json.exists():
                record = store.read_run()
                if "a" in record.nodes and record.nodes["a"].worker is not None:
                    path = WorktreeManager(repo, RUN).node_path("a") / "feature.txt"
                    if path.exists():
                        break
            time.sleep(0.03)
        else:
            raise AssertionError("graph worker did not reach the crash point")
        owner.kill()
        owner.wait(timeout=10)
        record = store.read_run()
        assert record.graph is not None
        config = Config.model_validate_json(read_text(store.path("config.snapshot.json")))
        result = asyncio.run(
            run_graph(
                repo, record.graph, config=config, resume_run=RUN, adapters={"fake": FakeAdapter()}
            )
        )
        assert result.exit_code == 0, result.to_json()
        node = store.read_run().nodes["a"]
        assert node.attempts == 2
        assert node.worker is None
        assert node.active_duration_s > 0
        assert git(repo, "show", f"{result.integration_commit}:feature.txt") == "preserved"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


@pytest.mark.parametrize("phase", ["node", "integration"])
def test_killed_verifier_owner_cleans_scratch_before_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import json

    # Completed fake nodes are reusable only when no incomparable secret environment exists.
    for key in list(os.environ):
        if key.endswith(("_KEY", "_TOKEN")):
            monkeypatch.delenv(key)
    repo = init_repo(tmp_path / "repo")
    marker, release = tmp_path / "verifier-started", tmp_path / "verifier-release"
    script = tmp_path / "blocking-verifier.py"
    script.write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('started')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n",
        encoding="utf-8",
    )
    check = command_line([sys.executable, str(script)])
    skipped = {"verify": "none", "verification_skip_reason": "Exercise recovery"}
    data = {
        **(skipped if phase == "node" else {"verify": "default"}),
        "commands": {"test": check},
        "nodes": [
            {
                "id": "a",
                "category": "implement",
                "goal": "Work",
                "budget": {"max_attempts": 2},
                "routing": {"agent": "fake"},
                **({"verify": "default"} if phase == "node" else skipped),
            }
        ],
    }
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", VERIFY_OWNER, str(repo), json.dumps(data)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    store, manager = RunStore(repo, RUN), WorktreeManager(repo, RUN)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            assert owner.poll() is None, "owner exited before verifier started"
            if marker.exists():
                break
            time.sleep(0.03)
        else:
            raise AssertionError("verifier did not start")
        node = store.read_run().nodes["a"]
        assert node.worker is not None
        identity = ProcessIdentity(node.worker.pid, node.worker.start)
        candidate = store.path("a/candidate" if phase == "node" else "merge-candidate")
        assert candidate.is_dir()
        output = node.output_commit
        owner.kill()
        owner.wait(timeout=10)
        release.write_text("continue", encoding="utf-8")
        record = store.read_run()
        assert record.graph is not None
        cfg = Config.model_validate_json(read_text(store.path("config.snapshot.json")))
        adapter = FakeAdapter()
        result = asyncio.run(
            run_graph(repo, record.graph, config=cfg, resume_run=RUN, adapters={"fake": adapter})
        )
        assert result.exit_code == 0, result.to_json()
        assert check_identity(identity) is IdentityState.DEAD
        assert store.read_run().nodes["a"].attempts == (2 if phase == "node" else 1)
        assert len(adapter.invocations) == (1 if phase == "node" else 0)
        assert not candidate.exists()
        assert not manager.node_path("graph-verification").exists()
        assert git(repo, "show", f"{result.integration_commit}:feature.txt") == "preserved"
        if output:
            assert store.read_run().nodes["a"].output_commit == output
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        if store.run_json.exists() and "a" in store.read_run().nodes:
            stop_old_worker(store.read_run().nodes["a"])


def test_repeated_resume_cannot_bypass_unconfirmed_worker(tmp_path: Path) -> None:
    from relay.core.registry import validate_merged
    from relay.core.specs import Graph

    repo = init_repo(tmp_path / "repo")
    store = RunStore.create(repo, config_hash="test", run_id=RUN)
    store.add_node("a")
    store.update_run(lambda r: setattr(r.nodes["a"], "worker_pending", True))
    spec = Graph.model_validate(
        {
            "verify": "none",
            "verification_skip_reason": "Exercise recovery admission",
            "nodes": [{"id": "a", "category": "implement", "goal": "Work"}],
        }
    )
    cfg = validate_merged({"version": 1, "agents": {"fake": {"cmd": "fake"}}})
    adapter = FakeAdapter()
    for _ in range(2):
        result = asyncio.run(
            run_graph(
                repo,
                spec,
                config=cfg,
                resume_run=RUN,
                max_attempts=3,
                adapters={"fake": adapter},
            )
        )
        assert result.state == "blocked", result.to_json()
        assert result.error == "old worker termination unconfirmed"
        node = store.read_run().nodes["a"]
        assert node.worker_pending and node.recovery_pending
        assert node.blocked_reason == "worker_unconfirmed"
        assert node.attempts == 0
        assert not adapter.invocations


@pytest.mark.skipif(os.name == "nt", reason="SIGINT delivery requires a POSIX foreground process")
def test_second_sigint_keeps_ownership_until_integration_thread_finishes(tmp_path: Path) -> None:
    import json

    from relay.core.runlock import OwnerState, inspect_owner

    repo = init_repo(tmp_path / "repo")
    started = tmp_path / "verifier-started"
    cleaning, release = tmp_path / "cleanup-started", tmp_path / "cleanup-release"
    script = tmp_path / "blocking-verifier.py"
    script.write_text(
        "import time\nfrom pathlib import Path\n"
        f"Path({str(started)!r}).write_text('started')\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    data = {
        "commands": {"test": command_line([sys.executable, str(script)])},
        "nodes": [
            {
                "id": "a",
                "category": "implement",
                "goal": "Work",
                "routing": {"agent": "fake"},
                "verify": "none",
                "verification_skip_reason": "Exercise final interruption",
            }
        ],
    }
    delayed_cleanup = """
import sys,time
from pathlib import Path
from relay.core.worktree import WorktreeManager
original=WorktreeManager.decommission_candidate
def cleanup(self,path):
    if path.name == 'merge-candidate':
        Path(sys.argv[3]).write_text('cleaning')
        while not Path(sys.argv[4]).exists(): time.sleep(0.01)
    return original(self,path)
WorktreeManager.decommission_candidate=cleanup
"""
    owner = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-c",
            delayed_cleanup + VERIFY_OWNER,
            str(repo),
            json.dumps(data),
            str(cleaning),
            str(release),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    store = RunStore(repo, RUN)

    def reached(marker: Path) -> None:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            assert owner.poll() is None, "graph owner exited before the synchronization point"
            if marker.exists():
                return
            time.sleep(0.02)
        raise AssertionError(f"did not reach {marker.name}")

    try:
        reached(started)
        worker = store.read_run().nodes["a"].worker
        assert worker is not None
        identity = ProcessIdentity(worker.pid, worker.start)
        owner.send_signal(signal.SIGINT)
        reached(cleaning)
        owner.send_signal(signal.SIGINT)
        time.sleep(0.1)
        assert owner.poll() is None
        assert check_identity(identity) is IdentityState.DEAD
        assert inspect_owner(repo, RUN).state is OwnerState.ALIVE
        assert store.read_run().state == "running"
        assert not any(e.type == "run.finished" for e in read_events(store.events_path))
        release.write_text("continue", encoding="utf-8")
        assert owner.wait(timeout=10) != 0
        assert inspect_owner(repo, RUN).state is OwnerState.ABSENT
        assert store.read_run().state == "interrupted"
        assert store.read_run().nodes["a"].worker is None
        events = read_events(store.events_path)
        assert [e.data["state"] for e in events if e.type == "run.finished"] == ["interrupted"]
        assert events[-1].type == "run.finished"
    finally:
        release.write_text("continue", encoding="utf-8")
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)
        if store.run_json.exists() and "a" in store.read_run().nodes:
            stop_old_worker(store.read_run().nodes["a"])
