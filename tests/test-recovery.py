"""Real foreground owner death and whole-attempt recovery (task 1.23)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import command_line, git, init_repo
from relay.adapters.base import AgentAdapter
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.cli.main import main
from relay.core import run as core_run
from relay.core.runlock import RunLock
from relay.core.runrecord import WorkerRecord
from relay.core.specs import ResultSpec, load_spec
from relay.core.store import RunStore
from test_run import config, spec

RUN = "2026-10-23-abcd"

# The child owns the real foreground lock. Killing it bypasses Python cleanup.
OWNER = r"""
import asyncio,sys,time
from pathlib import Path
from relay.adapters.fake import FakeAdapter
from relay.adapters.fake_scenario import Scenario
from relay.core import run as runner
from relay.core.registry import validate_merged
from relay.core.specs.task import TaskSpec,RunCheck
repo,phase,command=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
ready=repo.parent/'ready'
def pause():
    ready.write_text('ready')
    while True:time.sleep(0.05)
if phase in ('capture','publish'):
    name='capture_changes' if phase=='capture' else 'publish'
    original=getattr(runner,name)
    def stop(*args,**kwargs):
        result=original(*args,**kwargs)
        pause()
        return result
    setattr(runner,name,stop)
elif phase=='final':
    # After the durable run.finished, before the one record write publishing done+final_commit.
    def stop(*args,**kwargs):pause()
    runner._set_done=stop
elif phase=='complete':
    original=runner._set_node
    def stop(*args,**kwargs):
        original(*args,**kwargs)
        if args[2]=='done':pause()
    runner._set_node=stop
conf=validate_merged({'version':1,'defaults':{'permissions':'full','fallback':['fake']},'agents':{'fake':{'cmd':'fake','categories':['implement'],'models':{'fast':'fake'}}}})
task=TaskSpec(run_id='auto',id='n1',category='implement',goal='Add a feature',acceptance=[RunCheck(run=command)])
task.routing.agent='fake'
task.budget.max_attempts=2
adapter=FakeAdapter(Scenario(write={'feature.txt':'preserved'},hang=phase=='agent'))
asyncio.run(runner.run_foreground(repo,task,config=conf,adapters={'fake':adapter},run_id='2026-10-23-abcd',non_interactive=True))
"""


def wait_until_ready(repo: Path, phase: str, owner: subprocess.Popen[str]) -> RunStore:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if owner.poll() is not None:
            raise AssertionError("foreground owner exited before the crash point")
        try:
            store = RunStore(repo, RUN)
            record = store.read_run()
            if phase == "agent":
                if record.nodes["n1"].worker is not None:
                    node_dir = store.node_dir("n1")
                    observed = json.loads(
                        (node_dir / "fake-observed.json").read_text(encoding="utf-8")
                    )
                    if (Path(observed["cwd"]) / "feature.txt").exists():
                        return store
            elif (repo.parent / "ready").exists() and (
                phase != "verify" or record.nodes["n1"].worker is not None
            ):
                return store
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.03)
    raise AssertionError("foreground owner did not reach the crash point")


@pytest.mark.parametrize("phase", ["agent", "capture", "verify", "publish", "complete", "final"])
def test_killed_owner_resumes_from_durable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    if phase in ("agent", "verify") and os.name != "nt" and not sys.platform.startswith("linux"):
        pytest.skip("automatic orphan recovery requires verified process-start identities")
    repo = init_repo(tmp_path / "repo")
    if phase == "verify":
        script = tmp_path / "check.py"
        script.write_text(
            "import sys,time\nfrom pathlib import Path\np=Path(sys.argv[1])\n"
            "if not p.exists():\n p.write_text('ready')\n time.sleep(120)\n"
        )
        command = command_line([sys.executable, str(script), str(tmp_path / "ready")])
    else:
        command = command_line([sys.executable, "-c", "pass"])
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", OWNER, str(repo), phase, command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        store = wait_until_ready(repo, phase, owner)
        owner.kill()
        owner.wait(timeout=10)
        adapter = FakeAdapter(Scenario())

        def adapters(config: object) -> dict[str, AgentAdapter]:
            return {"fake": adapter}

        monkeypatch.setattr(core_run, "_default_adapters", adapters)
        assert main(["-C", str(repo), "resume", RUN, "--non-interactive", "--json"]) == 0
        record = store.read_run()
        assert record.state == "done"
        assert record.nodes["n1"].attempts == (1 if phase in ("complete", "final") else 2)
        assert len(adapter.invocations) == (0 if phase in ("complete", "final") else 1)
        assert git(repo, "show", f"{record.final_commit}:feature.txt") == "preserved"
        assert not (repo / "feature.txt").exists()
        assert record.nodes["n1"].active_duration_s > 0
        assert record.nodes["n1"].worker is None
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


def failed_run(repo: Path) -> RunStore:
    outcome = asyncio.run(
        core_run.run_foreground(
            repo,
            spec(),
            config=config(),
            adapters={"fake": FakeAdapter(Scenario(status="failed"))},
            run_id=RUN,
        )
    )
    assert outcome.exit_code == 1
    return RunStore(repo, RUN)


def test_explicit_failed_retry_preserves_total_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = failed_run(repo)
    adapter = FakeAdapter(Scenario(write={"retry.txt": "fixed"}))
    inject(monkeypatch, adapter)
    assert main(["-C", str(repo), "run", "--retry", RUN, "--json"]) == 6
    assert not adapter.invocations
    assert main(["-C", str(repo), "run", "--retry", RUN, "--max-attempts", "2", "--json"]) == 0
    assert store.read_run().nodes["n1"].attempts == 2
    assert len(adapter.invocations) == 1


@pytest.mark.parametrize("pending", [False, True])
def test_unconfirmed_worker_prevents_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = failed_run(repo)
    record = store.read_run()
    record.state = "interrupted"
    record.nodes["n1"].worker_pending = pending
    record.nodes["n1"].worker = None if pending else WorkerRecord(pid=os.getpid(), start=None)
    store.write_run(record)
    adapter = FakeAdapter()
    inject(monkeypatch, adapter)
    assert main(["-C", str(repo), "resume", RUN, "--max-attempts", "2", "--json"]) == 6
    assert store.read_run().nodes["n1"].blocked_reason == "worker_unconfirmed"
    assert not adapter.invocations


def test_live_owner_cannot_be_taken_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = init_repo(tmp_path / "repo")
    store = failed_run(repo)
    record = store.read_run()
    record.state = "interrupted"
    store.write_run(record)
    inject(monkeypatch, FakeAdapter())
    with RunLock.acquire(repo, RUN):
        before = store.run_json.read_bytes()
        assert main(["-C", str(repo), "resume", RUN, "--json"]) == 1
        assert store.run_json.read_bytes() == before


def inject(monkeypatch: pytest.MonkeyPatch, adapter: FakeAdapter) -> None:
    def adapters(config: object) -> dict[str, AgentAdapter]:
        return {"fake": adapter}

    monkeypatch.setattr(core_run, "_default_adapters", adapters)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX SIGINT delivery; Windows tree cleanup uses job tests"
)
def test_sigint_stops_worker_and_keeps_run_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    command = command_line([sys.executable, "-c", "pass"])
    owner = subprocess.Popen(
        [sys.executable, "-I", "-c", OWNER, str(repo), "agent", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        store = wait_until_ready(repo, "agent", owner)
        owner.send_signal(signal.SIGINT)
        owner.wait(timeout=10)
        assert store.read_run().state == "interrupted"
        assert store.read_run().nodes["n1"].worker is None
        adapter = FakeAdapter()
        inject(monkeypatch, adapter)
        assert main(["-C", str(repo), "resume", RUN, "--non-interactive", "--json"]) == 0
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


def test_legacy_interrupted_worker_requires_intervention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = failed_run(repo)
    record = store.read_run()
    record.state = "interrupted"
    record.nodes["n1"].state = "running"
    record.nodes["n1"].process_tracking = False
    store.write_run(record)
    adapter = FakeAdapter()
    inject(monkeypatch, adapter)
    assert main(["-C", str(repo), "resume", RUN, "--max-attempts", "2", "--json"]) == 6
    assert not adapter.invocations


def test_retry_rejects_changed_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["-C", str(tmp_path), "run", "--retry", RUN, "--check", "echo changed", "--json"]) == 2
    )
    assert "recorded inputs" in json.loads(capsys.readouterr().err)["error"]


def test_recovery_keeps_question_revisions_monotonic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo")
    store = failed_run(repo)
    record = store.read_run()
    record.state = "interrupted"
    record.nodes["n1"].question_revision = 3
    store.write_run(record)
    adapter = FakeAdapter(
        Scenario(status="needs_input", questions=[{"id": "q1", "text": "Which option?"}])
    )
    inject(monkeypatch, adapter)
    assert main(["-C", str(repo), "resume", RUN, "--max-attempts", "2", "--json"]) == 6
    result = load_spec(ResultSpec, store.node_dir("n1") / "result.spec.yaml")
    assert result.question_revision == 4
